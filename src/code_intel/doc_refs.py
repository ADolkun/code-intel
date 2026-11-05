"""Docs-vs-code references: what the scope docs name, and whether it still exists.

For each Markdown file in the config's scope this extracts the links and
anchors, repository paths, backend routes and code symbols it names and has
``doc_resolvers`` resolve each against **injected** inputs: the git-visible
files (``repo_files.RepoFiles``), the analyzed routes (DI rows), a symbol table
(``SymbolIndex`` rows) and identifier words read from tracked code files. Here
the report settles git-ignored paths in one batch, applies the ignore entries
and walks links from the reachability root to find orphaned docs. Text rules
(what counts as a path, a route or a symbol) live in ``doc_rules``; Markdown
structure comes from ``markdown_facts``.

``DocIndex`` keeps per-file Markdown facts, code words and the names each
package's ``__init__.py`` binds between reports,
keyed by the file stamp and checked by content inside the racy window, the
way ``symbol_resolve.SymbolIndex`` caches its Python index. Resolution itself
runs on every report.
"""

from __future__ import annotations

import os
import posixpath
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_intel.code_paths import FileStamp, content_digest, is_racy, read_bytes_with_digest
from code_intel.code_words import EMPTY, CodeWords, code_words_from_bytes, is_code_file, package_bindings_from_bytes
from code_intel.doc_report import (
    ConfigFinding,
    DocEntry,
    DocReport,
    Finding,
    Reference,
    RouteRow,
    SymbolRow,
    reference_message,
    route_rows,
    symbol_rows,
)
from code_intel.doc_resolvers import (
    Draft,
    LinkResolver,
    PathResolver,
    ResolutionContext,
    RouteResolver,
    SymbolResolver,
    untracked_path,
)
from code_intel.doc_rules import (
    BROKEN_STATUSES,
    STAGE,
    DocRefsConfig,
    NoPages,
    PageProvider,
    PageSet,
    PathForm,
    RouteMention,
    bare_route_mention,
    is_markdown,
    method_route_mentions,
    symbol_mention,
)
from code_intel.markdown_facts import DocFacts, read_doc_facts
from code_intel.problems import Problem, diagnostic
from code_intel.repo_files import RepoFiles

# A limitation, not the `frontend_unavailable` diagnostic: existence still
# resolves through code words, so no status depends on frontend definitions.
FRONTEND_LIMITATION_KIND = "frontend_definitions_unavailable"

_STAT_ERRORS: tuple[type[Exception], ...] = (OSError,)

# More threads stopped helping at 4 on a 14-core Mac.
_PREFETCH_WORKERS = min(8, os.process_cpu_count() or 1)


# ── Cache ────────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    stamp: FileStamp
    racy: bool
    digest: str | None
    value: Any
    problems: tuple[Problem, ...]


_Loader = Callable[[Path, str], tuple[Any, "str | None", list[Problem], bool]]


def _load_doc(path: Path, rel: str) -> tuple[Any, str | None, list[Problem], bool]:
    facts, digest, problem = read_doc_facts(path, rel, stage=STAGE)
    return facts, digest, [problem] if problem is not None else [], digest is None


def _load_words(path: Path, rel: str) -> tuple[Any, str | None, list[Problem], bool]:
    data, digest, problem = read_bytes_with_digest(path, rel, stage=STAGE)
    if data is None:
        return EMPTY, None, [problem] if problem is not None else [], True
    words, problems = code_words_from_bytes(rel, data, stage=STAGE)
    return words, digest, problems, False


def _load_bindings(path: Path, rel: str) -> tuple[Any, str | None, list[Problem], bool]:
    data, digest, problem = read_bytes_with_digest(path, rel, stage=STAGE)
    if data is None:
        return frozenset(), None, [problem] if problem is not None else [], True
    names, problems = package_bindings_from_bytes(rel, data, stage=STAGE)
    return names, digest, problems, False


def _try_digest(path: Path) -> str | None:
    try:
        return content_digest(path)
    except _STAT_ERRORS:
        return None


class DocIndex:
    """Per-file Markdown facts, code words and package bindings, cached by
    file stamp.

    An entry stored inside git's racy window also keeps the SHA-256 of the
    bytes it came from, and is reused only while the file still hashes the
    same; read failures are never cached because they can clear without a
    stamp change.
    """

    def __init__(self, repo_root: Path, *, clock: Callable[[], int] = time.time_ns) -> None:
        self.repo_root = repo_root
        self._clock = clock
        self._lock = threading.RLock()
        self._docs: dict[str, _Entry] = {}
        self._words: dict[str, _Entry] = {}
        self._bindings: dict[str, _Entry] = {}

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()
            self._words.clear()
            self._bindings.clear()

    def doc_facts(self, rel: str, problems: list[Problem]) -> DocFacts | None:
        with self._lock:
            return self._cached(self._docs, rel, _load_doc, problems, None)

    def code_words(self, rel: str, problems: list[Problem]) -> CodeWords:
        with self._lock:
            return self._cached(self._words, rel, _load_words, problems, EMPTY)

    def package_bindings(self, rel: str, problems: list[Problem]) -> frozenset[str]:
        with self._lock:
            return self._cached(self._bindings, rel, _load_bindings, problems, frozenset())

    def prune(self, docs: Iterable[str], words: Iterable[str] | None, inits: Iterable[str]) -> None:
        """Drop entries for files a report no longer read. ``words`` is None
        when the report needed no words, which says nothing about the files."""
        with self._lock:
            keep_docs = set(docs)
            for key in [k for k in self._docs if k not in keep_docs]:
                del self._docs[key]
            keep_inits = set(inits)
            for key in [k for k in self._bindings if k not in keep_inits]:
                del self._bindings[key]
            if words is not None:
                keep_words = set(words)
                for key in [k for k in self._words if k not in keep_words]:
                    del self._words[key]

    def _prefetch(self, docs: Iterable[str], code: Iterable[str]) -> None:
        """Loads a report's docs and code words on threads, so its walk finds
        them cached; the walk still collects their problems from the cache.
        The loads run in parallel only on a free-threaded Python (CI's docs
        check uses one). The caller holds the lock for the whole report."""
        jobs = [(self._docs, rel, _load_doc, None) for rel in docs]
        jobs += [(self._words, rel, _load_words, EMPTY) for rel in code]
        with ThreadPoolExecutor(max_workers=_PREFETCH_WORKERS) as pool:
            for _ in pool.map(lambda job: self._cached(job[0], job[1], job[2], [], job[3]), jobs):
                pass

    def _cached(self, cache: dict[str, _Entry], rel: str, load: _Loader, problems: list[Problem], failed: Any) -> Any:
        path = self.repo_root / rel
        try:
            stamp = FileStamp.from_stat(os.stat(path))
        except _STAT_ERRORS as exc:
            cache.pop(rel, None)
            problems.append(diagnostic("stat_failed", stage=STAGE, step="stat file", file=rel, exc=exc))
            return failed
        entry = cache.get(rel)
        if entry is not None and self._still_valid(entry, path, stamp):
            problems.extend(entry.problems)
            return entry.value
        racy = is_racy(stamp, self._clock())
        value, digest, found, read_failed = load(path, rel)
        problems.extend(found)
        if read_failed or (racy and digest is None):
            cache.pop(rel, None)
        else:
            cache[rel] = _Entry(stamp, racy, digest if racy else None, value, tuple(found))
        return value

    def _still_valid(self, entry: _Entry, path: Path, stamp: FileStamp) -> bool:
        """Same stamp, and for an entry stored inside the racy window, same
        content; an entry confirmed after the window drops its digest."""
        if entry.stamp != stamp:
            return False
        if not entry.racy:
            return True
        if _try_digest(path) != entry.digest:
            return False
        if not is_racy(stamp, self._clock()):
            entry.racy = False
            entry.digest = None
        return True

    def report(
        self,
        repo: RepoFiles,
        routes: Iterable[Mapping[str, Any]],
        unmounted_routes: Iterable[Mapping[str, Any]],
        route_limitations: Iterable[Mapping[str, Any]],
        symbols: Iterable[Any],
        frontend_available: bool,
        config: DocRefsConfig,
        extra_problems: Sequence[Problem] = (),
        pages: PageProvider | None = None,
    ) -> DocReport:
        """Resolve every scope doc. ``extra_problems`` (DI diagnostics, symbol
        table problems) are carried into the report as they are. ``pages`` finds
        the frontend's pages; without one only the backend routes are known."""
        with self._lock:
            page_set = (pages or NoPages()).pages(repo.tracked)
            run = _Run(
                self, repo, config, route_rows(routes), route_rows(unmounted_routes), symbol_rows(symbols), page_set
            )
            self._prefetch(run.scope, run.code_files() if run.checks_symbols else ())
            report = run.build(tuple(route_limitations), frontend_available, [*extra_problems, *page_set.problems])
            self.prune(run.facts_read, run.words_read if run.words_used else None, run.inits_read)
            return report


# ── Report ───────────────────────────────────────────────────────────────


def _route_mentions(facts: DocFacts) -> Iterator[tuple[RouteMention, int, str, bool]]:
    """Every route mention of a doc as ``(mention, line, context, bare)``: a
    code span is a bare ``/path`` only when it names no ``METHOD /path``."""
    for span in facts.code_spans:
        mentions = method_route_mentions(span.text, code=True)
        if mentions:
            for mention in mentions:
                yield mention, span.line, span.context, False
        else:
            bare = bare_route_mention(span.text)
            if bare is not None:
                yield bare, span.line, span.context, True
    for fence in facts.fences:
        for offset, text in enumerate(fence.lines):
            for mention in method_route_mentions(text, code=True):
                yield mention, fence.content_line + offset, "fence", False
    for run in facts.text_runs:
        for mention in method_route_mentions(run.text):
            yield mention, run.line, run.context, False


class _Run:
    """One report: walks the scope docs, hands each reference to its resolver
    (``doc_resolvers``), settles the git-ignored paths in one batch, applies
    the ignore entries, finds the orphans and assembles the ``DocReport``.
    It also reads and caches the inputs a resolver asks for (a doc's facts,
    the code words) through the ``DocIndex``."""

    def __init__(
        self,
        index: DocIndex,
        repo: RepoFiles,
        config: DocRefsConfig,
        routes: tuple[RouteRow, ...],
        unmounted: tuple[RouteRow, ...],
        symbols: tuple[SymbolRow, ...],
        page_set: PageSet,
    ) -> None:
        self.index = index
        self.repo = repo
        self.config = config
        self.problems: list[Problem] = []
        self.inits_read = sorted(rel for rel in repo.tracked if posixpath.basename(rel) == "__init__.py")
        # Read for every report, not on demand: the lookup qualifies dotted
        # names with the same bindings the check used.
        bindings = {posixpath.dirname(rel): index.package_bindings(rel, self.problems) for rel in self.inits_read}
        self.ctx = ResolutionContext.build(repo, config, routes, unmounted, symbols, bindings, page_set)
        self.scope = sorted(rel for rel in repo.files if config.in_scope(rel))
        self.checks_symbols = any("symbols" in config.checks_for(doc) for doc in self.scope)
        self.facts_read: set[str] = set()
        self.words_read: set[str] = set()
        self._facts: dict[str, DocFacts | None] = {}
        self._words: CodeWords | None = None
        self.links = LinkResolver(self.ctx, self.facts)
        self.paths = PathResolver(self.ctx)
        self.symbols = SymbolResolver(self.ctx, self.words)

    # Inputs ----------------------------------------------------------------

    def facts(self, rel: str) -> DocFacts | None:
        if rel not in self._facts:
            self.facts_read.add(rel)
            self._facts[rel] = self.index.doc_facts(rel, self.problems)
        return self._facts[rel]

    @property
    def words_used(self) -> bool:
        return self._words is not None

    def code_files(self) -> list[str]:
        """Every tracked code file except the consumer's config
        (``DocRefsConfig.is_config_file``)."""
        return [rel for rel in sorted(self.repo.tracked) if is_code_file(rel) and not self.config.is_config_file(rel)]

    def words(self) -> CodeWords:
        """Identifier words of the `code_files`, read on first use."""
        if self._words is None:
            words: set[str] = set()
            dotted: set[str] = set()
            for rel in self.code_files():
                self.words_read.add(rel)
                found = self.index.code_words(rel, self.problems)
                words.update(found.words)
                dotted.update(found.dotted)
            self._words = CodeWords(frozenset(words), frozenset(dotted))
        return self._words

    # Build -----------------------------------------------------------------

    def build(
        self,
        route_limitations: tuple[Mapping[str, Any], ...],
        frontend_available: bool,
        extra_problems: Sequence[Problem],
    ) -> DocReport:
        hint = (
            "; see the limitation below if the route is mounted by code the analysis cannot see"
            if route_limitations
            else ""
        )
        scope = self.scope
        routes = RouteResolver(self.ctx, hint, self._declared_roots(scope))
        drafts: dict[str, list[Draft]] = {}
        for doc in scope:
            facts = self.facts(doc)
            checks = self.config.checks_for(doc)
            drafts[doc] = self._doc_drafts(doc, facts, checks, routes) if facts is not None else []
        self._settle_pending([d for items in drafts.values() for d in items])
        config_findings = self._apply_ignores(drafts)

        docs: dict[str, DocEntry] = {}
        for doc in scope:
            rule = self.config.rule_for(doc)
            facts = self.facts(doc)
            refs = sorted((d.freeze() for d in drafts[doc]), key=lambda r: (r.line, r.kind, r.text))
            docs[doc] = DocEntry(
                doc=doc,
                checks=rule.checks if rule is not None else self.config.checks_for(doc),
                rule_reason=rule.reason if rule is not None else None,
                headings=facts.headings if facts is not None else (),
                references=tuple(refs),
            )

        inbound = self._inbound(scope)
        reachable = self._reachable()
        orphans = tuple(
            sorted(
                rel
                for rel in self.repo.files
                if self.config.in_orphan_scope(rel) and is_markdown(rel) and rel not in reachable
            )
        )
        problems: list[Problem] = [*self.repo.submodule_limitations(STAGE), *self.problems, *extra_problems]
        if not frontend_available:
            problems.append(
                Problem(
                    kind=FRONTEND_LIMITATION_KIND,
                    category=None,
                    stage=STAGE,
                    step=None,
                    file="frontend",
                    line=None,
                    message=(
                        "frontend definitions were not indexed (Node or TypeScript unavailable), so frontend symbols "
                        "carry no definition location; their existence still resolves through tracked code"
                    ),
                )
            )
        return DocReport(
            docs=docs,
            inbound=inbound,
            orphans=orphans,
            reachable=frozenset(reachable),
            config_findings=tuple(config_findings),
            routes=self.ctx.routes,
            route_limitations=route_limitations,
            symbols=self.ctx.symbols,
            problems=tuple(problems),
            config=self.config,
            package_bindings=self.ctx.package_bindings,
        )

    def _doc_drafts(self, doc: str, facts: DocFacts, checks: tuple[str, ...], routes: RouteResolver) -> list[Draft]:
        out: list[Draft] = []
        if "links" in checks:
            out.extend(self.links.link(doc, link) for link in facts.links)
        if "paths" in checks:
            for span in facts.code_spans:
                form: PathForm = "command" if any(ch.isspace() for ch in span.text.strip()) else "span"
                out.extend(self.paths.paths(doc, span.text, form, span.line, span.context))
            for fence in facts.fences:
                for offset, text in enumerate(fence.lines):
                    out.extend(self.paths.paths(doc, text, "fence", fence.content_line + offset, "fence"))
        if "routes" in checks:
            out.extend(
                routes.route(doc, mention, line, context, bare=bare)
                for mention, line, context, bare in _route_mentions(facts)
            )
        if "symbols" in checks:
            for span in facts.code_spans:
                mention = symbol_mention(span.text)
                if mention is not None:
                    out.append(self.symbols.symbol(doc, mention, span.line, span.context))
        return out

    def _declared_roots(self, scope: Sequence[str]) -> frozenset[str]:
        """The roots the route-checked docs declare with an un-gated
        ``METHOD /path``, read before any bare ``/path`` is judged so the
        verdict does not depend on the order the docs are walked in."""
        roots: set[str] = set()
        for doc in scope:
            facts = self.facts(doc)
            if facts is None or "routes" not in self.config.checks_for(doc):
                continue
            for mention, _, _, bare in _route_mentions(facts):
                root = RouteResolver.declared_root(mention, bare=bare, prefixes=self.config.route_prefixes)
                if root is not None:
                    roots.add(root)
        return frozenset(roots)

    def _settle_pending(self, drafts: Sequence[Draft]) -> None:
        pending = [d for d in drafts if d.pending]
        asked = {path for d in pending for p in d.pending for path in (p, p + "/")}
        ignored = self.repo.ignored(asked) if asked else frozenset()
        for draft in pending:
            if any(p in ignored or p + "/" in ignored for p in draft.pending):
                draft.set("local_only", "ignored by git, so it exists only locally")
            draft.pending = ()

    # Reachability ----------------------------------------------------------

    def _inbound(self, scope: Sequence[str]) -> dict[str, tuple[tuple[str, int], ...]]:
        inbound: dict[str, list[tuple[str, int]]] = {}
        for doc in scope:
            facts = self.facts(doc)
            if facts is None:
                continue
            for link in facts.links:
                target = self.links.markdown_target(doc, link)
                if target is not None and target != doc:
                    inbound.setdefault(target, []).append((doc, link.line))
        return {target: tuple(sorted(set(rows))) for target, rows in sorted(inbound.items())}

    def _reachable(self) -> set[str]:
        reachability = self.config.reachability
        if reachability is None or reachability.root not in self.repo.files:
            return set()
        root = reachability.root
        reached = {root}
        queue = deque([root])
        while queue:
            doc = queue.popleft()
            facts = self.facts(doc)
            if facts is None:
                continue
            for link in facts.links:
                target = self.links.markdown_target(doc, link)
                if target is not None and target not in reached:
                    reached.add(target)
                    queue.append(target)
        return reached

    # Ignore entries --------------------------------------------------------

    def _apply_ignores(self, drafts: Mapping[str, list[Draft]]) -> list[ConfigFinding]:
        """Mark failing references an entry names as ``ignored``. An entry is
        used when it names a failing reference, or one that could not be
        checked here (a link or path into a submodule that is not checked
        out), so a local run without submodules does not call a CI-needed
        entry unused."""
        findings: list[ConfigFinding] = []
        for index, entry in enumerate(self.config.ignore):
            used = False
            for draft in drafts.get(entry.file, ()):
                if draft.kind != entry.kind or entry.text not in (draft.text, draft.raw_text):
                    continue
                if draft.status in BROKEN_STATUSES:
                    draft.set("ignored", f"ignored ({draft.status}): {entry.reason}", checked=False)
                    used = True
                elif draft.status == "not_checked":
                    used = True
            if not used:
                findings.append(
                    ConfigFinding(
                        file=self.config.path,
                        line=self.config.ignore_line(index),
                        kind="ignore",
                        text=entry.text,
                        message=(
                            f"unused ignore entry: {entry.kind} {entry.text!r} in {entry.file} "
                            "(no failing reference matches it)"
                        ),
                    )
                )
        return findings


def build_report(
    repo: RepoFiles,
    routes: Iterable[Mapping[str, Any]],
    unmounted_routes: Iterable[Mapping[str, Any]],
    route_limitations: Iterable[Mapping[str, Any]],
    symbols: Iterable[Any],
    frontend_available: bool,
    config: DocRefsConfig,
    extra_problems: Sequence[Problem] = (),
    pages: PageProvider | None = None,
) -> DocReport:
    """One report with a throwaway cache (tests and one-shot callers)."""
    return DocIndex(repo.repo_root).report(
        repo, routes, unmounted_routes, route_limitations, symbols, frontend_available, config, extra_problems, pages
    )


# What other modules take from here: the runtime builds reports with
# `DocIndex`, takes its settings from `DocRefsConfig.from_config` and drops
# problems of untracked files with `untracked_path` (defined with the
# resolvers, which filter routes and symbols by it); tests and one-shot
# callers use `build_report` and the result types.
__all__ = [
    "DocIndex",
    "DocRefsConfig",
    "DocReport",
    "Finding",
    "Reference",
    "build_report",
    "reference_message",
    "untracked_path",
]
