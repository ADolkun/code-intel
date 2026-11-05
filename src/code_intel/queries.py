"""Query semantics for the MCP tools: pure functions over analyzer data.

Each function takes analyzer output (DI JSON v2, routes JSON v2, JSX usages or a
symbol result) plus the tool's arguments and returns a typed result. Nothing here
reads a cache, touches the filesystem or formats text. Rows pass through from the
analyzers unchanged apart from the display labels added here.

Name matching is exact. The only fuzzy output is the close-name
suggestion list for a name that matched nothing.

Keep this module importable on an older interpreter (no PEP 695/758 syntax), like
the rest of the server.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from code_intel import problems, route_paths
from code_intel.symbol_resolve import file_hint_matches

MATCH_MODES = ("exact", "suffix", "contains", "auto")

# How a route reaches a dependency, in the order the label is decided: the most
# specific declaration wins, and a dependency reached only through another
# dependency is `transitive`.
VIA_ORDER = ("direct", "route decorator", "router", "app")


# ── Limits ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Page[T]:
    """The rows shown after a limit, plus how many matched in total."""

    items: list[T]
    total: int
    omitted: int


def bounded[T](items: list[T], limit: int | None) -> tuple[list[T], int]:
    assert limit is None or limit >= 0, f"limit must be None or >= 0, got {limit!r}"
    if limit is None:
        return items, 0
    return items[:limit], max(0, len(items) - limit)


def page[T](items: list[T], limit: int | None) -> Page[T]:
    visible, omitted = bounded(items, limit)
    return Page(visible, len(items), omitted)


# ── Suggestions ──────────────────────────────────────────────────────────


def close_names(name: str, known: Iterable[str]) -> list[str]:
    return difflib.get_close_matches(name, sorted(set(known)), n=3, cutoff=0.6)


def unknown_name_hint(label: str, name: str | None, known: Iterable[str]) -> str | None:
    """'did you mean' text when ``name`` is not a known handler/function, else None."""
    known_set = set(known)
    if not name or name in known_set:
        return None
    close = close_names(name, known_set)
    if close:
        return f"Unknown {label} '{name}'; did you mean {', '.join(close)}?"
    return f"Unknown {label} '{name}'."


def slash_hint(path: str | None, match: str, known_paths: Iterable[str]) -> str | None:
    """Trailing slashes are significant (the app sets ``redirect_slashes=False``),
    so say when the other spelling exists instead of silently matching it."""
    if not path or match != "exact":
        return None
    key = route_paths.path_key(path)
    other = key[:-1] if key.endswith("/") and key != "/" else f"{key}/"
    if any(route_paths.path_key(p) == other for p in known_paths):
        return f"A route exists at '{other}' (trailing slash differs; slashes are significant)."
    return None


def no_match_hints(path: str | None, match: str, unknown: list[str | None]) -> list[str]:
    hints = [h for h in unknown if h]
    # Loosening the path match only helps when a path was actually given.
    if path and match == "exact":
        hints.append("Try match='suffix' for loose path matching.")
    return hints


# ── Path matching ────────────────────────────────────────────────────────


def _params_blank(text: str) -> str:
    return route_paths.PARAM_RE.sub("{}", text)


def canon(path: str) -> str:
    """Parameter-agnostic path: `/articles/{id}` and `/articles/{article_id}` agree."""
    return route_paths.path_key(path)


def path_matches(query: str, *candidates: str, match: str = "exact") -> bool:
    """Match a query path against candidate paths.

    - exact: equal ``path_key`` (query dropped, parameters as ``{}``, trailing
      slash significant).
    - suffix: exact, or the candidate's last segments equal the query's segments
      (so `/users` matches `/admin/audit/users` but not `/superusers`).
    - contains: substring of a candidate once every parameter in both is
      ``{}``, so parameter names are ignored here too; the query string is kept
      because a substring query may be about it.
    - auto: exact or suffix.
    """
    if match == "contains":
        needle = _params_blank(query)
        return any(needle in _params_blank(c) for c in candidates if c)
    query_key = canon(query)
    keys = [canon(c) for c in candidates if c]
    if match == "exact":
        return query_key in keys
    if match in ("suffix", "auto"):
        query_segments = route_paths.split_segments(query_key)
        n = len(query_segments)
        return any(k == query_key or route_paths.split_segments(k)[-n:] == query_segments for k in keys)
    raise ValueError(f"match {match!r} is not one of {MATCH_MODES}")


# ── DI labels ────────────────────────────────────────────────────────────


def dep_labels(dependency_functions: Mapping[str, Mapping]) -> dict[str, str]:
    """id -> short name, or the full id where two targets share a name."""
    counts: dict[str, int] = {}
    for info in dependency_functions.values():
        counts[info["name"]] = counts.get(info["name"], 0) + 1
    return {
        dep_id: (info["name"] if counts[info["name"]] == 1 else dep_id) for dep_id, info in dependency_functions.items()
    }


def _labels_for(ids: Iterable[str], labels: Mapping[str, str]) -> list[str]:
    return [labels.get(i, i) for i in ids]


# ── DI: di_deps ──────────────────────────────────────────────────


@dataclass(frozen=True)
class DependencyListing:
    """Every dependency target (no handler or path given), sorted by (name, module)."""

    functions: Page[dict]
    route_count: int


@dataclass(frozen=True)
class DiRouteQuery:
    handler: str | None
    path: str | None
    method: str | None
    match: str

    def as_dict(self) -> dict[str, Any]:
        return {"handler": self.handler, "path": self.path, "method": self.method, "match": self.match}


@dataclass(frozen=True)
class DiRouteSelection:
    """Routes matching a handler/path/method query. ``hints`` explain an empty or
    method-filtered result."""

    query: DiRouteQuery
    routes: Page[dict]
    hints: list[str]


def _handler_matches(route: Mapping, handler: str) -> bool:
    """A handler is named by its short name or its exact ``module:qualname`` id."""
    return route["handler"] == handler or route["handler_id"] == handler


def di_deps(
    data: Mapping,
    *,
    handler: str | None,
    path: str | None,
    method: str | None,
    match: str,
    limit: int | None,
) -> DependencyListing | DiRouteSelection:
    routes = data["routes"]
    functions = data["dependency_functions"]
    labels = dep_labels(functions)
    if not handler and not path:
        rows = [
            {**info, "label": labels[dep_id], "sub_dep_labels": _labels_for(info["sub_deps"], labels)}
            for dep_id, info in functions.items()
        ]
        rows.sort(key=lambda r: (r["name"], r["module"], r["id"]))
        return DependencyListing(functions=page(rows, limit), route_count=len(routes))

    query = DiRouteQuery(handler=handler, path=path, method=method, match=match)
    candidates = [
        r
        for r in routes
        if (not handler or _handler_matches(r, handler)) and (not path or path_matches(path, r["path"], match=match))
    ]
    matches = [r for r in candidates if not method or r["method"] == method]

    if not matches:
        hints: list[str] = []
        if candidates:
            # The route exists, only the method is wrong: say which methods exist.
            subject = handler or path
            methods = ", ".join(sorted({r["method"] for r in candidates}))
            hints.append(f"{subject} exists for {methods}; none for {method}.")
        else:
            hints = no_match_hints(
                path,
                match,
                [
                    unknown_name_hint("handler", handler, (r["handler"] for r in routes)),
                    slash_hint(path, match, (r["path"] for r in routes)),
                ],
            )
        return DiRouteSelection(query=query, routes=Page([], 0, 0), hints=hints)

    visible, omitted = bounded(matches, limit)
    rows = [{**r, "all_dep_labels": _labels_for(r["all_deps"], labels)} for r in visible]
    return DiRouteSelection(query=query, routes=Page(rows, len(matches), omitted), hints=[])


# ── DI: di_reverse ───────────────────────────────────────────────


@dataclass(frozen=True)
class UnknownDependency:
    dep_function: str
    suggestions: list[str]
    known_count: int


@dataclass(frozen=True)
class DependencyGroup:
    """Routes using one dependency target, each labelled with how it gets there."""

    target: dict
    sub_dep_labels: list[str]
    transitive_labels: list[str]
    routes: list[dict]  # the visible rows after the shared limit
    route_count: int
    omitted: int
    via_counts: dict[str, int]


@dataclass(frozen=True)
class DependencyUsage:
    dep_function: str  # as given
    query_name: str  # what was matched: the id, or the text before "("
    matched_on: str  # "id" | "name" | "alias"
    groups: list[DependencyGroup]

    @property
    def route_count(self) -> int:
        """Rows across every group: the limit that shows them all, since the limit is shared."""
        return sum(g.route_count for g in self.groups)


# A factory call as written in a route: the callable's name, then its arguments
# in one balanced pair of parentheses that ends the text.
_FACTORY_CALL_RE = re.compile(r"(?P<name>[^\s()]+)\((?P<args>.*)\)", re.DOTALL)


def is_dep_id(dep: str) -> bool:
    """Whether ``dep`` is a ``module:qualname`` id as `di_deps` prints
    it: a ``:`` before any ``(``, so ``pkg:C()`` and ``pkg:Svc().get`` are ids
    while a factory call with a ``:`` in its arguments is not."""
    return ":" in dep.partition("(")[0]


def bare_dep_name(dep: str) -> str | None:
    """The name a ``dep_function`` query matches on.

    `require_global_permission(AUDIT_VIEW)` gives `require_global_permission`;
    text without `(` is the name as given, untrimmed. Text with a `(` that is
    not ``name(arguments)`` gives None, so a malformed call is rejected instead
    of matching on whatever precedes the parenthesis.
    """
    if "(" not in dep and ")" not in dep:
        return dep
    call = _FACTORY_CALL_RE.fullmatch(dep)
    return call.group("name") if call else None


def find_dependency_targets(functions: Mapping[str, Mapping], dep_function: str) -> tuple[str, str, list[str]]:
    """``(matched_on, query_name, ids)`` by exact id, then exact name, then exact alias.

    ``dep_function`` must be an id or pass `bare_dep_name`; the tool validates
    it before any analysis runs. An id matches only itself, never a name.
    """
    if dep_function in functions:
        return "id", dep_function, [dep_function]
    if is_dep_id(dep_function):
        return "id", dep_function, []
    name = bare_dep_name(dep_function)
    if name is None:
        raise ValueError(f"dep_function {dep_function!r} is not a name or a name(arguments) factory call")
    by_name = [dep_id for dep_id, info in functions.items() if info["name"] == name]
    if by_name:
        return "name", name, sorted(by_name)
    by_alias = [dep_id for dep_id, info in functions.items() if name in info.get("aliases", [])]
    return "alias", name, sorted(by_alias)


def dep_via(route: Mapping, dep_id: str) -> tuple[str, Mapping | None]:
    """(label, the dep row that gives it) for how ``route`` gets ``dep_id``."""
    rows = [d for d in route["deps"] if d["id"] == dep_id]
    for via in VIA_ORDER:
        for row in rows:
            if row["via"] == via:
                return via, row
    return "transitive", None


def di_reverse(data: Mapping, dep_function: str, *, limit: int | None) -> DependencyUsage | UnknownDependency:
    functions = data["dependency_functions"]
    matched_on, query_name, ids = find_dependency_targets(functions, dep_function)
    if not ids:
        if matched_on == "id":
            known = set(functions)
        else:
            known = {info["name"] for info in functions.values()}
            known.update(alias for info in functions.values() for alias in info.get("aliases", []))
        return UnknownDependency(
            dep_function=dep_function,
            suggestions=close_names(query_name, known),
            known_count=len(functions),
        )

    labels = dep_labels(functions)
    remaining = limit
    groups = []
    for dep_id in ids:
        using = []
        via_counts: dict[str, int] = {}
        for r in data["routes"]:
            if dep_id not in r["all_deps"]:
                continue
            via, row = dep_via(r, dep_id)
            via_counts[via] = via_counts.get(via, 0) + 1
            using.append(
                {
                    "method": r["method"],
                    "path": r["path"],
                    "handler": r["handler"],
                    "handler_id": r["handler_id"],
                    "file": r["file"],
                    "line": r["line"],
                    "via": via,
                    "via_dep": row["display"] if row else None,
                    "via_alias": row.get("alias") if row else None,
                }
            )
        visible, omitted = bounded(using, remaining)
        if remaining is not None:
            remaining -= len(visible)
        info = functions[dep_id]
        groups.append(
            DependencyGroup(
                target=dict(info),
                sub_dep_labels=_labels_for(info["sub_deps"], labels),
                transitive_labels=_labels_for(info["transitive_deps"], labels),
                routes=visible,
                route_count=len(using),
                omitted=omitted,
                via_counts=via_counts,
            )
        )
    return DependencyUsage(dep_function=dep_function, query_name=query_name, matched_on=matched_on, groups=groups)


# ── Routes: route_match ──────────────────────────────────────────


def frontend_unavailable_diagnostic(data: Mapping) -> Mapping | None:
    for diag in data.get("diagnostics", []):
        if diag.get("kind") == "frontend_unavailable":
            return diag
    return None


def frontend_problem_text(diag: Mapping) -> str:
    """``"<message> (missing: <path>). Fix: <fix>"`` for a `frontend_unavailable`
    diagnostic, naming the missing path when the message does not already."""
    fix = diag.get("fix")
    message = problems.strip_fix(
        str(diag.get("message") or f"frontend_unavailable diagnostic for {diag.get('file')!r} has no message"), fix
    )
    missing = diag.get("missing")
    if missing and missing not in message:
        message = f"{message} (missing: {missing})"
    return f"{message}. Fix: {fix}" if fix else message


# Summary counts that only mean something once the frontend was analyzed.
FRONTEND_SUMMARY_KEYS = (
    "frontend_calls",
    "test_calls_skipped",
    "matched_routes",
    "matched_calls",
    "match_rows",
    "backend_only",
    "frontend_only",
    "coverage_pct",
)


# ── Routes: what an orphan claim rests on ────────────────────────────────

# `route_map.STAGE`: every finding of the frontend routes scan carries it; the
# backend model's findings carry their own stages.
FRONTEND_ROUTES_STAGE = "frontend routes"

# What a finding can make the analysis get wrong about the app.
CALLS_MISSED = "calls_missed"  # a call is not in the scan
VARIANTS_DROPPED = "variants_dropped"  # a call is, but some of its URL variants are not
CALLS_MISREAD = "calls_misread"  # a call's URL may be read wrong
ROUTES_MISSED = "routes_missed"  # a route is not in the model
ROUTES_MISPLACED = "routes_misplaced"  # a route is, at a path with an unresolved part that matches no call
ROUTES_CHANGED = "routes_changed"  # counted routes may also be moved or removed
CALLS_CAUGHT = "calls_caught"  # a mount it misses may catch requests for routes after it

# Frontend findings by effect. Not listed: `unresolved_http_method` (an UNKNOWN
# method matches any route) and `unresolved_prop_value` (a non-literal prop is
# kept as a possible variant). A `decode_failed` file is read with replaced
# bytes, which may sit in a URL or in a client's name; TypeScript's recovered tree after a
# `typescript_syntax_error` can swallow the rest of a file (an unclosed comment
# hides every later call) or misread a literal.
FRONTEND_CALL_GAPS: Mapping[str, frozenset[str]] = {
    **dict.fromkeys(
        (
            "list_failed",
            "stat_failed",
            "read_failed",
            "parse_failed",
            "analysis_failed",
            "frontend_client_module_missing",
            "unparsed_frontend_callsite",
            "unresolved_frontend_path",
        ),
        frozenset({CALLS_MISSED}),
    ),
    "decode_failed": frozenset({CALLS_MISSED, CALLS_MISREAD}),
    "typescript_syntax_error": frozenset({CALLS_MISSED, CALLS_MISREAD}),
    "url_variants_truncated": frozenset({VARIANTS_DROPPED}),
}

# Backend findings by effect. A file that is not analyzed is not imported
# either (Python cannot read it), and `router_include_cycle` cuts an edge whose
# routes are served. Code the analysis does not follow that is handed a router
# (`opaque_router_call`) is taken to use FastAPI's registration API, which only
# adds routes. An unresolved path part keeps the route under a placeholder
# that matches no call. Changing a router in place, a router class that
# overrides registration, a call chain the analysis stops following and a
# change from a module nothing imports (it may still be imported another way)
# can move or remove routes it counted. A mount catches every request under
# its path, so one the analysis misses (any finding that may hide code, an
# ASGI app, `host`, `routes=`) or misplaces may take the calls of routes
# registered after it and answer 404; `mount_shadows_route` names a route a
# modeled mount takes. Not listed: routes the analysis judges
# not live (`unmounted_router`, `deferred_*`), routes it keeps although they
# may not run (`conditional_*`), findings that widen matching
# (`unresolved_route_methods` is ANY, `unresolved_redirect_slashes` is True)
# and DI-only findings.
BACKEND_ROUTE_GAPS: Mapping[str, frozenset[str]] = {
    **dict.fromkeys(("unresolved_route_router", "router_include_cycle"), frozenset({ROUTES_MISSED})),
    **dict.fromkeys(
        (
            "list_failed",
            "stat_failed",
            "read_failed",
            "decode_failed",
            "parse_failed",
            "analysis_failed",
            "unresolved_app",
            "unresolved_include",
            "opaque_router_call",
            "asgi_mount",
            "unsupported_route_registration",
            "unsupported_router_argument",
        ),
        frozenset({ROUTES_MISSED, CALLS_CAUGHT}),
    ),
    **dict.fromkeys(
        ("unresolved_route_path", "unresolved_route_prefix", "unresolved_include_prefix"),
        frozenset({ROUTES_MISPLACED}),
    ),
    "unresolved_mount_path": frozenset({ROUTES_MISPLACED, CALLS_CAUGHT}),
    "mount_shadows_route": frozenset({CALLS_CAUGHT}),
    **dict.fromkeys(
        (
            "opaque_router_change",
            "custom_router_class",
            "recursive_router_call",
            "router_call_limit",
            "unimported_router_change",
        ),
        frozenset({ROUTES_CHANGED}),
    ),
}
FRONTEND_CALL_GAP_KINDS = frozenset(FRONTEND_CALL_GAPS)
# Kept in `problems`, which the runtime can import; a test pins it to the keys above.
BACKEND_ROUTE_GAP_KINDS = problems.BACKEND_ROUTE_GAP_KINDS

UP, DOWN = "up", "down"
_RISES, _FALLS, _EITHER = frozenset({UP}), frozenset({DOWN}), frozenset({UP, DOWN})
_ROUTE_FIGURES = ("matched_routes", "matched_calls", "match_rows", "backend_only", "frontend_only", "coverage_pct")

# Which way each effect can move each summary figure from the analysis to the
# app. Starlette serves a request from the first route that matches, so a route
# the analysis misses or misplaces may also take a counted route's callers;
# coverage is matched routes over routes, so a missed route moves it either way.
FIGURE_MOVES: Mapping[str, Mapping[str, frozenset[str]]] = {
    CALLS_MISSED: {
        "frontend_calls": _RISES,
        "matched_routes": _RISES,
        "matched_calls": _RISES,
        "match_rows": _RISES,
        "backend_only": _FALLS,
        "frontend_only": _RISES,
        "coverage_pct": _RISES,
    },
    VARIANTS_DROPPED: {
        "matched_routes": _RISES,
        "matched_calls": _RISES,
        "match_rows": _RISES,
        "backend_only": _FALLS,
        "frontend_only": _RISES,
        "coverage_pct": _RISES,
    },
    CALLS_MISREAD: dict.fromkeys(_ROUTE_FIGURES, _EITHER),
    ROUTES_MISSED: {
        "backend_routes": _RISES,
        "matched_routes": _EITHER,
        "matched_calls": _RISES,
        "match_rows": _EITHER,
        "backend_only": _RISES,
        "frontend_only": _FALLS,
        "coverage_pct": _EITHER,
    },
    ROUTES_MISPLACED: {
        "matched_routes": _EITHER,
        "matched_calls": _RISES,
        "match_rows": _EITHER,
        "backend_only": _EITHER,
        "frontend_only": _FALLS,
        "coverage_pct": _EITHER,
    },
    ROUTES_CHANGED: dict.fromkeys(("backend_routes", *_ROUTE_FIGURES), _EITHER),
    CALLS_CAUGHT: {
        "matched_routes": _FALLS,
        "matched_calls": _FALLS,
        "match_rows": _FALLS,
        "backend_only": _RISES,
        "frontend_only": _RISES,
        "coverage_pct": _FALLS,
    },
}

SUMMARY_FIGURES = (
    "backend_routes",
    "frontend_calls",
    "matched_routes",
    "matched_calls",
    "match_rows",
    "backend_only",
    "frontend_only",
    "coverage_pct",
)


def _gap_row(problem: Mapping) -> dict[str, Any]:
    return {
        "kind": problem.get("kind"),
        "file": problem.get("file"),
        "line": problem.get("line"),
        "message": problem.get("message"),
    }


@dataclass(frozen=True)
class Gap:
    row: dict
    effects: frozenset[str]

    def moves(self, figure: str) -> frozenset[str]:
        return frozenset().union(*(FIGURE_MOVES[e].get(figure, frozenset()) for e in self.effects))


@dataclass(frozen=True)
class OrphanGaps:
    """The findings after which a route figure may differ from the app's.

    An orphan list is confirmed when no finding can take an entry off it, and
    complete when none can add one."""

    found: tuple[Gap, ...] = ()

    def moves(self, figure: str) -> frozenset[str]:
        return frozenset().union(*(g.moves(figure) for g in self.found))

    def rows(self, figure: str, direction: str | None = None) -> list[dict]:
        """The findings that can move ``figure`` (in ``direction``, if given)."""
        return [g.row for g in self.found if (direction in g.moves(figure) if direction else g.moves(figure))]

    def effects(self, figures: Iterable[str]) -> dict[str, list[dict]]:
        """The findings that can move any of ``figures``, by effect, in `FIGURE_MOVES` order."""
        wanted = tuple(figures)
        return {
            effect: rows
            for effect, moves in FIGURE_MOVES.items()
            if any(moves.get(f) for f in wanted) and (rows := [g.row for g in self.found if effect in g.effects])
        }

    def confirmed(self, figure: str, count: int | None) -> bool | None:
        return None if count is None else (count == 0 or DOWN not in self.moves(figure))

    def bound(self, figure: str, count: int | None) -> str | None:
        return direction_bound(count, self.moves(figure))

    def summary_bounds(self, summary: Mapping) -> dict[str, str | None]:
        """How each summary figure bounds the app's figure."""
        moves = {f: set(self.moves(f)) for f in SUMMARY_FIGURES}
        # A count cannot pass the count it is part of.
        for part, whole in (("matched_routes", "backend_routes"), ("matched_calls", "frontend_calls")):
            value, total = summary.get(part), summary.get(whole)
            if value is not None and value == total and UP not in moves[whole]:
                moves[part].discard(UP)
        coverage = summary.get("coverage_pct")
        if coverage is not None and coverage >= 100:
            moves["coverage_pct"].discard(UP)
        return {f: direction_bound(summary.get(f), frozenset(moves[f])) for f in SUMMARY_FIGURES}


# Text for each bound but `exact`.
BOUND_WORDS = {"at_least": "at least", "at_most": "at most", "unbounded": "not bounded"}


def direction_bound(count: float | None, moves: frozenset[str] | set[str]) -> str | None:
    """`exact`, `at_least`, `at_most` or `unbounded`: what a figure taken despite
    findings that can move it says about the app's figure; null for a figure
    not taken. Nothing falls below 0."""
    if count is None:
        return None
    up, down = UP in moves, DOWN in moves and count > 0
    if up and down:
        return "unbounded"
    return "at_least" if up else "at_most" if down else "exact"


def orphan_gaps(data: Mapping) -> OrphanGaps:
    """`OrphanGaps` from a routes result's diagnostics and limitations."""
    found: list[Gap] = []
    for problem in [*data.get("diagnostics", []), *data.get("analysis_limitations", [])]:
        kind = problem.get("kind")
        side = FRONTEND_CALL_GAPS if problem.get("stage") == FRONTEND_ROUTES_STAGE else BACKEND_ROUTE_GAPS
        if kind in side:
            found.append(Gap(_gap_row(problem), side[kind]))
    return OrphanGaps(tuple(found))


@dataclass(frozen=True)
class RouteQuery:
    path: str | None
    handler: str | None
    frontend_func: str | None
    match: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "handler": self.handler, "frontend_func": self.frontend_func, "match": self.match}


@dataclass(frozen=True)
class RouteSummary:
    """Coverage summary (no query given)."""

    summary: dict
    complete: bool
    frontend_problem: str | None
    gaps: OrphanGaps = field(default_factory=OrphanGaps)


@dataclass(frozen=True)
class RouteMatches:
    query: RouteQuery
    matches: Page[dict]
    gaps: OrphanGaps = field(default_factory=OrphanGaps)


@dataclass(frozen=True)
class RouteOrphans:
    """No matched pair, but unmatched routes or calls fit the query."""

    query: RouteQuery
    backend: Page[dict]
    frontend: Page[dict]
    gaps: OrphanGaps = field(default_factory=OrphanGaps)


@dataclass(frozen=True)
class RouteNoMatch:
    query: RouteQuery
    hints: list[str]
    gaps: OrphanGaps = field(default_factory=OrphanGaps)


@dataclass(frozen=True)
class RouteFrontendMissing:
    """A query when the frontend was not analyzed: the backend routes that fit
    it, with no claim either way about their frontend consumers."""

    query: RouteQuery
    backend: Page[dict]
    frontend_problem: str
    gaps: OrphanGaps = field(default_factory=OrphanGaps)


def _frontend_paths(f: Mapping, match: str) -> tuple[str, ...]:
    # The raw source text only makes sense for a substring search.
    return (f["path"], f["raw_path"]) if match == "contains" else (f["path"],)


def _orphan_matches_backend(r: Mapping, query: RouteQuery) -> bool:
    # Filters combine with AND, like the matched-pair filter; an orphan list is
    # only consulted for a filter that names its side.
    if query.handler is None and query.path is None:
        return False
    if query.handler is not None and r["handler"] != query.handler:
        return False
    return query.path is None or path_matches(query.path, r["path"], match=query.match)


def _orphan_matches_frontend(c: Mapping, query: RouteQuery) -> bool:
    if query.frontend_func is None and query.path is None:
        return False
    if query.frontend_func is not None and c["function"] != query.frontend_func:
        return False
    return query.path is None or path_matches(query.path, *_frontend_paths(c, query.match), match=query.match)


RouteMatchResult = RouteSummary | RouteMatches | RouteOrphans | RouteNoMatch | RouteFrontendMissing


def route_match(
    data: Mapping,
    *,
    path: str | None,
    handler: str | None,
    frontend_func: str | None,
    match: str,
    limit: int | None,
) -> RouteMatchResult:
    """Pairs that fit the query, else the orphans that fit it, else hints.

    Without an analyzed frontend there are no pairs and every backend route
    would look like an orphan, so the backend routes that fit are returned as
    such and every frontend-derived count is None.
    """
    unavailable = frontend_unavailable_diagnostic(data)
    if not path and not handler and not frontend_func:
        return _route_summary(data, unavailable)
    query = RouteQuery(path=path, handler=handler, frontend_func=frontend_func, match=match)
    gaps = orphan_gaps(data)
    if unavailable is not None:
        backend = [r for r in data["backend_routes"] if _orphan_matches_backend(r, query)]
        return RouteFrontendMissing(
            query=query,
            backend=page(backend, limit),
            frontend_problem=frontend_problem_text(unavailable),
            gaps=gaps,
        )
    pairs = [m for m in data["matches"] if _pair_matches(m, query)]
    if pairs:
        return RouteMatches(query=query, matches=page(pairs, limit), gaps=gaps)
    backend_orphans = [r for r in data["backend_only"] if _orphan_matches_backend(r, query)]
    frontend_orphans = [c for c in data["frontend_only"] if _orphan_matches_frontend(c, query)]
    if backend_orphans or frontend_orphans:
        return RouteOrphans(
            query=query,
            backend=page(backend_orphans, limit),
            frontend=page(frontend_orphans, limit),
            gaps=gaps,
        )
    return RouteNoMatch(query=query, hints=_route_no_match_hints(data, query), gaps=gaps)


def _route_summary(data: Mapping, unavailable: Mapping | None) -> RouteSummary:
    summary = dict(data["summary"])
    if unavailable is not None:
        summary.update(dict.fromkeys(FRONTEND_SUMMARY_KEYS))
    return RouteSummary(
        summary=summary,
        complete=data["complete"],
        frontend_problem=frontend_problem_text(unavailable) if unavailable else None,
        gaps=orphan_gaps(data),
    )


def _pair_matches(m: Mapping, query: RouteQuery) -> bool:
    """Every given filter must fit the pair (AND)."""
    b, f = m["backend"], m["frontend"]
    if query.path and not path_matches(query.path, b["path"], *_frontend_paths(f, query.match), match=query.match):
        return False
    if query.handler and b["handler"] != query.handler:
        return False
    return not query.frontend_func or f["function"] == query.frontend_func


def _route_no_match_hints(data: Mapping, query: RouteQuery) -> list[str]:
    known_handlers = [r["handler"] for r in data["backend_routes"]]
    known_funcs = [c["function"] for c in data["frontend_calls"] if c["function"]]
    return no_match_hints(
        query.path,
        query.match,
        [
            unknown_name_hint("handler", query.handler, known_handlers),
            unknown_name_hint("frontend function", query.frontend_func, known_funcs),
            slash_hint(query.path, query.match, (r["path"] for r in data["backend_routes"])),
        ],
    )


# ── Routes: route_unmatched ──────────────────────────────────────


@dataclass(frozen=True)
class UnmatchedRoutes:
    """Orphans per requested side; a side that was not requested is None."""

    side: str
    backend: Page[dict] | None
    frontend: Page[dict] | None
    gaps: OrphanGaps = field(default_factory=OrphanGaps)


def route_unmatched(data: Mapping, *, side: str, limit: int | None) -> UnmatchedRoutes:
    backend = page(list(data["backend_only"]), limit) if side in ("backend", "both") else None
    frontend = page(list(data["frontend_only"]), limit) if side in ("frontend", "both") else None
    return UnmatchedRoutes(side=side, backend=backend, frontend=frontend, gaps=orphan_gaps(data))


# ── JSX usage ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JsxUsageResult:
    component: str
    usages: Page[Any]
    file_count: int
    root: str
    include_self_close: bool
    include_tests: bool
    problems: list[dict]
    complete: bool

    @property
    def analysis(self) -> dict[str, Any]:
        diagnostics = [p for p in self.problems if p.get("category")]
        limitations = [p for p in self.problems if not p.get("category")]
        return {"complete": self.complete, "diagnostics": diagnostics, "analysis_limitations": limitations}


def problem_dicts(items: Iterable[Any]) -> list[dict[str, Any]]:
    """Problems as JSON dicts; analyzer JSON already holds dicts."""
    return [dict(p.as_dict()) if isinstance(p, problems.Problem) else dict(p) for p in items]


def jsx_usages(
    component_name: str,
    usages: Sequence[Any],
    *,
    limit: int | None,
    root: str,
    include_self_close: bool,
    include_tests: bool,
    found_problems: Iterable[Any] = (),
) -> JsxUsageResult:
    rows = problem_dicts(found_problems)
    return JsxUsageResult(
        component=component_name,
        usages=page(list(usages), limit),
        file_count=len({u.file for u in usages}),
        root=root,
        include_self_close=include_self_close,
        include_tests=include_tests,
        problems=rows,
        complete=not any(p.get("category") for p in rows),
    )


# ── Symbol resolution ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolRow:
    candidate: Any  # symbol_resolve.SymbolCandidate
    file_hint_match: bool


@dataclass(frozen=True)
class SymbolResolution:
    name: str
    side: str
    kind: str | None
    file_hint: str | None
    candidates: Page[SymbolRow]
    complete: bool
    problems: list[dict] = field(default_factory=list)
    frontend_problem: str | None = None

    @property
    def analysis(self) -> dict[str, Any]:
        diagnostics = [p for p in self.problems if p.get("category")]
        limitations = [p for p in self.problems if not p.get("category")]
        return {"complete": self.complete, "diagnostics": diagnostics, "analysis_limitations": limitations}


def _problem_text(problem: Any) -> str | None:
    if problem is None:
        return None
    if isinstance(problem, str):
        return problem
    message = getattr(problem, "message", None)
    fix = getattr(problem, "fix", None)
    if message and fix:
        return f"{message}. Fix: {fix}"
    return str(message or problem)


def symbol_resolution(
    name: str,
    result: Any,
    *,
    side: str,
    kind: str | None,
    file_hint: str | None,
    limit: int | None,
) -> SymbolResolution:
    """``result`` is a `symbol_resolve.SymbolResult`: candidates, complete,
    problems and frontend_problem (set when ``side="both"`` lost the frontend)."""
    candidates = list(result.candidates)
    visible, omitted = bounded(candidates, limit)
    rows = [SymbolRow(c, bool(file_hint) and file_hint_matches(c.file, file_hint)) for c in visible]
    frontend_problem = _problem_text(result.frontend_problem)
    return SymbolResolution(
        name=name,
        side=side,
        kind=kind,
        file_hint=file_hint,
        candidates=Page(rows, len(candidates), omitted),
        complete=bool(result.complete) and frontend_problem is None,
        problems=problem_dicts(result.problems),
        frontend_problem=frontend_problem,
    )
