"""Dependency analysis against the FastAPI runtime.

Every case in ``oracles/fastapi_dependency_cases.py`` is imported by the real
FastAPI (``fastapi_oracle_results``) and analyzed statically; the DI JSON must
match the runtime exactly: routes in dispatch order, handlers, kinds and ``def``
lines, route-level dependencies in order with their app/router/decorator layer,
parameter dependencies, closures and ``redirect_slashes``. A case in
``INEXACT_DEPENDENCIES`` may differ, but must report the limitations listed there.
"""

from __future__ import annotations

import pytest

from code_intel import di_map, include_model
from code_intel.oracles import case_package
from code_intel.oracles.fastapi_dependency_cases import DEPENDENCY_CASES, INEXACT_DEPENDENCIES


def _runtime(results, case_id: str) -> dict:
    case = results.results["cases"].get(case_id)
    assert case is not None, f"oracle produced no result for {case_id}"
    assert "error" not in case, f"{case_id}: FastAPI could not import the case ({case['error']})"
    return case


def _routes(results, case_id: str) -> dict[tuple[str, str], dict]:
    return {(r["method"], r["path"]): r for r in _runtime(results, case_id)["routes"]}


def _short(dep_id: str) -> str:
    return dep_id.split(":", 1)[1]


_DI_CACHE: dict[str, dict] = {}


def _di(results, case_id: str) -> dict:
    if case_id not in _DI_CACHE:
        from code_intel import router_graph

        case = DEPENDENCY_CASES[case_id]
        version = include_model.for_version(results.results["fastapi_version"], source=None)
        model = router_graph.build_backend_model(
            results.root, case_package(case_id), case.app, results.root, include_model=version
        )
        # pyrefly: ignore[unsupported-operation]
        _DI_CACHE[case_id] = di_map.build_di(model)
    return _DI_CACHE[case_id]


# ── The analyzer equals the runtime ──────────────────────────────────────


@pytest.mark.parametrize("case_id", sorted(DEPENDENCY_CASES))
def test_dependency_case_matches_the_fastapi_runtime(fastapi_oracle_results, case_id: str) -> None:
    from code_intel.oracles import fastapi_oracle

    runtime = _runtime(fastapi_oracle_results, case_id)
    di = _di(fastapi_oracle_results, case_id)
    diffs = {name: rows for name, rows in fastapi_oracle.compare(runtime, di).items() if rows}
    assert di["diagnostics"] == []
    allowed = DEPENDENCY_CASES[case_id].allow
    unexpected = [lim for lim in di["analysis_limitations"] if lim["kind"] not in allowed]
    assert unexpected == []
    if case_id in INEXACT_DEPENDENCIES:
        reported = {lim["kind"] for lim in di["analysis_limitations"]}
        assert INEXACT_DEPENDENCIES[case_id] <= reported, "the limitations that cover the difference are reported"
        assert diffs, f"{case_id} now matches the runtime: drop it from INEXACT_DEPENDENCIES"
        return
    assert diffs == {}, f"{case_id}: analyzer differs from FastAPI"


# ── Runtime truths the cases pin ─────────────────────────────────────────


def test_runtime_truth_class_and_instance_dependencies(fastapi_oracle_results) -> None:
    routes = _routes(fastapi_oracle_results, "c03")
    for path in ("/class", "/class-bare", "/class-annot-bare"):
        assert [_short(d["id"]) for d in routes[("GET", path)]["param_deps"]] == ["Pager"], path
    assert routes[("GET", "/instance")]["closure"] == [
        "case_c03.deps_cls:RoleChecker()",
        "case_c03.deps:get_user",
        "case_c03.deps:get_db",
    ]


def test_runtime_truth_methods_and_lambdas(fastapi_oracle_results) -> None:
    routes = _routes(fastapi_oracle_results, "c04")
    got = {
        path: _short(routes[("GET", path)]["param_deps"][0]["id"])
        for path in ("/bound", "/static", "/classm", "/lambda", "/agen")
    }
    assert got == {
        "/bound": "Svc().get_items",
        "/static": "Svc.static_dep",
        "/classm": "Svc.cls_dep",
        "/lambda": "<lambda>@L21",
        "/agen": "agen",
    }


def test_runtime_truth_factory_ids_and_closures(fastapi_oracle_results) -> None:
    routes = _routes(fastapi_oracle_results, "c16")
    assert routes[("GET", "/scope")]["closure"] == ["case_c16.factories:require_scope", "case_c16.deps:get_db"]
    assert routes[("GET", "/role")]["closure"] == ["case_c16.factories:require_role", "case_c16.deps:get_db"]


def test_runtime_truth_last_annotated_marker_wins(fastapi_oracle_results) -> None:
    routes = _routes(fastapi_oracle_results, "c17")
    assert [_short(d["id"]) for d in routes[("GET", "/two")]["param_deps"]] == ["get_user"]
    assert [_short(d["id"]) for d in routes[("GET", "/nested")]["param_deps"]] == ["verify_key"]
    assert routes[("GET", "/query-last")]["param_deps"] == []


def test_runtime_truth_same_named_dependencies_stay_distinct(fastapi_oracle_results) -> None:
    routes = _routes(fastapi_oracle_results, "c14")
    assert routes[("GET", "/a")]["closure"] == ["case_c14.api.routes.a:get_ctx", "case_c14.deps:get_db"]
    assert routes[("GET", "/b")]["closure"] == ["case_c14.api.routes.b:get_ctx", "case_c14.deps:verify_key"]


# ── DI output details the runtime comparison does not cover ──────────────


def test_di_displays_instances_factories_and_aliases(fastapi_oracle_results) -> None:
    c03 = {r["path"]: r for r in _di(fastapi_oracle_results, "c03")["routes"]}
    assert c03["/instance"]["direct_deps"] == ["RoleChecker(admin)"]
    (alias,) = c03["/instance-alias"]["deps"]
    assert (alias["display"], alias["alias"], alias["via"]) == ("RoleChecker(admin)", "admin_only", "route decorator")
    c16 = {r["path"]: r for r in _di(fastapi_oracle_results, "c16")["routes"]}
    assert c16["/role"]["direct_deps"] == ["require_role(admin)"]
    d02 = _di(fastapi_oracle_results, "d02")
    aliased = next(r for r in d02["routes"] if r["path"] == "/aliased")
    guard = next(d for d in aliased["deps"] if d["via"] == "route decorator")
    assert (guard["id"], guard["display"], guard["alias"]) == (
        "case_d02.core.policy:require_any_permission",
        "require_any_permission(thing.view)",
        "_guard",
    )
    factory = d02["dependency_functions"]["case_d02.core.policy:require_any_permission"]
    assert factory["kind"] == "factory_result"
    assert factory["aliases"] == ["_guard", "shared_guard"]
    assert factory["sub_deps"] == ["case_d02.core.policy:get_policy_context"]


# ── Registry entries against the runtime's dependency trees ──────────────


def _runtime_sub_deps(results, case_id: str) -> dict[str, set[tuple[str, ...]]]:
    """id -> every distinct list of direct sub-dependency ids FastAPI built for it."""
    out: dict[str, set[tuple[str, ...]]] = {}

    def visit(node: dict) -> None:
        subs = tuple(dict.fromkeys(s["id"] for s in node["sub"]))
        out.setdefault(node["id"], set()).add(subs)
        for sub in node["sub"]:
            visit(sub)

    for route in _runtime(results, case_id)["routes"]:
        for node in (*route["route_deps"], *route["param_deps"]):
            visit(node)
    return out


@pytest.mark.parametrize("case_id", sorted(set(DEPENDENCY_CASES) - set(INEXACT_DEPENDENCIES)))
def test_registry_sub_deps_match_the_fastapi_runtime(fastapi_oracle_results, case_id: str) -> None:
    """Each registry entry lists the sub-dependencies FastAPI built for that id;
    an id FastAPI built with different ones is reported, naming the id."""
    di = _di(fastapi_oracle_results, case_id)
    registry = di["dependency_functions"]
    shared = [lim["message"] for lim in di["analysis_limitations"] if lim["kind"] == "shared_dependency_id"]
    for dep_id, forms in _runtime_sub_deps(fastapi_oracle_results, case_id).items():
        assert dep_id in registry, f"{case_id}: {dep_id} has no registry entry"
        listed = tuple(registry[dep_id]["sub_deps"])
        if len(forms) == 1:
            assert {listed} == forms, f"{case_id}: {dep_id}"
        else:
            assert listed in forms, f"{case_id}: {dep_id}"
            assert any(f"dependency {dep_id}:" in message for message in shared), f"{case_id}: {dep_id}"


def test_class_and_instance_are_distinct_targets(fastapi_oracle_results) -> None:
    """d24: ``Depends(C)`` and ``Depends(C())`` keep their own entries, reverse
    index rows and text labels."""
    di = _di(fastapi_oracle_results, "d24")
    registry = di["dependency_functions"]
    cls, inst = registry["case_d24.checkers:Checker"], registry["case_d24.checkers:Checker()"]
    assert (cls["kind"], cls["sub_deps"]) == ("class", ["case_d24.deps:get_db"])
    assert (inst["kind"], inst["sub_deps"]) == ("instance", ["case_d24.deps:get_user"])
    assert inst["aliases"] == ["shared"]
    assert registry["case_d24.checkers:KeyChecker"]["sub_deps"] == ["case_d24.deps:verify_key"]
    assert registry["case_d24.checkers:KeyChecker()"]["sub_deps"] == ["case_d24.deps:get_user"]
    reverse = di["reverse_index"]
    assert reverse["case_d24.checkers:Checker"] == ["GET /class", "GET /route-level"]
    assert reverse["case_d24.checkers:Checker()"] == ["GET /alias", "GET /instance", "GET /route-level"]
    assert reverse["case_d24.deps:verify_key"] == ["GET /inherited-class"]

    # pyrefly: ignore[bad-argument-type]
    rows = {tuple(line.split()[:2]) for line in di_map.format_text(di).splitlines()}
    assert ("case_d24.checkers:Checker()", "instance") in rows
    assert ("case_d24.checkers:Checker", "class") in rows


def test_class_and_instance_are_distinct_reverse_groups(fastapi_oracle_results) -> None:
    queries = pytest.importorskip("code_intel.queries")
    di = _di(fastapi_oracle_results, "d24")
    usage = queries.di_reverse(di, "Checker", limit=None)
    assert isinstance(usage, queries.DependencyUsage)
    groups = {g.target["id"]: g for g in usage.groups}
    assert set(groups) == {"case_d24.checkers:Checker", "case_d24.checkers:Checker()"}
    assert groups["case_d24.checkers:Checker"].sub_dep_labels == ["get_db"]
    assert groups["case_d24.checkers:Checker()"].sub_dep_labels == ["get_user"]
    assert {r["path"] for r in groups["case_d24.checkers:Checker"].routes} == {"/class", "/route-level"}


def test_bound_methods_are_distinct_from_functions(fastapi_oracle_results) -> None:
    """d25: a plain method bound to an instance is ``C().m`` without its first
    parameter; static and class methods are ``C.m`` either way."""
    routes = _routes(fastapi_oracle_results, "d25")
    assert routes[("GET", "/bound")]["closure"] == ["case_d25.svc:Svc().method", "case_d25.deps:verify_key"]
    assert routes[("GET", "/inherited-bound")]["closure"] == ["case_d25.svc:Base().inherited", "case_d25.deps:get_db"]
    assert [d["id"] for d in routes[("GET", "/static")]["param_deps"]] == ["case_d25.svc:Svc.static"] * 2
    assert [d["id"] for d in routes[("GET", "/classmethod")]["param_deps"]] == ["case_d25.svc:Svc.klass"] * 2
    registry = _di(fastapi_oracle_results, "d25")["dependency_functions"]
    assert registry["case_d25.svc:Svc.method"]["sub_deps"] == ["case_d25.deps:get_user", "case_d25.deps:verify_key"]
    assert registry["case_d25.svc:Svc().method"]["sub_deps"] == ["case_d25.deps:verify_key"]


def test_one_id_read_differently_is_reported(fastapi_oracle_results) -> None:
    """d26: a partial, a class partial and a factory share their target's id at
    runtime; each such id is named in a ``shared_dependency_id`` limitation."""
    di = _di(fastapi_oracle_results, "d26")
    messages = [lim["message"] for lim in di["analysis_limitations"] if lim["kind"] == "shared_dependency_id"]
    for dep_id in (
        "case_d26.main:paged",
        "case_d26.main:Pager",
        "case_d26.main:make_closure",
        "case_d26.main:make_instance",
    ):
        assert any(f"dependency {dep_id}:" in m for m in messages), dep_id


def test_a_redefined_function_keeps_each_definitions_sub_deps(fastapi_oracle_results) -> None:
    """d27: two definitions share ``main:get_x``; each route reaches the one it names."""
    routes = {r["path"]: r for r in _di(fastapi_oracle_results, "d27")["routes"]}
    assert routes["/early"]["all_deps"] == ["case_d27.main:early_dep", "case_d27.main:get_x", "case_d27.deps:get_db"]
    assert routes["/late"]["all_deps"] == [
        "case_d27.main:get_x",
        "case_d27.deps:get_user",
        "case_d27.deps:get_db",
    ]
