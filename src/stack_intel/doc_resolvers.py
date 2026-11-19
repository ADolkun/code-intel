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

from stack_intel.code_paths import is_under
from stack_intel.code_words import BUILTIN_WORDS, CodeWords
from stack_intel.definition_files import Unreadable
from stack_intel.doc_commands import PACKAGE_MANIFEST, PACKAGE_RUNNERS, CommandMention
from stack_intel.doc_report import Reference, RouteRow, SymbolRow
from stack_intel.doc_rules import (
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
from stack_intel.make_targets import MAKEFILE_NAMES, MakeTargets, strip_dot_slash
from stack_intel.markdown_facts import DocFacts, Link, match_anchor
from stack_intel.mise_tasks import MiseFile, config_file, config_owner, file_task_names, task_dir_entry
from stack_intel.repo_files import RepoFiles

_INDEX_NAMES: tuple[str, ...] = ("readme.md", "index.md")
# `Path.exists` raises ValueError for a NUL byte in the name.
_EXISTS_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)

FactsReader = Callable[[str], "DocFacts | None"]
WordsReader = Callable[[], CodeWords]
ScriptsReader = Callable[[str], "frozenset[str] | Unreadable"]
MakeReader = Callable[[str], "MakeTargets | Unreadable"]
MiseReader = Callable[[str], "MiseFile | Unreadable"]
# Whether a directory's tracked .npmrc sets if-present.
NpmrcReader = Callable[[str], "bool | Unreadable"]


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
    # Directories holding a tracked package.json.
    manifest_dirs: frozenset[str]
    # Directory → the tracked makefile GNU make reads there.
    makefiles: Mapping[str, str]
    # Directory → the tracked mise config files that configure it.
    mise_configs: Mapping[str, tuple[str, ...]]
    # Directory → the tracked files under its default mise task directories.
    mise_task_files: Mapping[str, tuple[str, ...]]
    # Directory → the names of the git-visible files directly in it.
    files_by_dir: Mapping[str, tuple[str, ...]]

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
        definitions = _definition_files(repo)
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
            manifest_dirs=frozenset(
                posixpath.dirname(path) for path in repo.tracked if posixpath.basename(path) == PACKAGE_MANIFEST
            ),
            makefiles=definitions.makefiles,
            mise_configs=definitions.mise_configs,
            mise_task_files=definitions.mise_task_files,
            files_by_dir=definitions.files_by_dir,
        )

    def dir_landing(self, directory: str) -> str | None:
        """A directory's README.md, else its index.md (case-insensitive)."""
        names = self.markdown_in_dir.get(directory, {})
        for name in _INDEX_NAMES:
            if name in names:
                return names[name]
        return None


class _DefinitionFiles(NamedTuple):
    makefiles: Mapping[str, str]
    mise_configs: Mapping[str, tuple[str, ...]]
    mise_task_files: Mapping[str, tuple[str, ...]]
    files_by_dir: Mapping[str, tuple[str, ...]]


def _definition_files(repo: RepoFiles) -> _DefinitionFiles:
    """The makefile and mise indexes of a ``ResolutionContext``."""
    makefiles: dict[str, str] = {}
    configs: dict[str, list[str]] = {}
    task_files: dict[str, list[str]] = {}
    by_dir: dict[str, list[str]] = {}
    for path in sorted(repo.files):
        directory, name = posixpath.split(path)
        by_dir.setdefault(directory, []).append(name)
    # Tracked files count wherever they are: a SKIP_DIRS name such as `dist` is
    # local-only only when git does not track it.
    for path in sorted(repo.tracked):
        directory, name = posixpath.split(path)
        if name in MAKEFILE_NAMES:
            current = makefiles.get(directory)
            if current is None or MAKEFILE_NAMES.index(name) < MAKEFILE_NAMES.index(posixpath.basename(current)):
                makefiles[directory] = path
        if (owner := config_owner(path)) is not None:
            configs.setdefault(owner, []).append(path)
        elif (entry := task_dir_entry(path)) is not None:
            task_files.setdefault(entry[0], []).append(path)
    return _DefinitionFiles(
        MappingProxyType(makefiles),
        MappingProxyType({key: tuple(value) for key, value in configs.items()}),
        MappingProxyType({key: tuple(value) for key, value in task_files.items()}),
        MappingProxyType({key: tuple(value) for key, value in by_dir.items()}),
    )


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
        expansions = expand_braces(path)
        if expansions is None:
            return draft.set("skipped", "a brace pattern with too many alternatives", checked=False)
        for expansion in expansions:
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
    so a root keeps being checked after its last live route is deleted.

    Without ``[backend]`` (``DocRefsConfig.backend_configured``) the routes
    are unknown, so only a page match resolves; every other verdict, failing
    or not, would be a guess about a backend that lives elsewhere and becomes
    ``not_checked``."""

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
            draft = self.match_tail(draft, mention.method, tail)
        else:
            draft = self.match(draft, mention.method, mention.path)
        if not self.ctx.config.backend_configured and draft.status != "frontend":
            draft.target, draft.defined_in = None, ()
            return draft.set("not_checked", "no [backend] table", checked=False)
        return draft

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


_NO_DIRECTORY = "the doc names no directory"
# Names docs write for "your script here"; checked only after the lookup, so a
# defined one still resolves.
_PLACEHOLDER_NAMES = frozenset({"target", "task", "script", "script-name", "task-name", "target-name"})
_NEEDS_MONOREPO = "a {form} name needs a monorepo root (monorepo_root = true in the root mise config)"


def _where(directory: str) -> str:
    return directory or "the repository root"


def _chain(directory: str) -> list[str]:
    """``a/b`` → ``["", "a", "a/b"]``: the directories mise reads config from."""
    parts = directory.split("/") if directory else []
    return ["", *("/".join(parts[: end + 1]) for end in range(len(parts)))]


@dataclass
class _Tasks:
    """The mise tasks some directories' files define. ``maybe``: names a file
    task may answer to; ``env_only``: names only an environment config the
    command does not select defines; ``unreadable`` and ``opaque``: why the
    set may be incomplete."""

    names: dict[str, str] = field(default_factory=dict)
    maybe: dict[str, str] = field(default_factory=dict)
    env_only: dict[str, str] = field(default_factory=dict)
    configs: list[str] = field(default_factory=list)
    unreadable: list[Unreadable] = field(default_factory=list)
    opaque: list[str] = field(default_factory=list)

    def add(self, names: Iterable[str], rel: str) -> None:
        for name in names:
            self.names.setdefault(name, rel)

    def merge(self, other: _Tasks) -> None:
        for mine, theirs in ((self.names, other.names), (self.maybe, other.maybe), (self.env_only, other.env_only)):
            for name, rel in theirs.items():
                mine.setdefault(name, rel)
        self.configs += other.configs
        self.unreadable += other.unreadable
        self.opaque += other.opaque


@dataclass(frozen=True)
class _Monorepo:
    """A root mise config with ``monorepo_root``: ``config_roots`` are its
    patterns, None when mise finds the roots itself (every directory with mise
    files); ``aliases`` its ``[monorepo.path_aliases]``."""

    config_roots: tuple[str, ...] | None
    aliases: Mapping[str, str]


@dataclass(frozen=True)
class _Lookup:
    """One way mise may read a task name: ``task`` in ``tasks``; ``missing``
    says where it was looked for."""

    tasks: _Tasks
    task: str
    missing: str


class CommandResolver:
    """A script, make target or mise task a doc runs, against the definition
    files of its runner, read through ``scripts`` (a directory's package.json
    scripts), ``make`` (a makefile's targets) and ``mise`` (a mise file's tasks);
    each returns ``Unreadable`` when the file cannot be read or parsed, which
    makes the mentions that depend on it ``not_checked``.

    A named directory is taken both from the repository root and from the doc's
    directory, and checked in its own definition files. Without one the name may
    be defined in the repository root's, the doc's own directory's or (scripts
    only) any package.json under the frontend root: ``ok`` when one of those
    defines it, ``not_checked`` when only another file does, and ``missing``
    only when no tracked file of the runner defines it and every one of them
    could be read and holds a provable set, and the runner has a file where the
    doc runs it. Nothing outside the repository and no environment variable
    counts. A repository with no file of the runner gives no reference. An
    ``unproven`` mention and a placeholder name never end ``missing``.

    mise names follow mise 2026.11: without a monorepo root a name is looked up
    in the configs from the root down to where it runs; with one, a name is
    looked up in the nearest directory with a mise config only, ``//p:t`` in
    config root ``p``'s own files, and ``p:t`` also as ``//p:t`` from the
    repository root.
    """

    def __init__(
        self,
        ctx: ResolutionContext,
        scripts: ScriptsReader,
        make: MakeReader,
        mise: MiseReader,
        npmrc: NpmrcReader,
    ) -> None:
        self.ctx = ctx
        self.scripts = scripts
        self.make = make
        self.mise = mise
        self.npmrc = npmrc
        self._tasks: dict[tuple[str, frozenset[str]], _Tasks] = {}
        self._folded: dict[str, str] | None = None

    def command(self, doc: str, mention: CommandMention, line: int, context: str) -> Draft | None:
        if mention.runner in PACKAGE_RUNNERS:
            if not self.ctx.manifest_dirs:
                return None
        elif mention.runner == "make":
            if not self.ctx.makefiles:
                return None
        elif not self.ctx.mise_configs and not self.ctx.mise_task_files:
            return None
        draft = Draft(doc, line, "command", mention.text, context)
        if mention.verdict == "skipped":
            return draft.set("skipped", mention.reason, checked=False)
        if mention.verdict == "unchecked":
            return draft.set("not_checked", mention.reason, checked=False)
        if mention.runner in PACKAGE_RUNNERS:
            return self._script(draft, mention)
        if mention.runner == "make":
            return self._target(draft, mention)
        return self._task(draft, mention)

    def _near(self, doc: str) -> list[str]:
        """Where an unqualified command may run: the repository root and the doc's directory."""
        directory = posixpath.dirname(doc)
        return ["", directory] if directory else [""]

    def _named_dirs(self, doc: str, directory: str) -> list[str]:
        """Where a named directory may be: under the repository root, or under
        the doc's directory when the doc's commands run from there."""
        out = [directory]
        base = posixpath.dirname(doc)
        if base:
            joined = posixpath.normpath(posixpath.join(base, directory)) if directory else base
            if joined not in out:
                out.append(joined)
        return out

    def _unproven(self, draft: Draft, mention: CommandMention, name: str, missing: str) -> Draft:
        if mention.verdict == "unproven":
            return draft.set("not_checked", mention.reason, checked=False)
        if any(ch.isalpha() for ch in name) and name.upper() == name:
            return draft.set("skipped", "an ALL-CAPS placeholder", checked=False)
        if name in _PLACEHOLDER_NAMES or name.startswith("your-") or is_example_path(name):
            return draft.set("skipped", "an example name", checked=False)
        return draft.set("missing", missing)

    def _script(self, draft: Draft, mention: CommandMention) -> Draft:
        dirs = self.ctx.manifest_dirs
        name = mention.name
        if mention.directory is not None:
            candidates = searched = [d for d in self._named_dirs(draft.doc, mention.directory) if d in dirs]
            if not candidates:
                return draft.set("skipped", f"no package.json in {_where(mention.directory)}", checked=False)
        else:
            root = self.ctx.config.frontend_root
            near = self._near(draft.doc)
            # A package.json vendored under the frontend (node_modules) is not
            # one the doc runs, though a doc may still name its directory.
            candidates = sorted(
                d
                for d in dirs
                if d in near or (root is not None and (root == "." or is_under(d, root)) and not has_skip_dir(d))
            )
            searched = sorted(dirs)
        found = {d: self.scripts(d) for d in searched}
        defining = [d for d in searched if not isinstance(names := found[d], Unreadable) and name in names]
        if defining:
            near_defining = [d for d in defining if d in candidates]
            draft.target = posixpath.join((near_defining or defining)[0], PACKAGE_MANIFEST)
            if near_defining:
                return draft.set("ok", f"defined in {draft.target}")
            files = [posixpath.join(d, PACKAGE_MANIFEST) for d in defining]
            return draft.set("not_checked", f"defined only in {_join_or(files)}; {_NO_DIRECTORY}", checked=False)
        if (failed := next((v for v in found.values() if isinstance(v, Unreadable)), None)) is not None:
            return draft.set("not_checked", failed.reason, checked=False)
        if not candidates:
            return draft.set("not_checked", "no package.json where the doc runs it", checked=False)
        if mention.verdict != "unproven" and (default := self._default_script(mention, searched)) is not None:
            return draft.set("not_checked", default, checked=False)
        files = [posixpath.join(d, PACKAGE_MANIFEST) for d in searched]
        return self._unproven(draft, mention, name, f"{_join_or(files)} defines no script {name!r}")

    def _default_script(self, mention: CommandMention, dirs: Sequence[str]) -> str | None:
        """Why the run may pass without the script: npm supplies ``start``
        beside a ``server.js`` and ``install`` beside a ``*.gyp`` file, and a
        tracked ``.npmrc`` may set ``if-present``."""
        for directory in dirs:
            names = self.ctx.files_by_dir.get(directory, ())
            if mention.name == "start" and "server.js" in names:
                return f"runs node server.js from {_where(directory)} when package.json has no start script"
            if mention.name == "install" and (gyp := next((n for n in names if n.endswith(".gyp")), None)):
                path = posixpath.join(directory, gyp)
                return f"runs node-gyp rebuild for {path} when package.json has no install script"
        if mention.runner in ("npm", "pnpm"):
            for directory in sorted({"", *dirs}):
                rc = posixpath.join(directory, ".npmrc")
                if rc not in self.ctx.repo.tracked:
                    continue
                sets = self.npmrc(directory)
                if isinstance(sets, Unreadable):
                    return sets.reason
                if sets:
                    return f"{rc} sets if-present, so a missing script passes"
        return None

    def _target(self, draft: Draft, mention: CommandMention) -> Draft:
        makefiles = self.ctx.makefiles
        name = strip_dot_slash(mention.name)
        if not name.strip("/"):
            return draft.set("not_checked", "an empty goal names no target", checked=False)
        if mention.directory is not None:
            candidates = searched = [d for d in self._named_dirs(draft.doc, mention.directory) if d in makefiles]
            if not candidates:
                return draft.set("skipped", f"no makefile in {_where(mention.directory)}", checked=False)
            places = candidates
        else:
            near = self._near(draft.doc)
            candidates = [d for d in near if d in makefiles]
            searched = sorted(makefiles)
            places = sorted({*searched, *near})
        found = {d: self.make(makefiles[d]) for d in searched}
        defining = [d for d in searched if not isinstance(targets := found[d], Unreadable) and name in targets.targets]
        if defining:
            near_defining = [d for d in defining if d in candidates]
            draft.target = makefiles[(near_defining or defining)[0]]
            if near_defining:
                return draft.set("ok", f"defined in {draft.target}")
            files = [makefiles[d] for d in defining]
            return draft.set("not_checked", f"defined only in {_join_or(files)}; {_NO_DIRECTORY}", checked=False)
        for directory, targets in found.items():
            rel = makefiles[directory]
            if isinstance(targets, Unreadable):
                return draft.set("not_checked", targets.reason, checked=False)
            if targets.opaque is not None:
                return draft.set(
                    "not_checked", f"{rel} {targets.opaque}, so its targets are not all known", checked=False
                )
            if mention.overrides and targets.variables:
                detail = f"the command sets {sorted(mention.overrides)[0]}, and {rel} names targets with variables"
                return draft.set("not_checked", detail, checked=False)
        if not candidates:
            return draft.set("not_checked", "no makefile where the doc runs it", checked=False)
        for directory in places:
            if (reason := self._buildable(directory, name)) is not None:
                return draft.set("not_checked", reason, checked=False)
        rels = [makefiles[d] for d in searched]
        return self._unproven(draft, mention, name, f"{_join_or(rels)} defines no target {name!r}")

    def _buildable(self, directory: str, name: str) -> str | None:
        """Why make may succeed on a target no rule names: a file of that name
        exists (in any letter case, as a case-insensitive file system finds it),
        or a built-in rule may make it from a file beside it."""
        path = posixpath.normpath(posixpath.join(directory, name))
        if path.startswith(("..", "/")):
            return f"{name} names a path outside the repository, which may exist where the command runs"
        repo = self.ctx.repo
        if repo.exists(path):
            return f"{path} exists, so make has nothing to do for it"
        if (folded := self._case_folded().get(path.lower())) is not None:
            return f"{folded} exists, and a case-insensitive file system finds it for {path}"
        parent, base = posixpath.split(path)
        sources = [f"{base},v", f"s.{base}", f"RCS/{base},v", f"RCS/{base}", f"SCCS/s.{base}"]
        stems = (base + ".", posixpath.splitext(base)[0] + ".")
        sources += [name for name in self.ctx.files_by_dir.get(parent, ()) if name.startswith(stems)]
        for source in sources:
            if repo.exists(candidate := posixpath.join(parent, source)):
                return f"{candidate} exists, and a built-in make rule may build {path} from it"
        return None

    def _case_folded(self) -> Mapping[str, str]:
        if self._folded is None:
            repo = self.ctx.repo
            self._folded = {path.lower(): path for path in sorted({*repo.files, *repo.dirs})}
        return self._folded

    # mise ------------------------------------------------------------------

    def _owner_tasks(self, owner: str, envs: frozenset[str]) -> _Tasks:
        """The tasks one directory's own config files, file tasks and includes
        define, with ``envs`` the environments the command selects."""
        key = (owner, envs)
        if key in self._tasks:
            return self._tasks[key]
        tasks = _Tasks()
        replaced = False
        for rel in self.ctx.mise_configs.get(owner, ()):
            parsed = self.mise(rel)
            if isinstance(parsed, Unreadable):
                tasks.unreadable.append(parsed)
                continue
            found = config_file(rel)
            env = found.env if found is not None else None
            if env is not None and env not in envs:
                for name in parsed.tasks:
                    tasks.env_only.setdefault(name, rel)
                continue
            tasks.configs.append(rel)
            if parsed.opaque is not None:
                tasks.opaque.append(parsed.opaque)
            tasks.add(parsed.tasks, rel)
            replaced = replaced or parsed.declares_includes
            for entry in parsed.includes:
                self._include(tasks, owner, rel, entry)
        if not replaced:
            prefix = owner + "/" if owner else ""
            for rel in self.ctx.mise_task_files.get(owner, ()):
                entry = task_dir_entry(rel[len(prefix) :])
                if entry is not None:
                    self._task_file(tasks, rel, entry[1])
        self._tasks[key] = tasks
        return tasks

    def _task_file(self, tasks: _Tasks, rel: str, inner: str) -> None:
        """A file inside a task directory, ``inner`` being its path there."""
        parsed = self.mise(rel)
        if isinstance(parsed, Unreadable):
            tasks.unreadable.append(parsed)
            return
        if not rel.endswith(".toml"):
            names, maybe = file_task_names(inner)
            tasks.add(names, rel)
            for name in maybe:
                tasks.maybe.setdefault(name, rel)
            tasks.add(parsed.tasks, rel)
            tasks.opaque += [parsed.opaque] if parsed.opaque is not None else []
            return
        if parsed.opaque is not None:
            tasks.opaque.append(parsed.opaque)
        group = posixpath.dirname(inner).replace("/", ":")
        tasks.add(parsed.tasks, rel)
        if group:
            tasks.add((f"{group}:{name}" for name in parsed.tasks), rel)

    def _include(self, tasks: _Tasks, owner: str, config: str, entry: str) -> None:
        """A local ``[task_config] includes`` entry, taken from the config
        root and from the config file's own directory alike."""
        if "[" in entry:
            tasks.opaque.append(f"{config} includes {entry!r}, a pattern the docs check does not expand")
            return
        expansions = expand_braces(entry)
        if expansions is None:
            tasks.opaque.append(f"{config} includes {entry!r}, which expands to too many paths")
            return
        patterns = {
            posixpath.normpath(posixpath.join(base, expanded))
            for base in (owner, posixpath.dirname(config))
            for expanded in expansions
        }
        tracked = self.ctx.repo.tracked
        for pattern in sorted(patterns):
            regex = glob_regex(pattern) if has_glob(pattern) else None
            for rel in sorted(tracked):
                if regex is not None:
                    if regex.fullmatch(rel):
                        self._task_file(tasks, rel, posixpath.basename(rel))
                    elif (head := next((d for d in _parents(rel) if regex.fullmatch(d)), None)) is not None:
                        self._task_file(tasks, rel, rel[len(head) + 1 :])
                elif rel == pattern:
                    self._task_file(tasks, rel, posixpath.basename(rel))
                elif is_under(rel, pattern):
                    self._task_file(tasks, rel, rel[len(pattern) + 1 :])

    def _visible(self, directory: str, envs: frozenset[str]) -> _Tasks:
        """Every task mise sees from ``directory`` without a monorepo root:
        those of each directory from the root down to it."""
        tasks = _Tasks()
        for owner in _chain(directory):
            tasks.merge(self._owner_tasks(owner, envs))
        return tasks

    def _monorepo(self) -> _Monorepo | Unreadable | None:
        """The root config's monorepo settings, None when it is no monorepo root."""
        found: _Monorepo | None = None
        for rel in self.ctx.mise_configs.get("", ()):
            config = config_file(rel)
            if config is not None and config.env is not None:
                continue
            parsed = self.mise(rel)
            if isinstance(parsed, Unreadable):
                return parsed
            if parsed.monorepo_root and found is None:
                found = _Monorepo(parsed.config_roots, parsed.path_aliases)
        return found

    def _is_config_root(self, monorepo: _Monorepo, path: str) -> bool:
        if not path:
            return True
        if monorepo.config_roots is None:
            return self._has_mise(path)
        return self._listed_root(monorepo, path)

    def _listed_root(self, monorepo: _Monorepo, path: str) -> bool:
        """``path`` matches a ``config_roots`` entry; mise ignores a ``**`` entry."""
        for pattern in monorepo.config_roots or ():
            pattern = _repo_dir(pattern.strip("/"))
            if "**" in pattern:
                continue
            if pattern == path or (has_glob(pattern) and glob_regex(pattern).fullmatch(path)):
                return True
        return False

    def _nearest(self, directory: str, monorepo: _Monorepo) -> str:
        """The project ``directory`` is in: the nearest directory at or above
        it with a mise config file, or with file tasks when ``config_roots``
        lists it."""
        for owner in reversed(_chain(directory)):
            if self.ctx.mise_configs.get(owner):
                return owner
            if owner and self.ctx.mise_task_files.get(owner) and self._listed_root(monorepo, owner):
                return owner
        return ""

    def _own(self, owner: str, task: str, envs: frozenset[str]) -> _Lookup:
        tasks = self._owner_tasks(owner, envs)
        return _Lookup(tasks, task, f"no task {task!r} in {_join_or(tasks.configs) or _where(owner)}")

    def _lookups(
        self, name: str, works: Sequence[str], envs: frozenset[str], monorepo: _Monorepo | None
    ) -> list[_Lookup] | str:
        """Every way mise may read ``name`` run from one of ``works``, or why
        the name cannot be settled. As mise 2026.11 does: ``//p:t`` names a
        config root, or a directory the command runs in or under; ``p:t``
        is also ``//p:t`` (after path aliases, when ``p`` has no ``/``) from
        the root project, or from inside ``p``."""
        if name.startswith("//"):
            if monorepo is None:
                return _NEEDS_MONOREPO.format(form="//path:task")
            path, _, task = name[2:].partition(":")
            path = _repo_dir(monorepo.aliases.get(path, path))
            if not self._is_config_root(monorepo, path) and not any(_at_or_under(work, path) for work in works):
                return [_Lookup(_Tasks(), task, f"{_where(path)} is not a monorepo config root")]
            return [self._own(path, task, envs)]
        if name.startswith(":"):
            if monorepo is None:
                return _NEEDS_MONOREPO.format(form=":task")
            return [self._own(self._nearest(work, monorepo), name[1:], envs) for work in works]
        if monorepo is None:
            return [
                _Lookup(self._visible(work, envs), name, f"no task {name!r} visible from {_where(work)}")
                for work in works
            ]
        lookups = [self._own(self._nearest(work, monorepo), name, envs) for work in works]
        path, colon, task = name.partition(":")
        if colon and "/" not in path:
            target = _repo_dir(monorepo.aliases.get(path, path))
            root = self._is_config_root(monorepo, target)
            if any(
                (root and not self._nearest(work, monorepo)) or (target and _at_or_under(work, target))
                for work in works
            ):
                lookups.append(self._own(target, task, envs))
        return lookups

    def _task(self, draft: Draft, mention: CommandMention) -> Draft:
        name = mention.name
        if name.endswith(":") or (name.startswith("//") and ":" not in name):
            return draft.set("skipped", "names no task", checked=False)
        monorepo = self._monorepo()
        if isinstance(monorepo, Unreadable):
            return draft.set("not_checked", monorepo.reason, checked=False)
        explicit = mention.directory is not None
        if mention.directory is not None:
            dirs = self.ctx.repo.dirs
            works = [d for d in self._named_dirs(draft.doc, mention.directory) if not d or d in dirs]
            if not works:
                return draft.set("skipped", f"no directory {mention.directory} in the repository", checked=False)
            if not any(self._has_mise(owner) for work in works for owner in _chain(work)):
                where = _where(mention.directory)
                return draft.set("skipped", f"no mise config in {where} or above it", checked=False)
        else:
            works = self._near(draft.doc)
        lookups = self._lookups(name, works, mention.environments, monorepo)
        if isinstance(lookups, str):
            return draft.set("not_checked", lookups, checked=False)
        read = _Tasks()
        for lookup in lookups:
            read.merge(lookup.tasks)
            if lookup.task in lookup.tasks.names:
                draft.target = lookup.tasks.names[lookup.task]
                return draft.set("ok", f"defined in {draft.target}")
        task = lookups[0].task
        if not explicit and not name.startswith((":", "//")):
            defining: list[str] = []
            for owner in sorted({*self.ctx.mise_configs, *self.ctx.mise_task_files}):
                tasks = self._owner_tasks(owner, mention.environments)
                read.merge(tasks)
                if name in tasks.names:
                    defining.append(tasks.names[name])
            if defining:
                draft.target = defining[0]
                return draft.set("not_checked", f"defined only in {_join_or(defining)}; {_NO_DIRECTORY}", checked=False)
        if read.unreadable:
            return draft.set("not_checked", read.unreadable[0].reason, checked=False)
        if read.opaque:
            return draft.set("not_checked", f"{read.opaque[0]}, so its tasks are not all known", checked=False)
        for lookup in lookups:
            if (rel := read.env_only.get(lookup.task)) is not None:
                found = config_file(rel)
                env = found.env if found is not None else None
                return draft.set(
                    "not_checked", f"defined only in {rel}, which mise reads only with MISE_ENV={env}", checked=False
                )
            if (rel := read.maybe.get(lookup.task)) is not None:
                return draft.set(
                    "not_checked", f"mise may drop the extension of {rel} from its task name", checked=False
                )
        if not explicit and not any(self._has_mise(owner) for work in works for owner in _chain(work)):
            return draft.set("not_checked", "no mise config where the doc runs it", checked=False)
        detail = " and ".join(dict.fromkeys(lookup.missing for lookup in lookups))
        return self._unproven(draft, mention, task.rsplit(":", 1)[-1], detail)

    def _has_mise(self, owner: str) -> bool:
        return bool(self.ctx.mise_configs.get(owner) or self.ctx.mise_task_files.get(owner))


def _at_or_under(directory: str, path: str) -> bool:
    return directory == path or is_under(directory, path)


def _repo_dir(path: str) -> str:
    normalized = posixpath.normpath(path) if path else "."
    return "" if normalized == "." else normalized


def _parents(path: str) -> list[str]:
    """``a/b/c`` → ``["a/b", "a"]``."""
    out: list[str] = []
    while "/" in path:
        path = posixpath.dirname(path)
        out.append(path)
    return out
