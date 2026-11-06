"""Unit tests for the pure `queries` functions over DI JSON v2 and routes JSON v2."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp_fixtures import (
    DOC_LOOKUP_OTHER_FILE,
    DOC_LOOKUP_ROUTE_FILE,
    TS_FIX,
    TS_MISSING,
    FakeCandidate,
    FakeSymbolResult,
    FakeUsage,
    backend_side,
    dep,
    di_result,
    di_route,
    doc_lookup_report,
    doc_lookup_two_owner_report,
    frontend_side,
    frontend_unavailable_diagnostic,
    limitation,
    pair,
    read_failed,
    routes_result,
    use,
)

from code_intel import doc_presenters, doc_queries, problems, queries
from code_intel.doc_report import SymbolRow

GET_DB = "service_backend.core.database:get_db"
PERM = "service_backend.core.auth:require_any_permission"
TEL_A = "service_backend.api.routes.squad_heartbeat_ws:_get_heartbeat_manager"
TEL_B = "service_backend.api.routes.heartbeat_ws:_get_heartbeat_manager"


def _di():
    return di_result(
        [
            dep(GET_DB, line=83),
            dep(PERM, kind="factory_result", line=5, sub=[GET_DB], trans=[GET_DB], aliases=["_require_list_access"]),
            dep(TEL_A, file="backend/service_backend/api/routes/squad_heartbeat_ws.py", line=64),
            dep(TEL_B, file="backend/service_backend/api/routes/heartbeat_ws.py", line=62),
        ],
        [
            di_route("list_users", "/users", deps=[use(GET_DB, "get_db", "direct", param="db")]),
            di_route(
                "create_user",
                "/users",
                method="POST",
                line=20,
                deps=[use(PERM, "require_any_permission(USER_CREATE)", "route decorator")],
                all_deps=[PERM, GET_DB],
            ),
            di_route(
                "list_locations",
                "/locations",
                deps=[use(PERM, "require_any_permission(…)", "route decorator", alias="_require_list_access")],
                all_deps=[PERM, GET_DB],
            ),
            di_route("audit_users", "/admin/audit/users", deps=[use(PERM, "require_any_permission(AUDIT)", "router")]),
            di_route("app_guarded", "/health", deps=[use(GET_DB, "get_db", "app")]),
            di_route("squad_ws", "/ws/squad", method="WS", deps=[use(TEL_A, "_get_heartbeat_manager", "direct")]),
            di_route(
                "article_ws",
                "/ws/articles/{article_id}",
                method="WS",
                deps=[use(TEL_B, "_get_heartbeat_manager", "direct")],
            ),
        ],
    )


def _routes():
    return routes_result(
        [pair("/articles/{article_id}", "get_article", "getArticle", def_line=8)],
        backend_only=[
            backend_side("GET", "/health", "health"),
            backend_side("GET", "/admin/stats", "stats"),
            backend_side("GET", "/reports/", "list_reports"),
        ],
        frontend_only=[frontend_side("GET", "/ghost", "ghost", line=3)],
    )


# ── Paths and limits ─────────────────────────────────────────────────────


def test_path_matches_modes() -> None:
    assert queries.path_matches("/articles/{id}", "/articles/{article_id}")
    assert queries.path_matches("/users", "/users?x=1")
    assert not queries.path_matches("/users/", "/users"), "trailing slashes are significant"
    assert not queries.path_matches("/users", "/admin/audit/users")
    assert queries.path_matches("/users", "/admin/audit/users", match="suffix")
    assert not queries.path_matches("/users", "/superusers", match="suffix"), "suffix is segment-aware"
    assert queries.path_matches("users", "/admin/users", match="suffix")
    assert queries.path_matches("/users", "/admin/audit/users", match="auto")
    assert queries.path_matches("audit", "/admin/audit/users", match="contains")
    assert queries.path_matches("{id}", "/articles/{article_id}", match="contains"), "parameter names are ignored"
    assert queries.path_matches("/articles/{id:int}", "/articles/{article_id}", match="contains")
    assert queries.path_matches("?page=", "/articles?page=2", match="contains"), "the query string is kept"
    with pytest.raises(ValueError, match="bogus"):
        queries.path_matches("/users", "/users", match="bogus")


def test_page_bounds_and_counts() -> None:
    assert queries.page([1, 2, 3], None) == queries.Page([1, 2, 3], 3, 0)
    assert queries.page([1, 2, 3], 2) == queries.Page([1, 2], 3, 1)
    assert queries.page([1, 2, 3], 0) == queries.Page([], 3, 3)


def test_page_rejects_negative_limits() -> None:
    """A negative limit is an argument error the tools reject first; the query
    layer refuses it instead of reading it as 0."""
    with pytest.raises(AssertionError, match="limit"):
        queries.page([1, 2, 3], -1)


def test_unknown_name_hint_suggests_close_names() -> None:
    assert queries.unknown_name_hint("handler", "list_user", ["list_users"]) == (
        "Unknown handler 'list_user'; did you mean list_users?"
    )
    assert queries.unknown_name_hint("handler", "list_users", ["list_users"]) is None
    assert queries.no_match_hints("/x", "exact", [None]) == ["Try match='suffix' for loose path matching."]
    assert queries.no_match_hints(None, "exact", ["hint"]) == ["hint"]


# ── DI ───────────────────────────────────────────────────────────────────


def test_di_deps_lists_targets_sorted_with_ids_for_colliding_names() -> None:
    result = queries.di_deps(_di(), handler=None, path=None, method=None, match="exact", limit=None)
    assert isinstance(result, queries.DependencyListing)
    rows = result.functions.items
    assert [r["name"] for r in rows] == [
        "_get_heartbeat_manager",
        "_get_heartbeat_manager",
        "get_db",
        "require_any_permission",
    ]
    assert [r["label"] for r in rows[:2]] == sorted([TEL_A, TEL_B]), "colliding names show their ids"
    assert rows[2]["label"] == "get_db"
    perm = rows[3]
    assert perm["sub_dep_labels"] == ["get_db"]
    assert result.route_count == 7

    limited = queries.di_deps(_di(), handler=None, path=None, method=None, match="exact", limit=1)
    assert isinstance(limited, queries.DependencyListing)
    assert (len(limited.functions.items), limited.functions.omitted) == (1, 3)


def test_di_deps_filters_by_handler_id_path_and_method() -> None:
    by_path = queries.di_deps(_di(), handler=None, path="/users", method="POST", match="exact", limit=None)
    assert isinstance(by_path, queries.DiRouteSelection)
    assert [r["handler"] for r in by_path.routes.items] == ["create_user"]
    assert by_path.routes.items[0]["all_dep_labels"] == ["require_any_permission", "get_db"]

    by_id = queries.di_deps(
        _di(), handler="service_backend.api.routes.r:list_users", path=None, method=None, match="exact", limit=None
    )
    assert isinstance(by_id, queries.DiRouteSelection)
    assert [r["handler"] for r in by_id.routes.items] == ["list_users"]


def test_di_deps_method_hint_names_the_methods_that_exist() -> None:
    """A handler that exists, but not for the requested method, says which methods it has."""
    result = queries.di_deps(_di(), handler=None, path="/users", method="PUT", match="exact", limit=None)
    assert isinstance(result, queries.DiRouteSelection)
    assert result.routes.total == 0
    assert result.hints == ["/users exists for GET, POST; none for PUT."]


def test_di_deps_no_match_carries_name_and_slash_hints() -> None:
    result = queries.di_deps(_di(), handler="list_user", path=None, method=None, match="exact", limit=None)
    assert isinstance(result, queries.DiRouteSelection)
    assert result.routes.total == 0
    assert result.hints[0].startswith("Unknown handler 'list_user'; did you mean list_users")
    slash = queries.di_deps(_di(), handler=None, path="/users/", method=None, match="exact", limit=None)
    assert isinstance(slash, queries.DiRouteSelection)
    assert "A route exists at '/users' (trailing slash differs; slashes are significant)." in slash.hints


def test_di_reverse_matches_id_then_name_then_alias() -> None:
    by_id = queries.di_reverse(_di(), GET_DB, limit=None)
    assert isinstance(by_id, queries.DependencyUsage)
    assert (by_id.matched_on, [g.target["id"] for g in by_id.groups]) == ("id", [GET_DB])

    by_name = queries.di_reverse(_di(), "require_any_permission(X)", limit=None)
    assert isinstance(by_name, queries.DependencyUsage)
    assert (by_name.matched_on, by_name.query_name) == ("name", "require_any_permission")

    by_alias = queries.di_reverse(_di(), "_require_list_access", limit=None)
    assert isinstance(by_alias, queries.DependencyUsage)
    assert by_alias.matched_on == "alias"
    assert [g.target["id"] for g in by_alias.groups] == [PERM]


def test_di_reverse_labels_how_each_route_gets_the_dependency() -> None:
    result = queries.di_reverse(_di(), "require_any_permission", limit=None)
    assert isinstance(result, queries.DependencyUsage)
    (group,) = result.groups
    vias = {r["handler"]: (r["via"], r["via_dep"], r["via_alias"]) for r in group.routes}
    assert vias == {
        "create_user": ("route decorator", "require_any_permission(USER_CREATE)", None),
        "list_locations": ("route decorator", "require_any_permission(…)", "_require_list_access"),
        "audit_users": ("router", "require_any_permission(AUDIT)", None),
    }
    assert group.via_counts == {"route decorator": 2, "router": 1}

    db = queries.di_reverse(_di(), "get_db", limit=None)
    assert isinstance(db, queries.DependencyUsage)
    assert db.groups[0].via_counts == {"direct": 1, "transitive": 2, "app": 1}


def test_di_reverse_groups_same_name_definitions_and_limits_across_groups() -> None:
    """`_get_heartbeat_manager` is two targets; usages stay per definition."""
    result = queries.di_reverse(_di(), "_get_heartbeat_manager", limit=None)
    assert isinstance(result, queries.DependencyUsage)
    assert [g.target["id"] for g in result.groups] == sorted([TEL_A, TEL_B])
    assert [[r["handler"] for r in g.routes] for g in result.groups] == [["article_ws"], ["squad_ws"]]
    assert [g.target["line"] for g in result.groups] == [62, 64]

    limited = queries.di_reverse(_di(), "_get_heartbeat_manager", limit=1)
    assert isinstance(limited, queries.DependencyUsage)
    assert [(len(g.routes), g.route_count, g.omitted) for g in limited.groups] == [(1, 1, 0), (0, 1, 1)]


def test_di_reverse_unknown_dependency_suggests_names_and_aliases() -> None:
    result = queries.di_reverse(_di(), "get_dbb", limit=None)
    assert result == queries.UnknownDependency(dep_function="get_dbb", suggestions=["get_db"], known_count=4)
    alias = queries.di_reverse(_di(), "_require_list_acess", limit=None)
    assert isinstance(alias, queries.UnknownDependency)
    assert alias.suggestions == ["_require_list_access"]


# ── Routes ───────────────────────────────────────────────────────────────


def test_route_match_summary_without_a_query() -> None:
    result = queries.route_match(_routes(), path=None, handler=None, frontend_func=None, match="exact", limit=50)
    assert isinstance(result, queries.RouteSummary)
    assert result.summary["backend_routes"] == 4
    assert (result.complete, result.frontend_problem) == (True, None)


def test_route_match_summary_names_an_unavailable_frontend() -> None:
    data = routes_result(
        backend_only=[backend_side("GET", "/health", "health")],
        complete=False,
        diagnostics=[frontend_unavailable_diagnostic()],
    )
    result = queries.route_match(data, path=None, handler=None, frontend_func=None, match="exact", limit=50)
    assert isinstance(result, queries.RouteSummary)
    assert result.complete is False
    assert result.frontend_problem == f"{TS_MISSING} not found. Fix: {TS_FIX}"
    assert result.summary["coverage_pct"] is None


def test_route_match_finds_pairs_param_agnostically() -> None:
    result = queries.route_match(
        _routes(), path="/articles/{id}", handler=None, frontend_func=None, match="exact", limit=50
    )
    assert isinstance(result, queries.RouteMatches)
    assert [m["backend"]["handler"] for m in result.matches.items] == ["get_article"]
    by_func = queries.route_match(
        _routes(), path=None, handler=None, frontend_func="getArticle", match="exact", limit=50
    )
    assert isinstance(by_func, queries.RouteMatches)
    assert by_func.matches.total == 1


def test_route_match_falls_back_to_orphans_with_and_semantics() -> None:
    result = queries.route_match(_routes(), path="/admin", handler=None, frontend_func=None, match="contains", limit=50)
    assert isinstance(result, queries.RouteOrphans)
    assert [r["handler"] for r in result.backend.items] == ["stats"]
    assert result.frontend.total == 0

    both = queries.route_match(_routes(), path="/health", handler="stats", frontend_func=None, match="exact", limit=50)
    assert isinstance(both, queries.RouteNoMatch), "an orphan matching only one filter must not surface"

    func = queries.route_match(_routes(), path=None, handler=None, frontend_func="ghost", match="exact", limit=50)
    assert isinstance(func, queries.RouteOrphans)
    assert [c["function"] for c in func.frontend.items] == ["ghost"]


def test_route_match_no_match_hints_unknown_names_and_slashes() -> None:
    result = queries.route_match(_routes(), path=None, handler=None, frontend_func="getArticl", match="exact", limit=50)
    assert isinstance(result, queries.RouteNoMatch)
    assert result.hints == ["Unknown frontend function 'getArticl'; did you mean getArticle?"]

    slash = queries.route_match(_routes(), path="/reports", handler=None, frontend_func=None, match="exact", limit=50)
    assert isinstance(slash, queries.RouteNoMatch)
    assert "A route exists at '/reports/' (trailing slash differs; slashes are significant)." in slash.hints


def test_route_unmatched_only_returns_requested_sides() -> None:
    backend = queries.route_unmatched(_routes(), side="backend", limit=1)
    assert backend.frontend is None
    assert backend.backend == queries.Page([_routes()["backend_only"][0]], 3, 2)
    both = queries.route_unmatched(_routes(), side="both", limit=None)
    assert both.backend is not None and both.frontend is not None


def test_frontend_unavailable_diagnostic_lookup() -> None:
    assert queries.frontend_unavailable_diagnostic(_routes()) is None
    data = routes_result(complete=False, diagnostics=[frontend_unavailable_diagnostic()])
    found = queries.frontend_unavailable_diagnostic(data)
    assert found is not None and found["kind"] == "frontend_unavailable"


# ── JSX and symbols ──────────────────────────────────────────────────────


def test_jsx_usages_counts_files_across_all_usages_not_the_page() -> None:
    usages = [FakeUsage("a.tsx", 1), FakeUsage("a.tsx", 2), FakeUsage("b.tsx", 3)]
    result = queries.jsx_usages(
        "Widget", usages, limit=1, root="frontend", include_self_close=True, include_tests=False
    )
    assert (result.usages.total, len(result.usages.items), result.usages.omitted) == (3, 1, 2)
    assert result.file_count == 2
    assert result.complete is True


def test_jsx_usages_diagnostics_make_the_result_incomplete() -> None:
    result = queries.jsx_usages(
        "Widget",
        [],
        limit=None,
        root="frontend",
        include_self_close=True,
        include_tests=False,
        found_problems=[read_failed("frontend/a.tsx")],
    )
    assert result.complete is False
    assert result.analysis["diagnostics"][0]["file"] == "frontend/a.tsx"


def _candidate(file: str) -> FakeCandidate:
    return FakeCandidate("get_db", file, 1, 2, "function", "def get_db()", "python")


def test_symbol_resolution_flags_file_hint_matches_by_substring_or_glob() -> None:
    found = FakeSymbolResult([_candidate("backend/core/db.py"), _candidate("backend/other.py")])
    result = queries.symbol_resolution("get_db", found, side="both", kind=None, file_hint="core", limit=None)
    assert [row.file_hint_match for row in result.candidates.items] == [True, False]
    globbed = queries.symbol_resolution(
        "get_db", found, side="both", kind=None, file_hint="backend/*/db.py", limit=None
    )
    assert [row.file_hint_match for row in globbed.candidates.items] == [True, False]
    no_hint = queries.symbol_resolution("get_db", found, side="both", kind=None, file_hint=None, limit=1)
    assert [row.file_hint_match for row in no_hint.candidates.items] == [False]
    assert no_hint.candidates.omitted == 1


def test_symbol_resolution_carries_completeness_and_frontend_problem() -> None:
    found = FakeSymbolResult(
        [_candidate("backend/core/db.py")], complete=False, problems=[read_failed()], frontend_problem="node not found"
    )
    result = queries.symbol_resolution("get_db", found, side="both", kind=None, file_hint=None, limit=None)
    assert result.complete is False
    assert result.frontend_problem == "node not found"
    assert result.analysis["diagnostics"][0]["kind"] == "read_failed"


# ── Frontend not analyzed and dependency queries ─────────────────────────


def _no_frontend():
    return routes_result(
        backend_only=[backend_side("GET", "/articles", "list_articles"), backend_side("GET", "/health", "health")],
        complete=False,
        diagnostics=[frontend_unavailable_diagnostic()],
    )


def test_route_match_without_a_frontend_returns_fitting_routes_not_orphans() -> None:
    result = queries.route_match(
        _no_frontend(), path=None, handler="list_articles", frontend_func=None, match="exact", limit=50
    )
    assert isinstance(result, queries.RouteFrontendMissing)
    assert [r["handler"] for r in result.backend.items] == ["list_articles"]
    assert result.frontend_problem == (f"{TS_MISSING} not found. Fix: {TS_FIX}")


def test_route_summary_without_a_frontend_nulls_every_frontend_count() -> None:
    result = queries.route_match(_no_frontend(), path=None, handler=None, frontend_func=None, match="exact", limit=50)
    assert isinstance(result, queries.RouteSummary)
    assert result.summary["backend_routes"] == 2
    assert all(result.summary[k] is None for k in queries.FRONTEND_SUMMARY_KEYS)


def test_frontend_problem_text_names_a_missing_path_the_message_omits() -> None:
    diag = {"message": "TypeScript could not be loaded. Fix: run npm ci", "missing": "frontend/x", "fix": "run npm ci"}
    assert (
        queries.frontend_problem_text(diag) == "TypeScript could not be loaded (missing: frontend/x). Fix: run npm ci"
    )


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("get_db", "get_db"),
        (" get_db ", " get_db "),
        ("require_permission(A, B)", "require_permission"),
        ("require_permission()", "require_permission"),
        ("f(g(x))", "f"),
        ("get_db(garbage", None),
        ("get_db)", None),
        ("(x)", None),
        ("get_db(x) trailing", None),
    ],
)
def test_bare_dep_name_parses_only_names_and_whole_factory_calls(text: str, name: str | None) -> None:
    assert queries.bare_dep_name(text) == name


@pytest.mark.parametrize(
    ("text", "is_id"),
    [
        ("pkg.deps:get_db", True),
        ("pkg.svc:Svc()", True),
        ("pkg.svc:Svc().get_items", True),
        ("get_db", False),
        ("require_permission(A)", False),
        ('require_permission("scope:view")', False),
    ],
)
def test_is_dep_id_needs_a_colon_before_any_parenthesis(text: str, is_id: bool) -> None:
    assert queries.is_dep_id(text) is is_id


def test_an_id_query_matches_only_that_id() -> None:
    assert queries.find_dependency_targets({}, "pkg.deps:get_db") == ("id", "pkg.deps:get_db", [])
    functions = {GET_DB: dep(GET_DB)}
    assert queries.find_dependency_targets(functions, "other.module:get_db") == ("id", "other.module:get_db", [])


def test_di_reverse_route_count_spans_every_group() -> None:
    limited = queries.di_reverse(_di(), "_get_heartbeat_manager", limit=1)
    assert isinstance(limited, queries.DependencyUsage)
    assert limited.route_count == 2


# ── Orphan gaps ──────────────────────────────────────────────────────────


def test_orphan_gap_kinds_are_registered_and_the_stage_is_the_routes_scan() -> None:
    from code_intel import route_map

    registered = set(problems.DIAGNOSTIC_KINDS) | set(problems.LIMITATION_KINDS)
    assert queries.FRONTEND_CALL_GAP_KINDS <= registered
    assert queries.BACKEND_ROUTE_GAP_KINDS <= registered
    assert queries.FRONTEND_ROUTES_STAGE == route_map.STAGE


@pytest.mark.parametrize(
    ("kind", "stage", "effects"),
    [
        ("read_failed", "frontend routes", {"calls_missed"}),
        ("decode_failed", "frontend routes", {"calls_missed", "calls_misread"}),
        ("parse_failed", "frontend routes", {"calls_missed"}),
        ("analysis_failed", "frontend routes", {"calls_missed"}),
        ("frontend_client_module_missing", "frontend routes", {"calls_missed"}),
        ("unparsed_frontend_callsite", "frontend routes", {"calls_missed"}),
        ("unresolved_frontend_path", "frontend routes", {"calls_missed"}),
        ("url_variants_truncated", "frontend routes", {"variants_dropped"}),
        ("typescript_syntax_error", "frontend routes", {"calls_missed", "calls_misread"}),
        ("unresolved_http_method", "frontend routes", None),
        ("unresolved_prop_value", "frontend routes", None),
        ("frontend_unavailable", "frontend routes", None),
        ("parse_failed", "load backend sources", {"routes_missed", "calls_caught"}),
        ("decode_failed", "load backend sources", {"routes_missed", "calls_caught"}),
        ("unresolved_app", "build router graph", {"routes_missed", "calls_caught"}),
        ("unresolved_include", "build router graph", {"routes_missed", "calls_caught"}),
        ("router_include_cycle", "build router graph", {"routes_missed"}),
        ("unresolved_route_router", "build router graph", {"routes_missed"}),
        ("unresolved_mount_path", "build router graph", {"routes_misplaced", "calls_caught"}),
        ("unsupported_route_registration", "build router graph", {"routes_missed", "calls_caught"}),
        ("unresolved_include_prefix", "build router graph", {"routes_misplaced"}),
        ("unresolved_route_prefix", "build router graph", {"routes_misplaced"}),
        ("unresolved_route_path", "analyze routers", {"routes_misplaced"}),
        ("opaque_router_call", "build router graph", {"routes_missed", "calls_caught"}),
        ("opaque_router_change", "build router graph", {"routes_changed"}),
        ("unimported_router_change", "build router graph", {"routes_changed"}),
        ("custom_router_class", "build router graph", {"routes_changed"}),
        ("router_call_limit", "build router graph", {"routes_changed"}),
        ("unresolved_route_methods", "analyze routers", None),
        ("unmounted_router", "build router graph", None),
        ("unresolved_dependency", "resolve dependencies", None),
    ],
)
def test_orphan_gaps_classify_each_finding_by_kind_and_stage(kind: str, stage: str, effects: set[str] | None) -> None:
    finding = {"kind": kind, "stage": stage, "file": "f", "line": 3, "message": "m", "category": "content_lost"}
    gaps = queries.orphan_gaps({"diagnostics": [finding], "analysis_limitations": []})
    row = {"kind": kind, "file": "f", "line": 3, "message": "m"}
    assert [(g.row, set(g.effects)) for g in gaps.found] == ([] if effects is None else [(row, effects)])


@pytest.mark.parametrize(
    ("count", "moves", "bound"),
    [
        (None, {"up", "down"}, None),
        (3, set(), "exact"),
        (3, {"up"}, "at_least"),
        (3, {"down"}, "at_most"),
        (3, {"up", "down"}, "unbounded"),
        (0, {"down"}, "exact"),
        (0, {"up", "down"}, "at_least"),
    ],
)
def test_direction_bound_says_which_way_findings_can_move_a_figure(
    count: int | None, moves: set[str], bound: str | None
) -> None:
    assert queries.direction_bound(count, frozenset(moves)) == bound


_SUMMARY = {
    "backend_routes": 4,
    "frontend_calls": 3,
    "matched_routes": 2,
    "matched_calls": 2,
    "match_rows": 2,
    "backend_only": 2,
    "frontend_only": 1,
    "coverage_pct": 50.0,
}


def _gaps(*effects: str) -> queries.OrphanGaps:
    return queries.OrphanGaps(
        tuple(queries.Gap({"kind": e, "file": "f", "line": 1, "message": "m"}, frozenset({e})) for e in effects)
    )


@pytest.mark.parametrize(
    ("effects", "expected"),
    [
        ((), dict.fromkeys(_SUMMARY, "exact")),
        (
            ("calls_missed",),
            {
                "backend_routes": "exact",
                "frontend_calls": "at_least",
                "matched_routes": "at_least",
                "matched_calls": "at_least",
                "match_rows": "at_least",
                "backend_only": "at_most",
                "frontend_only": "at_least",
                "coverage_pct": "at_least",
            },
        ),
        (
            ("variants_dropped",),
            {
                "backend_routes": "exact",
                "frontend_calls": "exact",
                "matched_routes": "at_least",
                "matched_calls": "at_least",
                "match_rows": "at_least",
                "backend_only": "at_most",
                "frontend_only": "at_least",
                "coverage_pct": "at_least",
            },
        ),
        (
            ("calls_misread",),
            {
                "backend_routes": "exact",
                "frontend_calls": "exact",
                **dict.fromkeys(
                    ("matched_routes", "matched_calls", "match_rows", "backend_only", "frontend_only", "coverage_pct"),
                    "unbounded",
                ),
            },
        ),
        (
            ("routes_missed",),
            {
                "backend_routes": "at_least",
                "frontend_calls": "exact",
                "matched_routes": "unbounded",
                "matched_calls": "at_least",
                "match_rows": "unbounded",
                "backend_only": "at_least",
                "frontend_only": "at_most",
                "coverage_pct": "unbounded",
            },
        ),
        (
            ("routes_misplaced",),
            {
                "backend_routes": "exact",
                "frontend_calls": "exact",
                "matched_routes": "unbounded",
                "matched_calls": "at_least",
                "match_rows": "unbounded",
                "backend_only": "unbounded",
                "frontend_only": "at_most",
                "coverage_pct": "unbounded",
            },
        ),
        (
            ("calls_caught",),
            {
                "backend_routes": "exact",
                "frontend_calls": "exact",
                "matched_routes": "at_most",
                "matched_calls": "at_most",
                "match_rows": "at_most",
                "backend_only": "at_least",
                "frontend_only": "at_least",
                "coverage_pct": "at_most",
            },
        ),
        (
            ("routes_changed",),
            {"frontend_calls": "exact", **{k: "unbounded" for k in _SUMMARY if k != "frontend_calls"}},
        ),
        (
            ("calls_missed", "routes_missed"),
            {
                "backend_routes": "at_least",
                "frontend_calls": "at_least",
                "matched_routes": "unbounded",
                "matched_calls": "at_least",
                "match_rows": "unbounded",
                "backend_only": "unbounded",
                "frontend_only": "unbounded",
                "coverage_pct": "unbounded",
            },
        ),
    ],
)
def test_summary_bounds_follow_what_each_finding_can_do(effects: tuple[str, ...], expected: dict) -> None:
    assert _gaps(*effects).summary_bounds(_SUMMARY) == expected


def test_summary_bounds_stop_at_the_ends_of_a_figure() -> None:
    every_route_called = {**_SUMMARY, "matched_routes": 4, "backend_only": 0, "coverage_pct": 100.0}
    calls = _gaps("calls_missed").summary_bounds(every_route_called)
    assert (calls["matched_routes"], calls["backend_only"], calls["coverage_pct"]) == ("exact", "exact", "exact")
    routes = _gaps("routes_missed").summary_bounds(every_route_called)
    assert (routes["matched_routes"], routes["coverage_pct"]) == ("unbounded", "at_most"), (
        "a missed route may be uncalled"
    )
    none_called = {**_SUMMARY, "matched_routes": 0, "coverage_pct": 0.0}
    assert _gaps("routes_missed").summary_bounds(none_called)["coverage_pct"] == "at_least"
    every_call_matched = {**_SUMMARY, "matched_calls": 3, "frontend_only": 0}
    assert _gaps("routes_missed").summary_bounds(every_call_matched)["matched_calls"] == "exact"
    assert set(_gaps("calls_missed").summary_bounds(dict.fromkeys(_SUMMARY)).values()) == {None}


def test_an_orphan_list_is_confirmed_unless_an_entry_can_leave_it() -> None:
    misplaced = _gaps("routes_misplaced")
    assert misplaced.confirmed("backend_only", 2) is False, "a misplaced route may have a caller at its real path"
    assert misplaced.confirmed("frontend_only", 1) is False
    assert misplaced.confirmed("backend_only", 0) is True, "no orphan is never an unconfirmed claim"
    assert misplaced.confirmed("backend_only", None) is None
    missed = _gaps("routes_missed")
    assert (missed.confirmed("backend_only", 2), missed.bound("backend_only", 2)) == (True, "at_least")
    assert [r["kind"] for r in _gaps("calls_missed", "routes_missed").rows("backend_only", "down")] == ["calls_missed"]


# ── Docs lookup ──────────────────────────────────────────────────────────


@pytest.fixture
def doc_report(tmp_path):
    return doc_lookup_report(tmp_path / "repo")


def _mentions(result: doc_queries.DocRefs) -> list[tuple[str, int, str, str]]:
    assert result.mentions is not None
    return [(r["file"], r["line"], r["kind"], r["text"]) for r in result.mentions.items]


def test_a_file_query_finds_paths_links_routes_it_handles_and_symbols_it_defines(doc_report) -> None:
    result = doc_queries.doc_refs_lookup(doc_report, file=DOC_LOOKUP_ROUTE_FILE, limit=None)
    assert (result.kind, result.value, result.method) == ("file", DOC_LOOKUP_ROUTE_FILE, None)
    assert _mentions(result) == [
        ("docs/a.md", 3, "link", "../backend/service_backend/api/routes/r.py"),
        ("docs/a.md", 3, "path", "backend/service_backend/api/routes/r.py"),
        ("docs/a.md", 5, "route", "/items/{item_id}/checkout"),
        ("docs/a.md", 5, "route", "GET /items/{id}/checkout"),
        ("docs/a.md", 5, "route", "POST /items/{id}/checkout"),
        ("docs/a.md", 7, "symbol", "Holder"),
        ("docs/a.md", 7, "symbol", "Holder.status"),
        ("docs/a.md", 7, "symbol", "checkout()"),
    ], "a local variable defined in the file is not a mention of it"
    assert result.doc_count == 1
    assert result.mentions is not None
    ambiguous = {r["text"]: r["ambiguous"] for r in result.mentions.items}
    assert ambiguous["Holder"] is True, "Holder is also defined in other.py"
    assert (ambiguous["Holder.status"], ambiguous["checkout()"]) == (False, False)
    assert ambiguous["POST /items/{id}/checkout"] is None, "ambiguous is only about symbols"
    assert result.mentions is not None
    wrong_method = next(r for r in result.mentions.items if r["text"] == "GET /items/{id}/checkout")
    assert (wrong_method["status"], wrong_method["message"]) == (
        "method_differs",
        "route method differs: GET /items/{id}/checkout (served: POST)",
    )
    assert (result.definitions, result.links, result.inbound, result.reachable) == (None, None, None, None)


def test_a_file_query_on_a_directory_or_a_deleted_file(doc_report) -> None:
    directory = doc_queries.doc_refs_lookup(doc_report, file="backend/service_backend", limit=None)
    assert ("docs/a.md", 9, "path", "backend/service_backend/gone.py") in _mentions(directory)
    assert ("docs/a.md", 7, "symbol", "Holder") in _mentions(directory)
    assert directory.mentions is not None
    holder = next(r for r in directory.mentions.items if r["text"] == "Holder")
    assert holder["ambiguous"] is False, "both definitions are under the directory"
    gone = doc_queries.doc_refs_lookup(doc_report, file="backend/service_backend/gone.py", limit=None)
    assert gone.mentions is not None
    assert [(r["text"], r["status"]) for r in gone.mentions.items] == [("backend/service_backend/gone.py", "missing")]
    other = doc_queries.doc_refs_lookup(doc_report, file=DOC_LOOKUP_OTHER_FILE, limit=None)
    assert other.mentions is not None
    assert [(r["text"], r["ambiguous"]) for r in other.mentions.items] == [("Holder", True)]


def test_a_file_query_on_a_doc_skips_its_own_anchor_links(doc_report) -> None:
    result = doc_queries.doc_refs_lookup(doc_report, file="docs/a.md", limit=None)
    assert _mentions(result) == [
        ("docs/README.md", 3, "link", "a.md"),
        ("docs/b.md", 3, "link", "a.md#a"),
        ("docs/plans/p.md", 3, "link", "../a.md"),
    ]


def test_a_route_query_matches_by_path_and_compatible_method(doc_report) -> None:
    result = doc_queries.doc_refs_lookup(doc_report, route="post /items/{x}/checkout", limit=None)
    assert (result.kind, result.value, result.method) == ("route", "/items/{x}/checkout", "POST")
    assert _mentions(result) == [
        ("docs/a.md", 5, "route", "/items/{item_id}/checkout"),
        ("docs/a.md", 5, "route", "POST /items/{id}/checkout"),
    ], "a mention without a method matches; GET does not"
    assert result.definitions is not None
    assert result.definitions.items == [
        {
            "method": "POST",
            "path": "/items/{item_id}/checkout",
            "kind": "http",
            "handler": "checkout",
            "file": DOC_LOOKUP_ROUTE_FILE,
            "line": 1,
        }
    ]
    any_method = doc_queries.doc_refs_lookup(doc_report, route="/api/items/{id}/checkout", limit=None)
    assert len(_mentions(any_method)) == 3, "no method matches every mention; /api is optional"
    other_method = doc_queries.doc_refs_lookup(doc_report, route="DELETE /items/{id}/checkout", limit=None)
    assert other_method.definitions is not None
    assert other_method.definitions.total == 0, "no live route serves DELETE"
    assert other_method.mentions is not None
    assert [r["text"] for r in other_method.mentions.items] == ["/items/{item_id}/checkout"], (
        "only the mention without a method"
    )


def test_a_route_installed_code_defines_names_the_include_and_where_the_library_defines_it(tmp_path) -> None:
    library = {"dist": "extlib", "version": "1.2.3", "file": "extlib/router.py", "line": 7}
    route = di_route(
        "checkout",
        "/items/{item_id}/checkout",
        method="POST",
        file=DOC_LOOKUP_ROUTE_FILE,
        line=1,
        library=library,
    )
    report = doc_lookup_report(tmp_path / "repo", route=route)
    result = doc_queries.doc_refs_lookup(report, route="POST /items/{id}/checkout", limit=None)
    assert result.definitions is not None
    assert result.definitions.items[0]["library"] == "extlib 1.2.3 extlib/router.py:7"
    assert len(_mentions(result)) == 2, "the mentions resolve against the library route"
    text = doc_presenters.doc_refs(result).text
    assert f"[{DOC_LOOKUP_ROUTE_FILE}:1 · extlib 1.2.3 extlib/router.py:7]" in text


def test_a_symbol_query_matches_whole_first_and_last_parts(doc_report) -> None:
    holder = doc_queries.doc_refs_lookup(doc_report, symbol="Holder", limit=None)
    assert holder.mentions is not None
    assert [r["text"] for r in holder.mentions.items] == ["Holder", "Holder.status"]
    assert holder.definitions is not None
    assert [(d["file"], d["line"], d["kind"]) for d in holder.definitions.items] == [
        (DOC_LOOKUP_ROUTE_FILE, 3, "class"),
        (DOC_LOOKUP_OTHER_FILE, 1, "class"),
    ]
    status = doc_queries.doc_refs_lookup(doc_report, symbol="status", limit=None)
    assert status.mentions is not None
    assert [r["text"] for r in status.mentions.items] == ["Holder.status"]
    dotted = doc_queries.doc_refs_lookup(doc_report, symbol="Holder.status", limit=None)
    assert dotted.mentions is not None
    assert [r["text"] for r in dotted.mentions.items] == ["Holder.status"]
    assert dotted.definitions is not None
    assert [(d["name"], d["container"], d["scope"]) for d in dotted.definitions.items] == [
        ("status", "Holder", "class")
    ]
    none = doc_queries.doc_refs_lookup(doc_report, symbol="Nope", limit=None).definitions
    assert none is not None and none.total == 0


def test_symbol_queries_say_when_definitions_span_owners_or_files(doc_report) -> None:
    holder = doc_queries.doc_refs_lookup(doc_report, symbol="Holder", limit=None)
    assert (holder.ambiguous, holder.owners) == (True, [DOC_LOOKUP_ROUTE_FILE, DOC_LOOKUP_OTHER_FILE])
    status = doc_queries.doc_refs_lookup(doc_report, symbol="status", limit=None)
    assert (status.ambiguous, status.owners) == (False, [f"Holder ({DOC_LOOKUP_ROUTE_FILE})"])
    nope = doc_queries.doc_refs_lookup(doc_report, symbol="Nope", limit=None)
    assert (nope.ambiguous, nope.owners) == (False, [])
    for other in (
        doc_queries.doc_refs_lookup(doc_report, file=DOC_LOOKUP_ROUTE_FILE, limit=None),
        doc_queries.doc_refs_lookup(doc_report, route="/items/{id}/checkout", limit=None),
        doc_queries.doc_refs_lookup(doc_report, doc="docs/a.md", limit=None),
    ):
        assert (other.ambiguous, other.owners) == (None, None), other.kind


def test_a_dotted_symbol_query_matches_only_its_owner(tmp_path) -> None:
    report = doc_lookup_two_owner_report(tmp_path / "repo")
    status = doc_queries.doc_refs_lookup(report, symbol="status", limit=None)
    assert status.mentions is not None
    assert [r["text"] for r in status.mentions.items] == ["Holder.status", "Other.status", "routes.r.Holder.status"]
    assert status.ambiguous is True
    assert status.owners == [f"Holder ({DOC_LOOKUP_ROUTE_FILE})", f"Other ({DOC_LOOKUP_OTHER_FILE})"]
    holder_status = doc_queries.doc_refs_lookup(report, symbol="Holder.status", limit=None)
    assert holder_status.mentions is not None
    assert [(r["file"], r["text"]) for r in holder_status.mentions.items] == [
        ("docs/a.md", "Holder.status"),
        ("docs/c.md", "routes.r.Holder.status"),
    ], "a longer name resolved to Holder.status is it; Other.status is not"
    assert holder_status.ambiguous is False
    other_status = doc_queries.doc_refs_lookup(report, symbol="Other.status", limit=None)
    assert other_status.mentions is not None
    assert [r["text"] for r in other_status.mentions.items] == ["Other.status"]


def test_a_dotted_symbol_query_finds_definitions_by_the_whole_qualifier_chain() -> None:
    rows = [
        SymbolRow("Owner", "class", None, "backend/pkg/mod.py", 1, "python"),
        SymbolRow("attr", "method", "Owner", "backend/pkg/mod.py", 2, "python", "class"),
        SymbolRow("func", "function", None, "backend/pkg/mod.py", 5, "python"),
        SymbolRow("attr", "method", "Outer.Inner", "backend/nest.py", 3, "python", "class"),
    ]

    def found(query: str) -> list[tuple[str, int]]:
        bindings = {"backend/pkg": frozenset({"Owner"})}
        return [(d["file"], d["line"]) for d in doc_queries._symbol_definitions(rows, query, bindings)]

    assert found("pkg.mod.Owner.attr") == [("backend/pkg/mod.py", 2)]
    assert found("mod.func") == [("backend/pkg/mod.py", 5)]
    assert found("Outer.Inner.attr") == [("backend/nest.py", 3)]
    assert found("other.mod.Owner.attr") == []
    assert found("pkg.Owner.attr") == [("backend/pkg/mod.py", 2)], "pkg/__init__.py binds Owner"
    assert found("pkg.func") == [], "pkg/__init__.py does not bind func"
    assert found("backend.mod.Owner.attr") == [], "a run that skips a package"
    assert found("SomeClass.Owner.attr") == [], "Owner is not nested in SomeClass"
    assert found("Wrong.Inner.attr") == []


def test_a_dotted_symbol_query_keeps_longer_names_and_suffixes_without_definitions() -> None:
    def ref(target: str, *defined_in: str) -> SimpleNamespace:
        return SimpleNamespace(target=target, defined_in=list(defined_in))

    holder = frozenset({"app/a.py:3"})
    assert doc_queries._symbol_matches(ref("Holder.status.value"), "Holder.status", holder), (
        "a name that goes on from the query is the query's attribute"
    )
    assert doc_queries._symbol_matches(ref("pkg.Holder.status", "app/a.py:3"), "Holder.status", holder)
    assert not doc_queries._symbol_matches(ref("pkg.Holder.status", "app/b.py:1"), "Holder.status", holder), (
        "with definitions, a longer name resolved elsewhere is another owner's"
    )
    assert not doc_queries._symbol_matches(ref("Other.status"), "Holder.status", holder)
    assert doc_queries._symbol_matches(ref("pkg.Gone.attr"), "Gone.attr", frozenset()), (
        "without definitions owners can't be told apart, so the suffix matches"
    )
    assert doc_queries._symbol_matches(ref("Gone.attr.value"), "Gone.attr", frozenset())
    assert not doc_queries._symbol_matches(ref("Gone.attribute"), "Gone.attr", frozenset())
    assert not doc_queries._symbol_matches(ref("xGone.attr"), "Gone.attr", frozenset())


def test_a_doc_query_lists_links_references_inbound_reachability_and_checks(doc_report) -> None:
    result = doc_queries.doc_refs_lookup(doc_report, doc="docs/a.md", limit=None)
    assert result.links is not None and result.references is not None and result.inbound is not None
    assert [(r["line"], r["text"], r["status"]) for r in result.links.items] == [
        (3, "../backend/service_backend/api/routes/r.py", "ok"),
        (9, "#a", "ok"),
        (9, "nope.md", "missing"),
    ]
    assert [r["kind"] for r in result.references.items] == ["path", "route", "route", "route"] + ["symbol"] * 4 + [
        "path"
    ]
    gone = result.references.items[-1]
    assert (gone["text"], gone["status"], gone["checked"]) == ("backend/service_backend/gone.py", "missing", True)
    assert result.inbound.items == [
        {"file": "docs/README.md", "line": 3},
        {"file": "docs/b.md", "line": 3},
        {"file": "docs/plans/p.md", "line": 3},
    ]
    assert (result.reachable, result.reachability_root, result.in_scope) == (True, "docs/README.md", True)
    assert result.checks == ["links", "paths", "routes", "symbols", "scripts"]
    assert (result.mentions, result.definitions, result.doc_count) == (None, None, None)

    plan = doc_queries.doc_refs_lookup(doc_report, doc="docs/plans/p.md", limit=None)
    assert plan.reachable is None, "outside the orphan check"
    missing = doc_queries.doc_refs_lookup(doc_report, doc="docs/nope.md", limit=None)
    assert missing.links is not None
    assert (missing.in_scope, missing.checks, missing.links.total) == (False, None, 0)


def test_doc_lookup_limits_each_list_and_counts_what_it_omits(doc_report) -> None:
    file = doc_queries.doc_refs_lookup(doc_report, file=DOC_LOOKUP_ROUTE_FILE, limit=2)
    assert file.mentions is not None
    assert (len(file.mentions.items), file.mentions.total, file.mentions.omitted) == (2, 8, 6)
    assert file.doc_count == 1, "doc_count counts every mention, not the shown ones"
    doc = doc_queries.doc_refs_lookup(doc_report, doc="docs/a.md", limit=1)
    assert doc.links is not None and doc.references is not None and doc.inbound is not None
    assert [(p.total, p.omitted) for p in (doc.links, doc.references, doc.inbound)] == [(3, 2), (9, 8), (3, 2)]
    route = doc_queries.doc_refs_lookup(doc_report, route="/items/{id}/checkout", limit=0)
    assert route.definitions is not None
    assert route.mentions is not None
    assert (route.definitions.omitted, route.mentions.omitted) == (1, 3)


def test_doc_lookup_analysis_keeps_route_gaps_and_adds_frontend_problems(tmp_path) -> None:
    # The runtime passes only route gaps into the report (tested there).
    report = doc_lookup_report(tmp_path / "repo", route_limitations=[limitation()])
    result = doc_queries.doc_refs_lookup(report, route="/items/{id}/checkout", limit=None)
    assert result.complete is True
    assert [p["kind"] for p in result.limitations] == ["opaque_router_call"]
    unavailable = problems.diagnostic(
        "frontend_unavailable", stage="doc refs", step="run typescript helper", file="frontend", message="no node"
    )
    symbol = doc_queries.doc_refs_lookup(report, symbol="Holder", limit=None, frontend_problems=[unavailable])
    assert symbol.complete is False
    assert [p["kind"] for p in symbol.diagnostics] == ["frontend_unavailable"]


def test_doc_lookup_needs_the_frontend_for_symbols_and_frontend_files() -> None:
    assert doc_queries.doc_lookup_needs_frontend(file=None, symbol="X", frontend_root="frontend")
    assert doc_queries.doc_lookup_needs_frontend(file="frontend/lib/api.ts", symbol=None, frontend_root="frontend")
    assert doc_queries.doc_lookup_needs_frontend(file="frontend", symbol=None, frontend_root="frontend")
    assert not doc_queries.doc_lookup_needs_frontend(file="frontend-docs/x.md", symbol=None, frontend_root="frontend")
    assert not doc_queries.doc_lookup_needs_frontend(file=DOC_LOOKUP_ROUTE_FILE, symbol=None, frontend_root="frontend")
    assert not doc_queries.doc_lookup_needs_frontend(file=None, symbol=None, frontend_root="frontend")
    assert doc_queries.doc_lookup_needs_frontend(file="web/chat/app.ts", symbol=None, frontend_root="web/chat")
    assert not doc_queries.doc_lookup_needs_frontend(file="frontend/app.ts", symbol=None, frontend_root="web/chat")


@pytest.mark.parametrize(
    ("text", "valid"),
    [("Appointment", True), ("Appointment.status", True), ("$store", True), ("_x", True), ("a b", False)]
    + [("a.", False), ("1x", False), ("x()", False)],
)
def test_doc_symbol_queries_are_names_or_dotted_names(text: str, valid: bool) -> None:
    assert doc_queries.is_doc_symbol(text) is valid


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("POST /x/{id}", ("POST", "/x/{id}")),
        ("websocket /ws", ("WS", "/ws")),
        ("/x", (None, "/x")),
        ("FETCH /x", None),
        ("appointments", None),
        ("GET x", None),
    ],
)
def test_doc_route_queries_parse_as_docs_write_routes(text: str, parsed) -> None:
    assert doc_queries.parse_doc_route(text) == parsed


def test_doc_check_counts_references_and_the_checked_ones(doc_report) -> None:
    result = doc_queries.doc_check(doc_report)
    references = list(doc_report.references())
    assert (
        result.reference_count
        == len(references)
        == sum(n for c in result.reference_counts.values() for n in c.values())
    )
    assert result.checked_count == sum(1 for ref in references if ref.checked)
    assert 0 < result.checked_count < result.reference_count, "the fixture has skipped and Tier B references"
    assert result.findings == doc_report.findings()


def test_doc_lookup_tries_a_route_with_and_without_api_on_both_sides(doc_report) -> None:
    """A query and a mention each match as written or without one leading
    ``/api``, as the check matches a mention (`doc_rules.route_path_variants`)."""
    with_api = doc_queries.doc_refs_lookup(doc_report, route="/api/items/{id}/checkout", limit=None)
    without = doc_queries.doc_refs_lookup(doc_report, route="/items/{id}/checkout", limit=None)
    assert _mentions(with_api) == _mentions(without)
    assert with_api.definitions is not None
    assert without.definitions is not None
    assert with_api.definitions.items == without.definitions.items
    none = doc_queries.doc_refs_lookup(doc_report, route="/api", limit=None).definitions
    assert none is not None and none.total == 0
