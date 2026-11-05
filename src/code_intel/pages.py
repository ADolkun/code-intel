"""The frontend pages the docs check knows, chosen by ``[frontend.pages] kind``.

``next-app`` and ``none`` work from repository paths alone (``doc_rules``).
``react-router`` reads the ``<Route path>`` literals of the configured route
modules from the TypeScript helper's facts and joins nested paths.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from code_intel.code_paths import FileStamp, SourceFile
from code_intel.config import Config
from code_intel.doc_rules import (
    CATCH_OPT,
    PARAM,
    STAGE,
    FrontendPage,
    NextAppPages,
    NoPages,
    PageProvider,
    PageSet,
    public_pages,
)
from code_intel.problems import Problem, diagnostic, limitation
from code_intel.ts_bridge import HelperError, Toolchain, ToolchainUnavailable
from code_intel.ts_facts import FactsCache, JsxElement, facts_problems

ROUTE_TAG = "Route"


def _join(base: str, path: str) -> str:
    return path if path.startswith("/") else f"{base.rstrip('/')}/{path}"


def _effective_paths(routes: Sequence[JsxElement]) -> dict[tuple[int, int], str]:
    """Each ``<Route>``'s path as React Router resolves it: its own ``path``
    joined onto the nearest enclosing route's, an absolute one standing alone.
    A route without a literal ``path`` (a layout, an index) passes its parent's
    through to its children."""
    by_position = {(route.line, route.column): route for route in routes}
    resolved: dict[tuple[int, int], str] = {}

    def resolve(route: JsxElement) -> str:
        key = (route.line, route.column)
        if key not in resolved:
            parent = by_position.get(route.ancestor) if route.ancestor is not None else None
            base = resolve(parent) if parent is not None else ""
            own = route.attributes.get("path")
            resolved[key] = base if own is None else _join(base, own)
        return resolved[key]

    for route in routes:
        resolve(route)
    return resolved


def _segment_variants(path: str) -> list[tuple[str, ...]]:
    """URL segments of a route path. ``:id`` is a parameter, a trailing ``*``
    takes zero or more segments, and an optional ``:id?`` yields the path with
    and without it."""
    variants: list[tuple[str, ...]] = [()]
    for segment in filter(None, path.split("/")):
        marker = CATCH_OPT if segment == "*" else PARAM if segment.startswith(":") else segment
        extended: list[tuple[str, ...]] = [(*v, marker) for v in variants]
        variants = [*variants, *extended] if segment.startswith(":") and segment.endswith("?") else extended
    return variants


def route_pages(rel: str, routes: Iterable[JsxElement]) -> list[FrontendPage]:
    """The pages of the ``<Route>`` elements of one module. A route whose path
    is only ``*`` is the not-found handler, which no doc names as a page."""
    elements = list(routes)
    effective = _effective_paths(elements)
    pages: dict[tuple[str, ...], FrontendPage] = {}
    for route in elements:
        if "path" not in route.attributes:
            continue
        full = effective[(route.line, route.column)]
        for segments in _segment_variants(full):
            if segments == (CATCH_OPT,):
                continue
            pages.setdefault(segments, FrontendPage(segments, "/" + full.strip("/"), rel, route.line))
    return list(pages.values())


@dataclass
class ReactRouterPages:
    """``kind = "react-router"``: the pages of ``modules``' ``<Route>`` elements.

    ``facts`` is a cache made with ``attribute_tags=(ROUTE_TAG,)``. The pages
    are unavailable, with a ``frontend_pages_unavailable`` limitation, when the
    TypeScript toolchain or helper cannot run; a listed module that is not a
    tracked file is a ``read_failed`` diagnostic.
    """

    repo_root: Path
    modules: tuple[str, ...]
    public_dir: str | None
    facts: FactsCache

    def pages(self, tracked: frozenset[str]) -> PageSet:
        public = public_pages(tracked, self.public_dir)
        problems: list[Problem] = []
        sources: list[SourceFile] = []
        for rel in self.modules:
            source = self._source(rel, tracked, problems)
            if source is not None:
                sources.append(source)
        found: list[FrontendPage] = []
        if sources:
            try:
                facts = self.facts.facts_for(sources)
            except ToolchainUnavailable as exc:
                return self._unavailable(public, problems, exc.problem.message, exc.problem.fix)
            except HelperError as exc:
                problem = exc.problem()
                return self._unavailable(public, problems, problem.message, problem.fix)
            for source in sources:
                module_facts = facts[source.rel]
                problems.extend(facts_problems(module_facts, stage=STAGE))
                found.extend(
                    route_pages(source.rel, (e for e in module_facts.elements if e.tag == ROUTE_TAG)),
                )
        return PageSet((*found, *public), problems=tuple(problems))

    def _source(self, rel: str, tracked: frozenset[str], problems: list[Problem]) -> SourceFile | None:
        path = self.repo_root / rel
        if rel not in tracked:
            problems.append(
                diagnostic(
                    "read_failed",
                    stage=STAGE,
                    step="read file",
                    file=rel,
                    message=f"{rel}: the react-router route module is not a tracked file",
                )
            )
            return None
        try:
            return SourceFile(path=path, rel=rel, stamp=FileStamp.from_stat(os.stat(path)))
        except OSError as exc:
            problems.append(diagnostic("stat_failed", stage=STAGE, step="stat file", file=rel, exc=exc))
            return None

    @staticmethod
    def _unavailable(public: list[FrontendPage], problems: list[Problem], message: str, fix: str) -> PageSet:
        problems.append(
            limitation(
                "frontend_pages_unavailable",
                stage=STAGE,
                file=None,
                line=None,
                message=f"the react-router pages were not read: {message}. Fix: {fix}",
            )
        )
        return PageSet(tuple(public), available=False, problems=tuple(problems))


def page_provider(
    repo_root: Path, config: Config, toolchain: Callable[[], Toolchain], clock: Callable[[], int]
) -> PageProvider:
    """The provider for ``config.frontend.pages``."""
    pages = config.frontend.pages
    if pages.kind == "react-router":
        cache = FactsCache(repo_root, toolchain, clock=clock, attribute_tags=(ROUTE_TAG,))
        return ReactRouterPages(repo_root, pages.modules, pages.public_dir, cache)
    if pages.kind == "next-app":
        return NextAppPages(posixpath.join(config.frontend.root, "app"), pages.public_dir)
    return NoPages(pages.public_dir)
