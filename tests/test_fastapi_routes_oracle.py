"""The router graph against the FastAPI runtime (routes side).

Every case in ``oracles/fastapi_cases.py`` is imported by ``fastapi_oracle.py``
under the locked FastAPI; the analyzer must give the same live routes in the
same Starlette order, with the same handlers, ``def`` lines and route-level
dependency layers (ids in FastAPI's order, each labelled app, router or route
decorator). A case's ``allow`` lists the only limitation kinds it may report; a
case in ``INEXACT`` may differ from the runtime, but must report the limitation
kinds listed there, which name what the analysis could not follow.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from code_intel import include_model
from code_intel.backend_model import BackendModel, Def, Located, Unknown
from code_intel.oracles import case_package
from code_intel.oracles.fastapi_cases import CASES, IMPORT_ERRORS_ON_NEWER_FASTAPI, INEXACT, INEXACT_LIVE
from code_intel.router_graph import build_backend_model


def dependency_id(model: BackendModel, loc: Located) -> str:
    """``module:qualname`` of a ``Depends(...)``/``Security(...)`` target, as the oracle names it."""
    node = loc.node
    if not isinstance(node, ast.Call) or not node.args:
        return f"?{ast.unparse(node)}"
    target = node.args[0]
    instance = ""
    if isinstance(target, ast.Call):  # a factory call or an instance: identified by the callee
        target = target.func
        instance = "()"
    for resolved in model.resolver.resolve_expr(Located(loc.module, target, loc.at)):
        if isinstance(resolved, Def):
            # A factory result is identified by the factory, an instance by its class with "()".
            suffix = instance if isinstance(resolved.node, ast.ClassDef) else ""
            return f"{resolved.module}:{resolved.qualname}{suffix}"
    return f"?{ast.unparse(node)}"


def analyzer_rows(model: BackendModel) -> list[tuple]:
    rows = []
    for route in model.routes:
        deps: list[tuple[str, str]] = []
        for layer in route.layers:
            if isinstance(layer.dependencies, Unknown):
                deps.append(("?", layer.via))
                continue
            deps.extend((dependency_id(model, loc), layer.via) for loc in layer.dependencies)
        rows.append((route.method, route.path, route.kind, route.handler_id, route.file, route.line, deps))
    return rows


def runtime_rows(result: dict) -> list[tuple]:
    return [
        (
            row["method"],
            row["path"],
            row["kind"],
            row["handler_id"],
            row["file"],
            row["line"],
            [(dep["id"], dep["via"]) for dep in row["route_deps"]],
        )
        for row in result["routes"]
    ]


def check_case(case_id: str, results: dict, root: Path, inexact: dict[str, frozenset[str]]) -> None:
    """The analyzer, told the FastAPI version that built ``results``, gives the
    same live routes, or reports the limitations ``inexact`` lists for the case."""
    case = CASES[case_id]
    result = results["cases"][case_id]
    if case_id in IMPORT_ERRORS_ON_NEWER_FASTAPI and "error" in result:
        assert IMPORT_ERRORS_ON_NEWER_FASTAPI[case_id] in result["error"]
        pytest.skip(f"FastAPI {results['fastapi_version']} does not import {case_id}: {result['error']}")
    assert "error" not in result, result.get("error")
    version = include_model.for_version(results["fastapi_version"], source=None)
    model = build_backend_model(root, case_package(case_id), case.app, root, include_model=version)

    assert model.diagnostics == ()
    reported = {p.kind for p in model.limitations}
    assert reported <= case.allow, [p.message for p in model.limitations]
    if case_id in inexact:
        assert inexact[case_id] <= reported, "the limitations that cover the difference are reported"
        assert analyzer_rows(model) != runtime_rows(result), (
            f"{case_id} now matches FastAPI {results['fastapi_version']}: drop it from its INEXACT table"
        )
        return
    assert analyzer_rows(model) == runtime_rows(result)
    assert model.redirect_slashes == result["redirect_slashes"]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_live_routes_match_the_fastapi_runtime(case_id: str, fastapi_oracle_results, fastapi_runtime) -> None:
    """Under the installed FastAPI, whose include model picks the table of inexact cases."""
    results = fastapi_oracle_results.results
    assert include_model.for_version(results["fastapi_version"], source=None) == fastapi_runtime.include_model
    inexact = INEXACT_LIVE if fastapi_runtime.include_model.kind == "live" else INEXACT
    check_case(case_id, results, fastapi_oracle_results.root, inexact)


def test_every_inexact_case_is_a_case_that_allows_its_limitations() -> None:
    for case_id, kinds in (*INEXACT.items(), *INEXACT_LIVE.items()):
        assert kinds <= CASES[case_id].allow, case_id


def test_every_route_case_runs_under_the_runtime(fastapi_oracle_results) -> None:
    """A case whose app fails to import would silently compare nothing."""
    results = fastapi_oracle_results.results["cases"]
    assert set(CASES) <= set(results)
    assert all(results[case_id]["routes"] for case_id in CASES if case_id not in IMPORT_ERRORS_ON_NEWER_FASTAPI)


def test_runtime_truths_the_analyzer_relies_on(fastapi_oracle_results) -> None:
    """Pins the runtime findings the analyzer's rules rest on, so a FastAPI upgrade that
    changes them fails loudly instead of quietly moving the expectations."""
    results = fastapi_oracle_results.results["cases"]

    def paths(case_id: str) -> list[str]:
        return [row["path"] for row in results[case_id]["routes"]]

    def deps(case_id: str, path: str) -> list[str]:
        (row,) = [r for r in results[case_id]["routes"] if r["path"] == path]
        return [d["id"].split(":")[1] for d in row["route_deps"]]

    assert paths("c11") == ["/v1/items", "/v2/items"]
    assert deps("c12", "/api/k/deps-concat") == ["verify_key", "verify_key", "audit"]
    assert paths("c19") == ["/made/m", "/ws/live"]
    assert paths("c21") == ["/live"]
    assert paths("c22") == ["/api/shared/a", "/api/shared/b"]
    assert paths("c23") == ["/x/y/a/own", "/x/y/a/sub/s"]
    assert deps("c26", "/open/public") == []
    assert deps("c26", "/admin/guarded") == ["verify_key"]
    assert deps("c27", "/a/sub/s") == ["verify_key", "audit"]
    assert [d["via"] for d in results["c06"]["routes"][0]["route_deps"]] == ["app"]
