"""How the docs check resolves one reference: a link and its anchor, a
repository path, a backend route or a code symbol.

Everything a resolver needs is in an immutable ``ResolutionContext``, built
once per report from the injected inputs (git-visible files, routes, symbol
rows, the config), plus two accessors the report passes in: a doc's Markdown
facts (for anchors) and the identifier words of tracked code (for symbols),
both read and cached by ``doc_refs``. A resolver reads no file, runs no git and
keeps no state between references, so the same context and inputs always give
the same verdict. ``doc_refs`` walks the docs, dispatches each reference here,
settles the git-ignored paths, applies the ignore entries and builds the report.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import NamedTuple
from urllib.parse import unquote

from code_intel.code_paths import is_under
from code_intel.code_words import BUILTIN_WORDS, CodeWords
from code_intel.doc_report import Reference, RouteRow, SymbolRow
from code_intel.doc_rules import (
    CLASS_KINDS,
    FRAMEWORK_PATHS,
    NO_BINDINGS,
    NO_PAGES,
    WILD,
    DocRefsConfig,
    FrontendPage,
    PageSet,
    PathForm,
    RouteMention,
    SymbolMention,
    added_prefix_variants,
    expand_braces,
    glob_regex,
    has_glob,
    has_known_extension,
    has_route_placeholder,
    has_skip_dir,
    is_example_path,
    is_example_symbol,
    is_external_href,
    is_markdown,
    is_placeholder_path,
    is_upper_fragment,
    match_segments,
    match_suffix,
    mention_segments,
    methods_compatible,
    normalize_repo_path,
    omitted_prefix_variants,
    path_tokens,
    path_variants,
    qualifiers_match,
    route_root,
    route_tail,
    split_path_token,
    strict_prefix,
    strict_suffix,
    symbol_tier,
)
from code_intel.markdown_facts import DocFacts, Link, match_anchor
from code_intel.repo_files import RepoFiles

_INDEX_NAMES: tuple[str, ...] = ("readme.md", "index.md")
# `Path.exists` raises ValueError for a NUL byte in the name.
_EXISTS_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)

FactsReader = Callable[[str], "DocFacts | None"]
WordsReader = Callable[[], CodeWords]


def untracked_path(repo: RepoFiles, file: str | None) -> bool:
    """``file`` names a repository path git does not track: CI's checkout has
    no such file, so a problem, route or page that comes from it would make a
    local report differ from CI's.

    A real path is one that exists under the repository root, or a
    path-shaped name (one with a ``/``) in a directory git lists. Anything
    else (no file, a tracked file or directory, a module name such as
    ``service_backend.main``, a path outside the repository) is not one, so
    a problem that names it is kept.
    """
    if not file or file in repo.tracked or file in repo.dirs:
        return False
    if file.startswith("/") or ".." in file.split("/"):
        return False
    if "/" in file and posixpath.dirname(file) in repo.dirs:
        return True
    try:
        return (repo.repo_root / file).exists()
    except _EXISTS_ERRORS:
        return False


# ── Drafts ───────────────────────────────────────────────────────────────


@dataclass
class Draft:
    """A reference while it is being resolved; ``freeze`` makes the
    ``Reference`` the report keeps."""

    doc: str
    line: int
    kind: str
    text: str
    context: str
    target: str | None = None
    status: str = "ok"
    detail: str | None = None
    checked: bool = True
    defined_in: tuple[str, ...] = ()
    route: tuple[str | None, str] | None = None
    # A link's href as written, when percent-decoding changed it: an ignore
    # entry may name either form.
    raw_text: str | None = None
    # Paths to ask `git check-ignore` about before a `missing` verdict stands.
    pending: tuple[str, ...] = field(default=())

    def set(self, status: str, detail: str | None = None, *, checked: bool | None = None) -> Draft:
        self.status = status
        self.detail = detail
        if checked is not None:
            self.checked = checked
        return self

    def freeze(self) -> Reference:
        return Reference(
            doc=self.doc,
            line=self.line,
            kind=self.kind,
            text=self.text,
            context=self.context,
            target=self.target,
            status=self.status,
            detail=self.detail,
            checked=self.checked,
            defined_in=self.defined_in,
            route=self.route,
        )


# ── Context ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolutionContext:
    """One report's inputs and the indexes derived from them. Routes, pages
    and symbols of untracked files are already dropped (``untracked_path``),
    as are symbols of the checker's own files."""

    repo: RepoFiles
    config: DocRefsConfig
    routes: tuple[RouteRow, ...]
    unmounted: tuple[RouteRow, ...]
    symbols: tuple[SymbolRow, ...]
    # First path segments of every git-visible path, and of the paths under
    # each of the config's `path_roots`.
    top: frozenset[str]
    children: Mapping[str, frozenset[str]]
    dir_names: frozenset[str]
    by_basename: Mapping[str, tuple[str, ...]]
    # Directory → {lowercased README.md/index.md name → path}.
    markdown_in_dir: Mapping[str, Mapping[str, str]]
    pages: tuple[FrontendPage, ...]
    # False when the pages could not be read: a path only a page could satisfy is then not checked.
    pages_available: bool
    backend_roots: frozenset[str]
    gate_roots: frozenset[str]
    by_name: Mapping[str, tuple[SymbolRow, ...]]
    python_classes: frozenset[str]
    # Package directory → the names its `__init__.py` binds (`qualifiers_match`).
    package_bindings: Mapping[str, frozenset[str]]

    @classmethod
    def build(
        cls,
        repo: RepoFiles,
        config: DocRefsConfig,
        routes: Sequence[RouteRow],
        unmounted: Sequence[RouteRow],
        symbols: Sequence[SymbolRow],
        package_bindings: Mapping[str, frozenset[str]] = NO_BINDINGS,
        page_set: PageSet = NO_PAGES,
    ) -> ResolutionContext:
        # Routes of untracked files are dropped as their symbol rows are.
        kept_routes = tuple(row for row in routes if not untracked_path(repo, row.file))
        kept_unmounted = tuple(row for row in unmounted if not untracked_path(repo, row.file))
        kept_symbols = tuple(row for row in symbols if row.file in repo.tracked and not config.is_config_file(row.file))

        everything = repo.files | repo.dirs
        children = {
            prefix: frozenset(path[len(prefix) :].split("/", 1)[0] for path in everything if path.startswith(prefix))
            for prefix in (root.prefix for root in config.path_roots)
        }
        by_basename: dict[str, list[str]] = {}
        for path in everything:
            by_basename.setdefault(posixpath.basename(path), []).append(path)
        markdown_in_dir: dict[str, dict[str, str]] = {}
        for path in repo.files:
            name = posixpath.basename(path).lower()
            if name in _INDEX_NAMES:
                markdown_in_dir.setdefault(posixpath.dirname(path), {})[name] = path

        pages = page_set.pages
        prefixes = config.route_prefixes
        backend_roots = _roots((row.segments for row in (*kept_routes, *kept_unmounted)), prefixes)
        # FastAPI serves its own pages (`match` knows them), whatever the app mounts.
        framework_roots = _roots((mention_segments(path) for path in FRAMEWORK_PATHS), prefixes)
        gate_roots = backend_roots | framework_roots | _roots((page.segments for page in pages), prefixes)

        by_name: dict[str, list[SymbolRow]] = {}
        for row in kept_symbols:
            by_name.setdefault(row.name, []).append(row)
        python_classes = frozenset(
            row.name for row in kept_symbols if row.kind in CLASS_KINDS and row.language == "python"
        )
        return cls(
            repo=repo,
            config=config,
            routes=kept_routes,
            unmounted=kept_unmounted,
            symbols=kept_symbols,
            top=frozenset(path.split("/", 1)[0] for path in everything),
            # Read-only: one context is shared by every resolver of a report.
            children=MappingProxyType(children),
            dir_names=frozenset(posixpath.basename(path) for path in repo.dirs),
            by_basename=MappingProxyType({name: tuple(paths) for name, paths in by_basename.items()}),
            markdown_in_dir=MappingProxyType(
                {directory: MappingProxyType(names) for directory, names in markdown_in_dir.items()}
            ),
            pages=pages,
            pages_available=page_set.available,
            backend_roots=backend_roots,
            gate_roots=gate_roots,
            by_name=MappingProxyType({name: tuple(rows) for name, rows in by_name.items()}),
            python_classes=python_classes,
            package_bindings=MappingProxyType(dict(package_bindings)),
        )

    def dir_landing(self, directory: str) -> str | None:
        """A directory's README.md, else its index.md (case-insensitive)."""
        names = self.markdown_in_dir.get(directory, {})
        for name in _INDEX_NAMES:
            if name in names:
                return names[name]
        return None


def _roots(segment_lists: Iterable[Sequence[str]], prefixes: Sequence[str]) -> frozenset[str]:
    return frozenset(root for segments in segment_lists if (root := route_root(segments, prefixes)) is not None)


def mark_missing(
    ctx: ResolutionContext, draft: Draft, tries: Sequence[str], detail: str, *, example_of: str | None = None
) -> Draft:
    """A path that is not git-visible: inside an uninitialized submodule
    (``not_checked``), local-only (a SKIP_DIRS segment, or ignored by git,
    settled in one batch by the report), an example name, or ``missing``."""
    for path in tries:
        sub = ctx.repo.uninitialized_submodule_of(path)
        if sub is not None:
            return draft.set("not_checked", f"submodule {sub} is not checked out", checked=False)
    if any(has_skip_dir(path) for path in tries):
        return draft.set("local_only", "local-only directory, never in git")
    if example_of is not None and is_example_path(example_of):
        return draft.set("skipped", "example name", checked=False)
    draft.pending = tuple(tries)
    return draft.set("missing", detail)


# ── Links ────────────────────────────────────────────────────────────────


class LinkResolver:
    """Links and anchors, and the Markdown file a link reaches (for
    reachability and inbound links)."""

    def __init__(self, ctx: ResolutionContext, facts: FactsReader) -> None:
        self.ctx = ctx
        self.facts = facts

    def _resolve_href(self, doc: str, path_part: str) -> str | None:
        """The repository path a link reaches; ``""`` is the repository root
        (``/``, or ``../`` from a top-level directory), None when it escapes."""
        joined = posixpath.join("" if path_part.startswith("/") else posixpath.dirname(doc), path_part.lstrip("/"))
        if posixpath.normpath(joined) == ".":
            return ""
        return normalize_repo_path(joined)

    def _is_dir(self, target: str) -> bool:
        return target == "" or target in self.ctx.repo.dirs

    def link(self, doc: str, link: Link) -> Draft:
        href = link.href
        text = unquote(href)
        draft = Draft(doc, link.line, "link", text, link.context, raw_text=href if href != text else None)
        if not href:
            # `![alt]()` / `[x]()`: GitHub renders it against the page itself;
            # imported vendor pages use it for images that were never copied.
            return draft.set("skipped", "empty link target", checked=False)
        if is_external_href(href):
            return draft.set("skipped", "external link", checked=False)
        raw_path, has_fragment, fragment = href.partition("#")
        path_part = unquote(raw_path.split("?", 1)[0])
        if not path_part:
            # `#heading` or `?plain=1`: the page itself, which exists; only a
            # fragment, if written, needs checking.
            draft.target = doc
            return self.anchor(draft, doc, fragment) if has_fragment else draft
        target = self._resolve_href(doc, path_part)
        if target is None:
            return draft.set("missing", "points outside the repository")
        draft.target = target
        is_dir = self._is_dir(target)
        if not (is_dir or (target in self.ctx.repo.files and not path_part.endswith("/"))):
            return mark_missing(self.ctx, draft, [target], f"{target} does not exist")
        if has_fragment and is_dir:
            # A directory link lands on its README (as reachability reads it),
            # so its fragment must be a heading there.
            landing = self.ctx.dir_landing(target)
            if landing is None:
                return draft.set("not_checked", "no README.md or index.md to check the anchor against", checked=False)
            return self.anchor(draft, landing, fragment)
        if has_fragment and is_markdown(target):
            return self.anchor(draft, target, fragment)
        return draft

    def anchor(self, draft: Draft, target: str, fragment: str) -> Draft:
        if not fragment:
            return draft.set("anchor_missing", "empty anchor")
        facts = self.facts(target)
        if facts is None:
            return draft.set("not_checked", f"{target} could not be read", checked=False)
        if not match_anchor(fragment, facts.anchors):
            return draft.set("anchor_missing", f"no heading or id in {target}")
        return draft

    def markdown_target(self, doc: str, link: Link) -> str | None:
        """The Markdown file a link reaches: the file itself, or a linked
        directory's README.md, else its index.md (case-insensitive)."""
        href = link.href
        if not href or is_external_href(href):
            return None
        path_part = unquote(href.partition("#")[0].split("?", 1)[0])
        if not path_part:
            return None
        target = self._resolve_href(doc, path_part)
        if target is None:
            return None
        if target in self.ctx.repo.files:
            return target if is_markdown(target) else None
        if self._is_dir(target):
            return self.ctx.dir_landing(target)
        return None


# ── Paths ────────────────────────────────────────────────────────────────


class PathResolver:
    """Repository paths in a code span, a span with spaces (a command) or a
    fence line."""

    def __init__(self, ctx: ResolutionContext) -> None:
        self.ctx = ctx

    def paths(self, doc: str, text: str, form: PathForm, line: int, context: str) -> list[Draft]:
        out: list[Draft] = []
        for token in path_tokens(text, form):
            draft = self.path(doc, token, form, line, context)
            if draft is not None:
                out.append(draft)
        return out

    def classify(self, doc: str, path: str, relative: bool, form: PathForm) -> tuple[str | None, list[str]]:
        """``(class, base directories)``; class None means "not a path to check here".

        A doc's own directory is the last base for the relative classes: a
        README describing its tool names ``tests/x.py`` relative to itself.
        """
        doc_dir = posixpath.dirname(doc)
        first = path.split("/", 1)[0]
        if first == "..":
            return ("doc-rel", [doc_dir]) if form != "fence" else (None, [])
        if relative:
            # `./x` in a whole span is the doc's sibling; failing that, it is
            # usually written from the repository or a code root (`./infra/…`).
            # Only a whole span is relative: in a command or a fence `./x` is
            # relative to the shell's directory, so `path` never asks.
            _, fallback = self.classify(doc, path, False, form) if "/" in path else (None, [])
            return "doc-rel", [doc_dir, *(base for base in fallback if base != doc_dir)]
        if "/" not in path:
            return None, []
        bases: list[str] = []
        cls: str | None = None
        if first in self.ctx.top:
            cls = "root"
            bases.append("")
        for path_root in self.ctx.config.path_roots:
            if first in self.ctx.children[path_root.prefix]:
                bases.append(path_root.prefix)
                cls = cls or path_root.name
        if cls == "root":
            return cls, bases
        if form == "fence":
            return None, []
        if cls is not None:
            return cls, [*bases, doc_dir]
        if not has_known_extension(path):
            return None, []
        return ("other" if first in self.ctx.dir_names else "unknown"), [doc_dir]

    def path(self, doc: str, token: str, form: PathForm, line: int, context: str) -> Draft | None:
        # In a command, `./x` is relative to the shell's directory, not the doc.
        relative = form == "span" and token.lstrip("\"'`([").startswith("./")
        parsed = split_path_token(token)
        if parsed is None:
            return None
        path = parsed.path
        cls, bases = self.classify(doc, path, relative, form)
        if cls is None:
            return None
        draft = Draft(doc, line, "path", parsed.text, context, target=path)
        if is_placeholder_path(path):
            return draft.set("skipped", "placeholder", checked=False)
        if cls == "unknown":
            return draft.set("skipped", "its first directory is not in the repository", checked=False)
        for expansion in expand_braces(path):
            found, tries = self.resolve_in(bases, expansion)
            if found is None and cls == "other":
                if self._other_path(draft, expansion, path).status != "ok":
                    return draft
                continue
            if found is None:
                draft.target = tries[0] if tries else expansion
                where = (
                    f"{tries[0]} does not exist"
                    if cls == "doc-rel" and len(tries) == 1
                    else "not found under " + _join_or([b + "/" if b else "the repository root" for b in _bases(bases)])
                )
                if has_glob(expansion):
                    where = "the pattern matches no file"
                return mark_missing(self.ctx, draft, tries, where, example_of=path)
            if draft.target == path:
                draft.target = found
        return draft

    def resolve_in(self, bases: Sequence[str], path: str) -> tuple[str | None, list[str]]:
        """The repository path ``path`` resolves to under one of ``bases``, and every path tried."""
        repo = self.ctx.repo
        tries: list[str] = []
        want_dir = path.endswith("/")
        for base in bases:
            full = normalize_repo_path(posixpath.join(base, path))
            if full is None:
                continue
            tries.append(full)
            if has_glob(full):
                pattern = glob_regex(full)
                hits = sorted(p for p in (repo.files | repo.dirs) if pattern.fullmatch(p))
                if hits:
                    return hits[0], tries
                continue
            if want_dir:
                if full in repo.dirs:
                    return full, tries
                continue
            for variant in path_variants(full):
                if repo.exists(variant):
                    return variant, tries
        return None, tries

    def _other_path(self, draft: Draft, expansion: str, path: str) -> Draft:
        """A path whose first directory exists somewhere in the repository:
        any file whose path ends with it counts."""
        if has_glob(expansion):
            pattern = glob_regex("**/" + expansion)
            hits = sorted(p for p in self.ctx.repo.files if pattern.fullmatch(p))
        else:
            name = posixpath.basename(expansion.rstrip("/"))
            hits = sorted(
                p for p in self.ctx.by_basename.get(name, ()) if p == expansion or p.endswith("/" + expansion)
            )
        if hits:
            if draft.target == path:
                draft.target = hits[0]
            return draft
        for sub in sorted(self.ctx.repo.uninitialized_submodules):
            if _ends_inside(expansion, sub):
                return draft.set("not_checked", f"submodule {sub} is not checked out", checked=False)
        draft.target = expansion
        return mark_missing(self.ctx, draft, [], f"no repository file path ends with {expansion}", example_of=path)


def _ends_inside(path: str, sub: str) -> bool:
    """Whether a file under ``sub`` could end with ``path``: ``path`` starts
    with a trailing part of ``sub`` (``core_security/x.py`` or
    ``packages/core_security`` for ``backend/packages/core_security``), whole
    segments only."""
    parts = sub.split("/")
    return any(is_under(path, "/".join(parts[start:])) for start in range(len(parts)))


def _bases(bases: Sequence[str]) -> list[str]:
    return [base.rstrip("/") for base in dict.fromkeys(bases)]


def _join_or(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


# ── Routes ───────────────────────────────────────────────────────────────


class _RouteStage(NamedTuple):
    """One step of route matching: whether it applies to this mention, the
    rows it searches, the test a row must pass against one path variant, and
    the status it gives with its detail (from the rows that fit)."""

    applies: bool
    rows: Sequence[RouteRow]
    fits: Callable[[tuple[str, ...], RouteRow], bool]
    status: str
    detail: Callable[[Sequence[RouteRow]], str | None]


def _route_hit(draft: Draft, rows: Sequence[RouteRow], status: str, detail: str | None) -> Draft:
    ordered = sorted(rows, key=lambda r: (r.path, r.method, r.file, r.line))
    draft.target = ordered[0].display
    draft.defined_in = tuple(dict.fromkeys(f"{r.file}:{r.line}" for r in ordered))
    return draft.set(status, detail)


def _route_stages(draft: Draft, variants: Sequence[tuple[str, ...]], stages: Sequence[_RouteStage]) -> Draft | None:
    """The first stage, in order, with rows that fit one of the path's
    variants, tried in order; None when no stage applies."""
    for stage in stages:
        if not stage.applies:
            continue
        for variant in variants:
            rows = [row for row in stage.rows if stage.fits(variant, row)]
            if rows:
                return _route_hit(draft, rows, stage.status, stage.detail(rows))
    return None


def _served(rows: Sequence[RouteRow]) -> str:
    return "served: " + ", ".join(sorted({r.method for r in rows}))


_AMBIGUOUS_SHOWN = 5


def _ambiguous_tail(draft: Draft, tail: str, noun: str, names: Sequence[str], locations: Sequence[str]) -> Draft:
    """A ``.../tail`` several routes or pages end with: it fails, naming the
    first few, because it cannot say which one the doc means."""
    draft.defined_in = tuple(dict.fromkeys(locations))
    shown = ", ".join(names[:_AMBIGUOUS_SHOWN])
    if len(names) > _AMBIGUOUS_SHOWN:
        shown += f" and {len(names) - _AMBIGUOUS_SHOWN} more"
    return draft.set("ambiguous", f"{len(names)} {noun} end with {tail}: {shown}; write the full path")


def _unmounted(rows: Sequence[RouteRow]) -> str:
    first = min(rows, key=lambda r: (r.file, r.line))
    return f"{first.handler} at {first.file}:{first.line} is not mounted on the app"


def _page_hit(
    draft: Draft,
    pages: Sequence[FrontendPage],
    status: str = "frontend",
    detail: str = "a frontend page, route handler or public file",
) -> Draft:
    page = min(pages, key=lambda p: (p.file, p.line))
    draft.target = page.display
    draft.defined_in = (f"{page.file}:{page.line}",)
    return draft.set(status, detail)


class RouteResolver:
    """Route mentions against the live routes, the unmounted ones and the
    frontend pages. ``hint`` ends a ``missing`` detail (it points at the
    route limitations when there are any). ``declared_roots`` are the roots
    the route-checked docs name with an un-gated ``METHOD /path``
    (``declared_root``): they pass the bare-path gate with the context's own,
    so a root keeps being checked after its last live route is deleted."""

    def __init__(self, ctx: ResolutionContext, hint: str = "", declared_roots: frozenset[str] = frozenset()) -> None:
        self.ctx = ctx
        self.hint = hint
        self.gate_roots = ctx.gate_roots | declared_roots

    def _root(self, path: str) -> str | None:
        return route_root(mention_segments(path), self.ctx.config.route_prefixes)

    def _missing(self, draft: Draft, method: str | None, detail: str) -> Draft:
        """``missing``, unless the frontend pages could not be read and a page
        might be what the doc names (only a mention without a method can be one)."""
        if method is None and not self.ctx.pages_available:
            return draft.set("not_checked", "frontend pages could not be read", checked=False)
        return draft.set("missing", detail)

    def route(self, doc: str, mention: RouteMention, line: int, context: str, *, bare: bool) -> Draft:
        draft = Draft(doc, line, "route", mention.text, context, route=(mention.method, mention.path))
        tail = route_tail(mention.path)
        if has_route_placeholder(mention.path if tail is None else tail):
            return draft.set("skipped", "placeholder", checked=False)
        # A tail has no route root, so a gated one never passes the gate.
        if (bare or mention.gated) and (tail is not None or self._root(mention.path) not in self.gate_roots):
            return draft.set("skipped", "not under a backend or frontend route root", checked=False)
        if tail is not None:
            return self.match_tail(draft, mention.method, tail)
        return self.match(draft, mention.method, mention.path)

    @staticmethod
    def declared_root(mention: RouteMention, *, bare: bool, prefixes: Sequence[str]) -> str | None:
        """The root a mention declares an API root: one checked without the
        gate (a ``METHOD /path`` with an upper-case or canonical method, not
        a ``.../tail`` and not a placeholder), else None."""
        if bare or mention.gated or route_tail(mention.path) is not None or has_route_placeholder(mention.path):
            return None
        return route_root(mention_segments(mention.path), prefixes)

    def match(self, draft: Draft, method: str | None, path: str) -> Draft:
        ctx = self.ctx
        segments = mention_segments(path)
        prefixes = ctx.config.route_prefixes
        written = [mention_segments(variant) for variant in (path, *omitted_prefix_variants(path, prefixes))]
        added = [mention_segments(variant) for variant in added_prefix_variants(path, prefixes)]

        def fits(variant: tuple[str, ...], row: RouteRow) -> bool:
            return methods_compatible(method, row.method) and match_segments(variant, row.segments) == "exact"

        def any_method(variant: tuple[str, ...], row: RouteRow) -> bool:
            return match_segments(variant, row.segments) == "exact"

        def weak(variant: tuple[str, ...], row: RouteRow) -> bool:
            return match_segments(variant, row.segments) == "weak"

        def prefix(variant: tuple[str, ...], row: RouteRow) -> bool:
            return methods_compatible(method, row.method) and strict_prefix(variant, row.segments)

        # A bare path may name the start of routes; a trailing wildcard says
        # so explicitly, also with a method (`GET /appointments/*`).
        wild = bool(segments) and segments[-1] == WILD
        exact_stages = (
            _RouteStage(True, ctx.routes, fits, "ok", lambda rows: None),
            _RouteStage(method is not None, ctx.routes, any_method, "method_differs", _served),
        )
        found = _route_stages(draft, written, exact_stages)
        if found is not None:
            return found
        # A page named exactly beats a backend parameter filled with a word:
        # `/articles/compare` with its own page.tsx is not `/articles/{article_id}`.
        # It also beats a route that only fits once a prefix is added: with a
        # `/api` prefix, the page `/assets` is not the route `/api/assets`.
        if method is None:
            pages = [p for p in ctx.pages if match_segments(segments, p.segments) == "exact"]
            if pages:
                return _page_hit(draft, pages)
        # Before the weak stage: FastAPI serves `/docs` itself even when a
        # top-level `/{id}` route would take the word.
        if "/" + "/".join(segments) in FRAMEWORK_PATHS:
            return draft.set("framework", "served by FastAPI itself")
        found = _route_stages(draft, added, exact_stages)
        if found is not None:
            return found
        variants = [*written, *added]
        found = _route_stages(
            draft,
            variants,
            (
                _RouteStage(
                    True,
                    ctx.routes,
                    weak,
                    "weak_param",
                    lambda rows: f"only {' or '.join(sorted({r.path for r in rows}))} fits, by a word in a parameter",
                ),
                _RouteStage(True, ctx.unmounted, fits, "unmounted", _unmounted),
                _RouteStage(
                    method is None or wild, ctx.routes, prefix, "prefix", lambda rows: "the start of a live route"
                ),
            ),
        )
        if found is not None:
            return found
        if route_root(segments, prefixes) not in ctx.backend_roots:
            rows = [
                r for r in ctx.routes if methods_compatible(method, r.method) and strict_suffix(segments, r.segments)
            ]
            if rows:
                return _route_hit(draft, rows, "suffix", "the end of a live route")
        if method is None:
            pages = [p for p in ctx.pages if strict_prefix(segments, p.segments)]
            if pages:
                return _page_hit(draft, pages)
            # Still a failure, as for a backend route, but one that names the
            # page whose parameter took the word.
            pages = [p for p in ctx.pages if match_segments(segments, p.segments) == "weak"]
            if pages:
                displays = " or ".join(sorted({p.display for p in pages}))
                return _page_hit(draft, pages, "weak_param", f"only the page {displays} fits, by a word in a parameter")
        return self._missing(draft, method, "not in the analyzed backend routes" + self.hint)

    def match_tail(self, draft: Draft, method: str | None, tail: str) -> Draft:
        """A ``.../tail`` mention: the stages of ``match`` that a tail can
        meet, each fitting the end of a route instead of the whole of it."""
        ctx = self.ctx
        segments = mention_segments(tail)

        def fits(variant: tuple[str, ...], row: RouteRow) -> bool:
            return methods_compatible(method, row.method) and match_suffix(variant, row.segments) == "exact"

        def any_method(variant: tuple[str, ...], row: RouteRow) -> bool:
            return match_suffix(variant, row.segments) == "exact"

        # Unlike a whole path, a weak fit keeps the method: nearly every tail
        # would otherwise end some route of another method in a parameter.
        def weak(variant: tuple[str, ...], row: RouteRow) -> bool:
            return methods_compatible(method, row.method) and match_suffix(variant, row.segments) == "weak"

        # A tail names one route only while one path ends with it: once two
        # do (`.../approve`), it would keep passing after either is removed.
        rows = [row for row in ctx.routes if fits(segments, row)]
        if len({row.path for row in rows}) > 1:
            ordered = sorted(rows, key=lambda r: (r.path, r.method, r.file, r.line))
            names = list(dict.fromkeys(r.display for r in ordered))
            return _ambiguous_tail(draft, tail, "live routes", names, [f"{r.file}:{r.line}" for r in ordered])
        if rows:
            return _route_hit(draft, rows, "suffix", "the end of a live route")
        found = _route_stages(
            draft,
            [segments],
            (_RouteStage(method is not None, ctx.routes, any_method, "method_differs", _served),),
        )
        if found is not None:
            return found
        if method is None:
            pages = [p for p in ctx.pages if match_suffix(segments, p.segments) == "exact"]
            displays = sorted({p.display for p in pages})
            if len(displays) > 1:
                locations = sorted(f"{p.file}:1" for p in pages)
                return _ambiguous_tail(draft, tail, "frontend pages", displays, locations)
            if pages:
                return _page_hit(draft, pages, detail="the end of a frontend page, route handler or public file")
        found = _route_stages(
            draft,
            [segments],
            (
                _RouteStage(
                    True,
                    ctx.routes,
                    weak,
                    "weak_param",
                    lambda rows: (
                        f"only {' or '.join(sorted({r.path for r in rows}))} ends that way, by a word in a parameter"
                    ),
                ),
                _RouteStage(True, ctx.unmounted, fits, "unmounted", _unmounted),
            ),
        )
        if found is not None:
            return found
        if method is None:
            pages = [p for p in ctx.pages if match_suffix(segments, p.segments) == "weak"]
            if pages:
                displays = " or ".join(sorted({p.display for p in pages}))
                return _page_hit(
                    draft, pages, "weak_param", f"only the page {displays} ends that way, by a word in a parameter"
                )
        return self._missing(draft, method, f"no live route ends with {tail}" + self.hint)


# ── Symbols ──────────────────────────────────────────────────────────────


class SymbolResolver:
    """Symbol mentions against the symbol index, then the identifier words of
    tracked code (read through ``words`` only when the index has no answer)."""

    def __init__(self, ctx: ResolutionContext, words: WordsReader) -> None:
        self.ctx = ctx
        self.words = words

    def symbol(self, doc: str, mention: SymbolMention, line: int, context: str) -> Draft:
        tier = symbol_tier(mention, self.ctx.python_classes)
        draft = Draft(doc, line, "symbol", mention.text, context, target=mention.name, checked=tier == "A")
        parts = mention.parts
        if len(parts) == 1:
            definitions = list(self.ctx.by_name.get(mention.name, ()))
        else:
            definitions = [
                row
                for row in self.ctx.by_name.get(parts[-1], ())
                if qualifiers_match(
                    row.container,
                    row.file,
                    parts[:-1],
                    name=row.name,
                    scope=row.scope,
                    bindings=self.ctx.package_bindings,
                )
            ]
        if definitions:
            ordered = sorted(definitions, key=lambda r: (r.file, r.line))
            draft.defined_in = tuple(dict.fromkeys(row.location for row in ordered))
            return draft.set("ok", "defined in the symbol index")
        words = self.words()
        used = (
            mention.name in words.words or mention.name in BUILTIN_WORDS
            if len(parts) == 1
            else mention.name in words.dotted
        )
        if used:
            return draft.set("ok", "used in tracked code")
        if len(parts) == 1 and is_upper_fragment(mention.name) and any(w.endswith(mention.name) for w in words.words):
            return draft.set("ok", "the end of a name used in tracked code")
        if is_example_symbol(mention):
            return draft.set("skipped", "placeholder or example name", checked=False)
        if tier == "B":
            return draft.set("unresolved", "no definition in the symbol index and no use in tracked code")
        # No submodule exemption: backend code imports and names what it uses
        # from a submodule, so a name the docs share with it resolves through
        # tracked code whether or not the submodule is checked out.
        return draft.set("missing", "no definition in the symbol index and no use in tracked code")
