"""Pure rules of the docs check: its settings from the config's ``[docs]`` table, globs, and how a code span,
fence line or prose run names a route, a repository path or a code symbol.

Nothing here reads Markdown or touches git: the regexes run over text that
``markdown_facts`` already extracted (a code span, one fence line, one prose
run), and route/path/symbol matching works on plain strings and sets, so
``doc_refs`` can resolve the results against injected inputs.
"""

from __future__ import annotations

import functools
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from code_intel.code_paths import SKIP_DIRS, rel_posix
from code_intel.config import (
    CONFIG_REL,
    DOC_CHECKS,
    Config,
    ConfigError,
    DocIgnore,
    DocRule,
    Reachability,
)
from code_intel.problems import Problem
from code_intel.route_paths import PARAM_RE

# The stage every docs-check problem carries, whichever module finds it.
STAGE = "doc refs"

# A failing status fails the check; every other status passes. Orphans and
# unused ignore entries are file-level findings, not reference statuses.
BROKEN_STATUSES = frozenset({"missing", "anchor_missing", "method_differs", "weak_param", "unmounted", "ambiguous"})


# ── Config ───────────────────────────────────────────────────────────────

_IGNORE_HEADER_RE = re.compile(r"^[ \t]*\[\[[ \t]*docs\.ignore[ \t]*\]\]", re.MULTILINE)


@dataclass(frozen=True)
class PathRoot:
    """A directory docs write repository paths relative to, and the class a path found there gets."""

    prefix: str  # with a trailing slash
    name: str


def _directory_prefix(directory: str) -> str:
    """``backend`` -> ``backend/``; the repository root (``.``) -> ``""``."""
    normalized = posixpath.normpath(directory)
    return "" if normalized == "." else normalized + "/"


@dataclass(frozen=True)
class DocRefsConfig:
    """The ``[docs]`` table with the layout facts the checks resolve against.

    ``path``: the consumer's config file as the report names it; it is not code,
    so its text never counts as a use of a symbol (an ignore entry's text would
    resolve itself). ``ignore_lines``: the line of each ignore entry in it.
    ``path_roots``: the directories a doc may write a path relative to, besides
    the repository root and the doc's own directory.
    """

    scope: tuple[str, ...]
    route_prefixes: tuple[str, ...]
    reachability: Reachability | None
    rules: tuple[DocRule, ...] = ()
    ignore: tuple[DocIgnore, ...] = ()
    path_roots: tuple[PathRoot, ...] = ()
    ignore_lines: tuple[int, ...] = ()
    path: str = CONFIG_REL

    @classmethod
    def from_config(cls, config: Config, repo_root: Path) -> DocRefsConfig:
        """The docs settings of a loaded config. Raises ``ConfigError`` without a ``[docs]`` table."""
        path = rel_posix(config.path, repo_root)
        docs = config.docs
        if docs is None:
            raise ConfigError(path, "missing required table [docs]")
        package = posixpath.join(config.backend.root, config.backend.package.replace(".", "/"))
        candidates = (
            PathRoot(_directory_prefix(config.backend.root), "backend-rel"),
            PathRoot(_directory_prefix(config.frontend.root), "frontend-rel"),
            PathRoot(_directory_prefix(package), "pkg-rel"),
        )
        roots = tuple(root for index, root in enumerate(candidates) if root.prefix and root not in candidates[:index])
        try:
            text = config.path.read_text(encoding="utf-8-sig")
        except OSError, UnicodeDecodeError:
            text = ""
        return cls(
            scope=docs.scope,
            route_prefixes=docs.route_prefixes,
            reachability=docs.reachability,
            rules=docs.rules,
            ignore=docs.ignore,
            path_roots=roots,
            ignore_lines=tuple(text.count("\n", 0, m.start()) + 1 for m in _IGNORE_HEADER_RE.finditer(text)),
            path=path,
        )

    def in_scope(self, rel: str) -> bool:
        return matches_any(rel, self.scope)

    def rule_for(self, rel: str) -> DocRule | None:
        for rule in self.rules:
            if matches_any(rel, rule.paths):
                return rule
        return None

    def checks_for(self, rel: str) -> tuple[str, ...]:
        rule = self.rule_for(rel)
        return rule.checks if rule is not None else DOC_CHECKS

    def in_orphan_scope(self, rel: str) -> bool:
        reachability = self.reachability
        return (
            reachability is not None
            and matches_any(rel, reachability.include)
            and not matches_any(rel, reachability.exclude)
        )

    def is_config_file(self, rel: str) -> bool:
        return rel == self.path

    def ignore_line(self, index: int) -> int:
        """The config line of ``ignore[index]``, 1 when the file's text did not say."""
        return self.ignore_lines[index] if index < len(self.ignore_lines) else 1


# ── Globs ────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1024)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """``*`` within a segment, ``**`` across segments (``a/**/b`` also matches
    ``a/b``), ``?`` one character. Brackets are literal: Next.js directory names
    such as ``[id]`` are real paths, not character classes."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def glob_match(path: str, pattern: str) -> bool:
    return glob_regex(pattern).fullmatch(path) is not None


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(path, pattern) for pattern in patterns)


def has_glob(path: str) -> bool:
    return "*" in path or "?" in path


# ── Routes ───────────────────────────────────────────────────────────────

ROUTE_METHODS: tuple[str, ...] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "TRACE",
    "WS",
    "WebSocket",
)
_CANONICAL_METHODS = {m.upper(): ("WS" if m == "WebSocket" else m) for m in ROUTE_METHODS}
_METHOD_ALT = "|".join([*ROUTE_METHODS, "WEBSOCKET"])
# R1 in a span, a fence line or a prose run: a method word, whitespace, then a
# path, which may open with an ellipsis (`POST .../approve`, a route tail). The
# path runs to whitespace, a backtick, a quote, `|`, `,` or `;`;
# `_trim_route_path` then cuts it at an unbalanced closer.
_R1_RE = re.compile(rf"(?<![\w/.-])({_METHOD_ALT})[ \t]+((?:\.\.\.|…)?/[^\s`'\"|,;]*)")
# Code (a span or a fence line) also writes the method in lower case
# (`get /appointments`, an HTTP client call); prose does not, and English
# words there ("Cloud Trace / Logging") would read as routes.
_R1_CODE_RE = re.compile(_R1_RE.pattern, re.IGNORECASE)
_ROUTE_TEXT_RE = re.compile(r"(?:([A-Za-z]+)\s+)?(/\S*)")
_CLOSERS = {")": "(", "]": "[", ">": "<", "}": "{"}
_SENTENCE_END = ".:!?"
ROUTE_PLACEHOLDERS: tuple[str, ...] = ("${", "{{", "...", "…")
ROUTE_ELLIPSES: tuple[str, ...] = ("...", "…")
FRAMEWORK_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


def _trim_route_path(raw: str) -> str:
    depth = {opener: 0 for opener in _CLOSERS.values()}
    end = len(raw)
    for index, ch in enumerate(raw):
        if ch in depth:
            depth[ch] += 1
        elif ch in _CLOSERS:
            opener = _CLOSERS[ch]
            if depth[opener] == 0:
                end = index
                break
            depth[opener] -= 1
    path = raw[:end]
    # `/api/...` is a placeholder, not a sentence end: keep it for the placeholder check.
    while len(path) > 1 and path[-1] in _SENTENCE_END and not path.endswith(("...", "…")):
        path = path[:-1]
    return path


def canonical_method(word: str) -> str | None:
    """``GET``/``get`` → ``GET``; ``WebSocket``/``WS`` → ``WS``; None for anything else."""
    return _CANONICAL_METHODS.get(word.upper())


def parse_route_text(text: str) -> tuple[str | None, str] | None:
    """``"METHOD /path"`` or ``"/path"`` → ``(method or None, path)``, else None.

    The method is case-insensitive and canonical in the result (``WebSocket`` →
    ``WS``); the path is cut at an unbalanced closer and trailing sentence
    punctuation, and keeps its query, which matching drops."""
    match = _ROUTE_TEXT_RE.fullmatch(text.strip())
    if match is None:
        return None
    method = None
    if match.group(1) is not None:
        method = canonical_method(match.group(1))
        if method is None:
            return None
    path = _trim_route_path(match.group(2))
    if not path.startswith("/") or path.startswith("//"):
        return None
    return method, path


@dataclass(frozen=True)
class RouteMention:
    """``text`` is the mention as written (method word kept), ``method`` canonical.
    ``gated``: the path must start under a route root to be checked, as a bare
    ``/path`` must (R2)."""

    text: str
    method: str | None
    path: str
    gated: bool = False


def method_route_mentions(text: str, *, code: bool = False) -> list[RouteMention]:
    """Every ``METHOD /path`` or ``METHOD .../tail`` (R1) in one span, fence line (``code``) or prose
    run. In code the method word is case-insensitive; in prose it must be upper
    case or ``WebSocket``. A method written any other way is gated: shell and
    client commands (``head /var/log/x``, ``redis-cli get /cache/key``) put a
    file or key path there, not a route."""
    out: list[RouteMention] = []
    for match in (_R1_CODE_RE if code else _R1_RE).finditer(text):
        path = _trim_route_path(match.group(2))
        if (route_tail(path) or path).startswith("//"):
            continue
        word = match.group(1)
        gated = word not in ROUTE_METHODS and not word.isupper()
        out.append(RouteMention(f"{word} {path}", canonical_method(word), path, gated))
    return out


def bare_route_mention(span: str) -> RouteMention | None:
    """A code span that is entirely ``/path`` (R2)."""
    text = span.strip()
    parsed = parse_route_text(text)
    if parsed is None or parsed[0] is not None or parsed[1] != text:
        return None
    return RouteMention(text, None, text)


def has_route_placeholder(path: str) -> bool:
    return any(marker in path for marker in ROUTE_PLACEHOLDERS)


def route_tail(path: str) -> str | None:
    """``/approve`` for a ``.../approve`` (or ``…/approve``) mention, which
    names the end of a route; None for any other path."""
    for ellipsis in ROUTE_ELLIPSES:
        if path.startswith(ellipsis + "/"):
            return path[len(ellipsis) :]
    return None


# Segment markers. A literal segment is its own text; these never occur in a
# real path segment.
PARAM = "\0param"
CATCH = "\0catch"  # one or more segments
CATCH_OPT = "\0catch?"  # zero or more segments (Next.js `[[...x]]`)
WILD = "\0wild"  # a trailing `*`/`**` in a mention: everything below
_CATCHES = frozenset({CATCH, CATCH_OPT})

_COLON_PARAM_RE = re.compile(r":[A-Za-z_][A-Za-z0-9_]*([*+]?)")
_ANGLE_PARAM_RE = re.compile(r"<[^<>/]+>")
_BRACKET_PARAM_RE = re.compile(r"\[(\[)?(\.\.\.)?[A-Za-z_][A-Za-z0-9_]*\]?\]")


def _strip_route_extras(path: str) -> str:
    return path.split("?", 1)[0].split("#", 1)[0]


def _brace_segment(seg: str) -> str | None:
    """A Starlette-style segment: ``{x:path}`` → CATCH; ``{x}``, ``{x:int}`` or a
    segment that only contains a parameter (``{name}.csv``) → PARAM; else None.
    Docs and backend routes go through this one rule, so a route written in a
    doc exactly as it is defined always matches it."""
    whole = PARAM_RE.fullmatch(seg)
    if whole is not None:
        return CATCH if whole.group(2) == "path" else PARAM
    return PARAM if PARAM_RE.search(seg) else None


def _mention_segment(seg: str, last: bool) -> str:
    if last and seg in ("*", "**"):
        return WILD
    brace = _brace_segment(seg)
    if brace is not None:
        return brace
    colon = _COLON_PARAM_RE.fullmatch(seg)
    if colon is not None:
        return CATCH if colon.group(1) else PARAM
    if _ANGLE_PARAM_RE.fullmatch(seg):
        return PARAM
    bracket = _BRACKET_PARAM_RE.fullmatch(seg)
    if bracket is not None:
        if bracket.group(2):
            return CATCH_OPT if bracket.group(1) else CATCH
        return PARAM
    return seg


def mention_segments(path: str) -> tuple[str, ...]:
    """A doc's route path as segments, every parameter syntax unified
    (``{x}``, ``{x:conv}``, ``:x``, ``<x>``, ``[x]`` → PARAM; ``[...x]``,
    ``:x*``, ``{x:path}`` → CATCH). The query and fragment are dropped; a
    trailing slash stays as an empty last segment because it is significant."""
    parts = _strip_route_extras(path).split("/")[1:]
    return tuple(_mention_segment(seg, index == len(parts) - 1) for index, seg in enumerate(parts))


def backend_route_segments(path: str) -> tuple[str, ...]:
    """A backend route path (Starlette syntax) as segments. A segment that only
    contains a parameter (``{id}.csv``) counts as a parameter."""
    return tuple(_brace_segment(seg) or seg for seg in _strip_route_extras(path).split("/")[1:])


def _has_prefix(path: str, prefix: str) -> bool:
    bare = _strip_route_extras(path)
    return bare == prefix or bare.startswith(prefix + "/")


def omitted_prefix_variants(path: str, prefixes: Iterable[str]) -> tuple[str, ...]:
    """``path`` without each configured prefix it starts with: a doc names
    ``/api/appointments/{id}`` for the route the app serves at
    ``/appointments/{id}`` (a proxy adds the prefix). The prefix alone stays."""
    return tuple(path[len(prefix) :] for prefix in prefixes if _strip_route_extras(path).startswith(prefix + "/"))


def added_prefix_variants(path: str, prefixes: Iterable[str]) -> tuple[str, ...]:
    """``path`` under each configured prefix it does not already start with: a
    doc names ``/appointments/{id}`` for the route served at
    ``/api/appointments/{id}``."""
    return tuple(prefix + path for prefix in prefixes if not _has_prefix(path, prefix))


def route_path_variants(path: str, prefixes: Iterable[str]) -> tuple[str, ...]:
    """A route path as a doc writes it, then without a prefix it starts with,
    then under the prefixes it lacks."""
    prefixes = tuple(prefixes)
    return (path, *omitted_prefix_variants(path, prefixes), *added_prefix_variants(path, prefixes))


def page_route_segments(segments: Sequence[str]) -> tuple[str, ...] | None:
    """Next.js app-router directory segments → URL segments. Route groups
    ``(x)`` and parallel slots ``@x`` drop out; a private ``_x`` folder is not
    routable (None)."""
    out: list[str] = []
    for seg in segments:
        if seg.startswith("_"):
            return None
        if (seg.startswith("(") and seg.endswith(")")) or seg.startswith("@"):
            continue
        bracket = _BRACKET_PARAM_RE.fullmatch(seg)
        if bracket is not None:
            out.append((CATCH_OPT if bracket.group(1) else CATCH) if bracket.group(2) else PARAM)
        else:
            out.append(seg)
    return tuple(out)


@dataclass(frozen=True)
class FrontendPage:
    """A URL the frontend serves: a page, a route handler or a public file.
    ``display`` is the URL as the frontend's source spells it."""

    segments: tuple[str, ...]
    display: str
    file: str
    line: int = 1


@dataclass(frozen=True)
class PageSet:
    """What a ``PageProvider`` found. ``available`` is False when the pages
    could not be read at all (for example the TypeScript toolchain is missing):
    a method-less path that only a page could satisfy is then ``not_checked``
    instead of ``missing``. ``problems`` are carried into the report."""

    pages: tuple[FrontendPage, ...] = ()
    available: bool = True
    problems: tuple[Problem, ...] = field(default=())


NO_PAGES = PageSet()


class PageProvider(Protocol):
    """How a frontend's pages are found; ``[frontend.pages] kind`` picks one.

    ``tracked`` is the set of git-tracked files, so a provider that needs no
    parser (``NextAppPages``, ``NoPages``) works from paths alone.
    """

    def pages(self, tracked: frozenset[str]) -> PageSet: ...


_PAGE_FILE_NAMES = frozenset(f"{stem}.{ext}" for stem in ("page", "route") for ext in ("tsx", "ts", "jsx", "js"))


def public_pages(tracked: Iterable[str], public_dir: str | None) -> list[FrontendPage]:
    """Every file under ``public_dir`` is served at its path below it."""
    if public_dir is None:
        return []
    prefix = _directory_prefix(public_dir)
    pages: list[FrontendPage] = []
    for rel in sorted(tracked):
        if rel.startswith(prefix):
            rest = rel[len(prefix) :]
            pages.append(FrontendPage(tuple(rest.split("/")), "/" + rest, rel))
    return pages


@dataclass(frozen=True)
class NoPages:
    """A frontend with no router the check reads (``kind = "none"``): only public files."""

    public_dir: str | None = None

    def pages(self, tracked: frozenset[str]) -> PageSet:
        return PageSet(tuple(public_pages(tracked, self.public_dir)))


@dataclass(frozen=True)
class NextAppPages:
    """Next.js app-router pages from repository paths alone: ``<app_dir>/**/(page|route).*``
    and the public files."""

    app_dir: str
    public_dir: str | None = None

    def pages(self, tracked: frozenset[str]) -> PageSet:
        prefix = _directory_prefix(self.app_dir)
        found: list[FrontendPage] = []
        for rel in sorted(tracked):
            if rel.startswith(prefix) and posixpath.basename(rel) in _PAGE_FILE_NAMES:
                dirs = rel[len(prefix) :].split("/")[:-1]
                segments = page_route_segments(dirs)
                if segments is not None:
                    display = "/" + "/".join(seg for seg in dirs if not (seg.startswith("(") or seg.startswith("@")))
                    found.append(FrontendPage(segments, display, rel))
        return PageSet((*found, *public_pages(tracked, self.public_dir)))


def route_root(segments: Sequence[str], prefixes: Iterable[str] = ()) -> str | None:
    """The first segment after one optional configured prefix (the longest that
    fits), used by the bare-path gate."""
    if not segments:
        return None
    head = segments[0]
    for prefix in sorted(prefixes, key=len, reverse=True):
        parts = tuple(prefix.strip("/").split("/"))
        if len(segments) > len(parts) and tuple(segments[: len(parts)]) == parts:
            head = segments[len(parts)]
            break
    return head if head and not head.startswith("\0") else None


MatchKind = Literal["exact", "weak"]


def match_segments(mention: Sequence[str], route: Sequence[str]) -> MatchKind | None:
    """How a mention's segments fit a route's: ``exact`` when every literal
    equals a literal or fills a parameter with a value that contains a digit
    (``/articles/42``), ``weak`` when a parameter can only be filled with a
    word (``/articles/camo-types`` against ``/articles/{article_id}``), None
    otherwise. A mention parameter never matches a route literal. Catch-alls on
    either side absorb segments; a mention wildcard never matches here (it is
    a prefix, decided separately)."""
    if WILD in mention:
        return None
    if not any(seg in _CATCHES for seg in mention) and not any(seg in _CATCHES for seg in route):
        # Without catch-alls segments pair one to one; this is nearly every
        # route, and the search below costs a cache per call.
        if len(mention) != len(route):
            return None
        weak_fill = False
        for m, r in zip(mention, route, strict=True):
            if m == r:
                continue
            if r != PARAM or m in ("", PARAM):
                return None
            weak_fill = weak_fill or not any(ch.isdigit() for ch in m)
        return "weak" if weak_fill else "exact"

    @functools.cache
    def go(i: int, j: int, weak: bool) -> MatchKind | None:
        if i == len(mention) and j == len(route):
            return "weak" if weak else "exact"
        found: MatchKind | None = None
        if j < len(route) and route[j] in (CATCH, CATCH_OPT):
            start = i if route[j] == CATCH_OPT else i + 1
            for end in range(start, len(mention) + 1):
                found = _better(found, go(end, j + 1, weak))
            return found
        if i < len(mention) and mention[i] in (CATCH, CATCH_OPT):
            start = j if mention[i] == CATCH_OPT else j + 1
            for end in range(start, len(route) + 1):
                found = _better(found, go(i + 1, end, weak))
            return found
        if i == len(mention) or j == len(route):
            return None
        m, r = mention[i], route[j]
        if m == r:
            return go(i + 1, j + 1, weak)
        if r == PARAM and m != PARAM and m != "":
            return go(i + 1, j + 1, weak or not any(ch.isdigit() for ch in m))
        return None

    best = go(0, 0, False)
    go.cache_clear()
    return best


def _better(a: MatchKind | None, b: MatchKind | None) -> MatchKind | None:
    if a == "exact" or b == "exact":
        return "exact"
    return a or b


def strict_prefix(mention: Sequence[str], route: Sequence[str]) -> bool:
    """The mention's segments (wildcard and trailing slash dropped) are the
    first segments of the route, literal for literal and parameter for
    parameter. A bare wildcard mention also covers the route equal to its stem."""
    stem = list(mention)
    wild = bool(stem) and stem[-1] == WILD
    if wild or (stem and stem[-1] == ""):
        stem.pop()
    if len(route) < len(stem) or (not wild and len(route) == len(stem)):
        return False
    return all(_strict_equal(m, r) for m, r in zip(stem, route, strict=False))


def strict_suffix(mention: Sequence[str], route: Sequence[str]) -> bool:
    """The mention's segments are the last segments of a longer route
    (a parameter literal must contain a digit)."""
    if len(route) <= len(mention) or WILD in mention:
        return False
    return all(_strict_equal(m, r) for m, r in zip(mention, route[len(route) - len(mention) :], strict=True))


def match_suffix(tail: Sequence[str], route: Sequence[str]) -> MatchKind | None:
    """How a route tail's segments fit the end of a route: the best
    ``match_segments`` fit against the route or the route without some of its
    leading segments. The whole route counts, because the ellipsis may stand
    for the ``/api`` prefix a doc leaves out."""
    best: MatchKind | None = None
    for start in range(len(route)):
        best = _better(best, match_segments(tail, route[start:]))
        if best == "exact":
            break
    return best


def _strict_equal(m: str, r: str) -> bool:
    if m == r:
        return True
    if m == PARAM:
        return r in (PARAM, CATCH, CATCH_OPT)
    return r == PARAM and m != "" and not m.startswith("\0") and any(ch.isdigit() for ch in m)


def methods_compatible(mention: str | None, route: str) -> bool:
    """No mention method matches any route; ``WS`` matches WebSocket routes
    only; ``HEAD`` is served by GET routes; a route method ``ANY`` takes all."""
    if mention is None or route == "ANY":
        return True
    if mention == "HEAD":
        return route in ("GET", "HEAD")
    return mention == route


# ── Paths ────────────────────────────────────────────────────────────────

PathForm = Literal["span", "command", "fence"]

KNOWN_EXTENSIONS = frozenset(
    {
        "py",
        "pyi",
        "ts",
        "tsx",
        "mts",
        "cts",
        "js",
        "jsx",
        "cjs",
        "mjs",
        "md",
        "json",
        "yml",
        "yaml",
        "toml",
        "ini",
        "sql",
        "sh",
        "proto",
        "css",
        "cfg",
        "txt",
        "csv",
        "lock",
        "env",
        "html",
        "png",
        "svg",
        "ico",
        "jpg",
        "jpeg",
        "gif",
        "webp",
    }
)
# `:12`, `:12-30`, `:12–30`, `:12:4`, `#L12`, `#L12-L30`, `#symbol`, `::node`.
_PATH_SUFFIX_RE = re.compile(r"(?::\d+(?:[-–:,]\d+)*|#L\d+(?:-L?\d+)?|#[A-Za-z_][\w.-]*|::[\w:\[\]./-]+)$")
_URL_PREFIXES: tuple[str, ...] = ("http:", "https:", "mailto:", "gs:", "s3:", "git@", "data:", "blob:", "file:")
_PLACEHOLDER_RE = re.compile(r"<[^<>]*>|\{[^{},]*\}|\.\.\.|…|\$\{?[A-Za-z_]|YYYY|NNNN|XXX|Xxx")
_BRACES_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_QUOTES = "\"'`"
_LEAD_OPENERS = "([{"
_TRAIL_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _balanced_trim(token: str) -> str:
    previous = None
    while token and token != previous:
        previous = token
        token = token.strip(_QUOTES)
        token = token.rstrip(",;")
        if len(token) > 1 and token[0] in _LEAD_OPENERS and token[-1] == _closer(token[0]):
            token = token[1:-1]
            continue
        if token and token[0] in _LEAD_OPENERS and token.count(token[0]) > token.count(_closer(token[0])):
            token = token[1:]
        if token and token[-1] in _TRAIL_CLOSERS and token.count(token[-1]) > token.count(_TRAIL_CLOSERS[token[-1]]):
            token = token[:-1]
        if len(token) > 1 and token[-1] in ".:" and token[-2] not in "./":
            token = token[:-1]
    return token


def _closer(opener: str) -> str:
    return {"(": ")", "[": "]", "{": "}"}[opener]


def path_tokens(text: str, form: PathForm) -> list[str]:
    """Candidate path tokens of a whole span (``span``), a span with spaces
    (``command``) or one fence line (``fence``), trimmed of quotes, unbalanced
    brackets, a ``key=`` or ``call(`` head and sentence punctuation. Whether a
    token is a path is decided later by ``split_path_token``."""
    raw = [text.strip()] if form == "span" else text.split()
    out: list[str] = []
    for token in raw:
        if "=" in token:
            token = token.rsplit("=", 1)[1]
        if "(" in token[1:]:
            token = token[token.rindex("(") + 1 :]
        parts = token.split(",") if "{" not in token else [token]
        for part in parts:
            trimmed = _balanced_trim(part)
            if trimmed:
                out.append(trimmed)
    return out


@dataclass(frozen=True)
class PathToken:
    """``text``: the token as written (suffix kept). ``path``: suffix and a
    leading ``./`` removed, the form that is resolved."""

    text: str
    path: str


def has_known_extension(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    return bool(dot) and bool(stem) and ext.lower() in KNOWN_EXTENSIONS


def split_path_token(token: str) -> PathToken | None:
    """The token as a path candidate, or None when it is not one: a URL, a
    flag, ``~/…``, an absolute ``/…`` (a route or a filesystem path), or text
    with neither a ``/`` nor a known extension."""
    if not token or token.startswith(("-", "~", "/")) or "://" in token or token.startswith(_URL_PREFIXES):
        return None
    path = _PATH_SUFFIX_RE.sub("", token)
    while path.startswith("./"):
        path = path[2:]
    if not path or ("/" not in path and not has_known_extension(path)):
        return None
    return PathToken(token, path)


def is_placeholder_path(path: str) -> bool:
    return _PLACEHOLDER_RE.search(path) is not None


def is_example_path(path: str) -> bool:
    """A segment whose stem starts ``my_``/``my-``, is ``example`` or starts
    ``example_``/``example-``/``Example``: illustrations, not repository files."""
    for seg in path.strip("/").split("/"):
        stem = seg.split(".", 1)[0]
        lowered = stem.lower()
        if lowered.startswith(("my_", "my-")) or lowered == "example" or lowered.startswith(("example_", "example-")):
            return True
        if stem.startswith("Example"):
            return True
    return False


def has_skip_dir(path: str) -> bool:
    """A segment such as ``.venv`` or ``node_modules``: local-only, never in git."""
    return any(seg in SKIP_DIRS for seg in path.split("/"))


def expand_braces(path: str) -> list[str]:
    """``a/{b,c}.py`` → ``["a/b.py", "a/c.py"]``, nested groups expanded in turn."""
    match = _BRACES_RE.search(path)
    if match is None:
        return [path]
    out: list[str] = []
    for alternative in match.group(1).split(","):
        out.extend(expand_braces(path[: match.start()] + alternative + path[match.end() :]))
    return out


def path_variants(path: str) -> list[str]:
    """The path, then the files it may stand for: ``pkg/mod.attr`` →
    ``pkg/mod.py``…; an import specifier without extension → ``x.ts``…"""
    out = [path]
    name = path.rsplit("/", 1)[-1]
    if not path.endswith("/") and not has_known_extension(path):
        stem, dot, attr = path.rpartition(".")
        if dot and stem and "/" not in attr and re.fullmatch(r"[A-Za-z_]\w*", attr):
            out.extend(f"{stem}{ext}" for ext in (".py", ".ts", ".tsx", "/__init__.py"))
        if "." not in name:
            out.extend(
                f"{path}{ext}" for ext in (".ts", ".tsx", ".js", ".jsx", ".py", "/index.ts", "/index.tsx", "/index.js")
            )
    return out


def normalize_repo_path(path: str) -> str | None:
    """POSIX-normalized repo-relative path, or None when it escapes the repository."""
    normalized = posixpath.normpath(path)
    if normalized == "." or normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        return None
    return normalized


_MARKDOWN_SUFFIXES: tuple[str, ...] = (".md", ".markdown")


def is_external_href(href: str) -> bool:
    """A link with a URL scheme (``https:``, ``mailto:``, ``blob:``…) or ``//host``."""
    if href.startswith("//"):
        return True
    scheme, colon, _ = href.partition(":")
    return bool(colon) and bool(scheme) and scheme[0].isalpha() and all(ch.isalnum() or ch in "+.-" for ch in scheme)


def is_markdown(rel: str) -> bool:
    return rel.lower().endswith(_MARKDOWN_SUFFIXES)


# ── Symbols ──────────────────────────────────────────────────────────────

_SYMBOL_RE = re.compile(r"(<\s*)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(\(\))?(\s*/?\s*>)?")
_HUMP_RE = re.compile(r"[A-Z][a-z0-9]+|[A-Z]+(?![a-z])")
CLASS_KINDS = frozenset({"class", "enum"})


@dataclass(frozen=True)
class SymbolMention:
    """``name`` without ``()`` or JSX brackets; ``text`` as written."""

    text: str
    name: str
    call: bool
    jsx: bool

    @property
    def parts(self) -> list[str]:
        return self.name.split(".")


def symbol_mention(span: str) -> SymbolMention | None:
    """A whole code span that names a symbol: ``Ident(.Ident)*``, optionally
    ``()``, or JSX ``<Comp>``/``<Comp />``. Lowercase or all-caps JSX (``<div>``, ``<ENTRY>``)
    is an HTML tag or a placeholder, and a dotted name ending in a file
    extension (``config.py``) is a file name; neither is a symbol."""
    text = span.strip()
    match = _SYMBOL_RE.fullmatch(text)
    if match is None:
        return None
    opened, name, call, closed = match.group(1), match.group(2), match.group(3), match.group(4)
    jsx = opened is not None
    if jsx != (closed is not None) or (jsx and call):
        return None
    last = name.rsplit(".", 1)[-1]
    # A component name is capitalized and not all caps; `<ENTRY>` is a placeholder.
    if jsx and not (last[:1].isupper() and any(ch.islower() for ch in last)):
        return None
    if "." in name and name.rsplit(".", 1)[-1].lower() in KNOWN_EXTENSIONS:
        return None
    return SymbolMention(text, name, call is not None, jsx)


def is_camel_case(word: str) -> bool:
    """Two or more humps (``AppointmentStatus``, ``HTTPException``); a single
    capitalized word, an acronym or UPPER_SNAKE is not."""
    return (
        word[:1].isupper() and "_" not in word and any(ch.islower() for ch in word) and len(_HUMP_RE.findall(word)) >= 2
    )


def is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def symbol_tier(mention: SymbolMention, python_classes: frozenset[str] | set[str]) -> Literal["A", "B"]:
    """Tier A fails when it resolves nowhere; Tier B is reported ``unresolved``
    but never fails. A: a CamelCase last part, a non-dotted ``_private`` name or
    ``name()`` call, a JSX component, or a dotted name whose head is in
    ``python_classes``. Everything else (bare camelCase/snake_case/UPPER_SNAKE,
    a single capitalized word, ``appointment.approve``, a protocol dunder such
    as ``__wrapped__``) is B.

    ``python_classes`` holds Python class names only: TypeScript classes are
    indexed only where Node runs, and a tier that depended on them would fail
    ``ApiClient.removedMethod`` on one machine and pass it on another."""
    parts = mention.parts
    if mention.jsx or is_camel_case(parts[-1]):
        return "A"
    if len(parts) == 1:
        private = mention.name.startswith("_") and not is_dunder(mention.name)
        return "A" if private or mention.call else "B"
    return "A" if parts[0] in python_classes else "B"


_MODULE_INDEX_NAMES = frozenset({"__init__", "index"})
# No package binds anything: a report built without reading `__init__.py`.
NO_BINDINGS: Mapping[str, frozenset[str]] = MappingProxyType({})


def module_path(file: str) -> tuple[str, ...]:
    """The import path of a source file as its parts: ``pkg/mod.py`` is
    ``pkg.mod``; a package's ``__init__.py`` or a folder's ``index.ts`` stands
    for its directory."""
    parts = posixpath.splitext(file)[0].split("/")
    return tuple(parts[:-1] if parts[-1] in _MODULE_INDEX_NAMES else parts)


def qualifiers_match(
    container: str | None,
    file: str,
    qualifiers: Sequence[str],
    *,
    name: str,
    scope: str,
    bindings: Mapping[str, frozenset[str]],
) -> bool:
    """A symbol row (``name`` in ``container`` of ``file``) defines
    ``Q1...Qn.name`` when the qualifiers, aligned from the right, fit its
    container chain and, where they outrun it, its module path. One qualifier
    fits a container ending with it (a nested class ``Outer.Inner`` defines
    ``Inner.attr``); more must match the container chain all the way
    (``Wrong.Inner.attr`` is not ``Outer.Inner``'s). Those left over once the
    container is used up name the module, so ``appointment.Appointment.status``
    fits ``backend/service_backend/models/appointment.py`` and
    ``appointment_conflict.check_overlap`` (a module-level row) its file.

    In Python they may instead name a package containing the module, as a run
    of the module path ending at that package, but only when the package's
    ``__init__.py`` binds the next name (the container's first part, else
    ``name``): ``bindings`` maps a package directory to those names. So
    ``models.Appointment.status`` fits, since ``models/__init__.py`` imports
    ``Appointment``, while ``pkg.PkgAuthService`` does not fit
    ``pkg/auth.py`` when ``pkg/__init__.py`` imports nothing, nor
    does ``service_backend.appointment`` (skipping ``models``). A TypeScript
    directory re-exports nothing without its ``index.ts``, and taking it would
    resolve a permission such as ``article.view`` to any ``view`` under
    ``components/article/``. A row with no container is reachable through its
    module only at module level, never as a local of a function body (``const
    timezone`` inside a component)."""
    chain = tuple(container.split(".")) if container else ()
    wanted = tuple(qualifiers)
    if len(wanted) <= len(chain):
        return chain[len(chain) - len(wanted) :] == wanted
    head = wanted[: len(wanted) - len(chain)]
    if wanted[len(head) :] != chain or (not chain and scope != "module"):
        return False
    module = module_path(file)
    if module[len(module) - len(head) :] == head:
        return True
    if not file.endswith((".py", ".pyi")):
        return False
    exported = chain[0] if chain else name
    return any(
        module[end - len(head) : end] == head and exported in bindings.get("/".join(module[:end]), ())
        for end in range(len(head), len(module))
    )


def is_upper_fragment(name: str) -> bool:
    """``_ACCOUNTING``: an UPPER_SNAKE suffix that prose attaches to a name
    (``HEARTBEAT_HISTORY`` + ``_ACCOUNTING``), resolved against word endings."""
    return (
        name.startswith("_")
        and not is_dunder(name)
        and name.lstrip("_").replace("_", "").isalnum()
        and name.upper() == name
    )


def is_example_symbol(mention: SymbolMention) -> bool:
    """A part that is an illustration rather than a name: ``X``/``XXX``/``Xxx``,
    or ``my_…``/``MY_…``/``example…``/``Example…`` (``AuditAction.MY_NEW_ACTION``)."""
    for part in mention.parts:
        lowered = part.lower()
        if lowered in ("x", "xx", "xxx", "xxxx") or lowered.startswith(("my_", "example")):
            return True
    return False
