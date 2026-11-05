"""The library capture applied by the analysis: library routers in the router graph,
library dependencies in the resolver, the staleness rules, and the oracle cases that
compare the result with the running app."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import test_verify
from fixture_repo import hermetic_git
from library_cases import COMPLETE, GAPS, LibraryCase
from library_fixture import checkout, lock_text
from test_capture import CAPTURE_FILE, edit_main, run_capture

from code_intel import (
    di_map,
    library_capture,
    library_view,
    route_map,
    runner,
    runtime_check,
    ts_bridge,
    verify,
)
from code_intel.analysis_runtime import AnalysisRuntime
from code_intel.backend_model import CapturedEndpoint
from code_intel.config import Config, ConfigFile, load_config
from code_intel.oracles import fastapi_oracle

pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("fastapi")
    hermetic_git(monkeypatch, tmp_path / "home")
    return checkout(tmp_path / "repo", tmp_path / "site")


@pytest.fixture
def captured(repo: Path) -> Path:
    code, _, err = run_capture(repo)
    assert code == 0, err
    return repo


def config_of(repo: Path) -> Config:
    return load_config(repo)


def model_of(repo: Path) -> Any:
    return route_map.build_backend_model(repo, config_of(repo).backend)


def di_of(repo: Path) -> Any:
    return di_map.compute(repo, config_of(repo).backend)


def limitations(result: Any, kind: str) -> list[str]:
    return [lim["message"] for lim in result["analysis_limitations"] if lim["kind"] == kind]


def route(di: Any, method: str, path: str) -> Any:
    return next(r for r in di["routes"] if (r["method"], r["path"]) == (method, path))


def edit_capture(repo: Path, change: Any) -> None:
    path = repo / CAPTURE_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def stage(repo: Path) -> None:
    test_verify.git(repo, "add", "-A")


# ── The router graph ─────────────────────────────────────────────────────


def test_a_captured_router_is_mounted_like_a_repository_router(captured: Path) -> None:
    di = di_of(captured)
    me = route(di, "GET", "/users/me")
    assert (me["file"], me["line"]) == ("backend/pkg/main.py", 25)
    assert me["library"] == {"dist": "extlib", "version": "1.2.3", "file": "extlib/__init__.py", "line": 45}
    assert me["handler_id"] == "extlib:Users.get_router.<locals>.me"
    assert me["chain"][-1] == "library:pkg.main:L25"
    assert (me["app_deps"], me["router_deps"], me["decorator_deps"], me["direct_deps"]) == (
        [],
        ["get_manager", "audit"],
        [],
        ["get_manager"],
    )
    rename = route(di, "POST", "/users/rename")
    assert (rename["router_deps"], rename["decorator_deps"]) == (["get_manager", "audit"], ["count_hits"])
    assert route(di, "WS", "/status/live")["kind"] == "websocket"
    assert [r["path"] for r in di["routes"]] == [
        "/own",
        "/signed",
        "/users/me",
        "/users/rename",
        "/status/ping",
        "/status/live",
        "/nested/v1/inner/item",
    ]


def test_a_nested_library_router_gets_the_include_prefix_and_every_layer_in_fastapi_order(captured: Path) -> None:
    item = route(di_of(captured), "GET", "/nested/v1/inner/item")
    assert item["router_deps"] == ["get_manager", "count_hits", "signed", "audit"]


def test_the_library_routers_are_router_nodes_with_the_include_as_their_place(captured: Path) -> None:
    model = model_of(captured)
    nodes = [n for n in model.routers if n.id.startswith("library:")]
    assert [(n.id, n.file, n.line) for n in nodes] == [
        ("library:pkg.main:L25", "backend/pkg/main.py", 25),
        ("library:pkg.main:L26", "backend/pkg/main.py", 26),
        ("library:pkg.main:L27", "backend/pkg/main.py", 27),
    ]
    assert {e.child for e in model.edges} >= {n.id for n in nodes}
    assert all(isinstance(r.endpoint, CapturedEndpoint) for r in model.routes if r.library is not None)
    di = di_map.build_di(model)
    assert {r["id"]: r["mounted"] for r in di["routers"] if r["id"].startswith("library:")} == dict.fromkeys(
        (n.id for n in nodes), True
    )


def test_no_unresolved_include_is_reported_for_a_captured_router_and_the_mixed_one_stays(captured: Path) -> None:
    messages = limitations(di_of(captured), "unresolved_include")
    assert len(messages) == 1 and "make_mixed_router" in messages[0]
    assert "(not in .code-intel/library-capture.json; run code-intel capture)" in messages[0]


def test_an_include_that_a_condition_guards_is_conditional_like_any_other(captured: Path) -> None:
    edit_main(
        captured,
        'app.include_router(extlib.make_status_router(), prefix="/status")',
        'if own_handler():\n    app.include_router(extlib.make_status_router(), prefix="/status")',
    )
    run_capture(captured)
    di = di_of(captured)
    assert route(di, "GET", "/status/ping")["mount"] == "conditional"
    assert limitations(di, "conditional_router_mount")


def test_without_the_capture_table_the_gap_has_no_hint_and_nothing_is_applied(captured: Path) -> None:
    config = captured / ".code-intel" / "config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text[: text.index("[backend.capture]")] + text[text.index("[frontend]") :], encoding="utf-8")
    stage(captured)
    di = di_of(captured)
    assert di["library"] is None
    assert len(limitations(di, "unresolved_include")) == 4
    assert not any("run code-intel capture" in m for m in limitations(di, "unresolved_include"))
    assert {r["path"] for r in di["routes"]} == {"/own", "/signed"}


def test_without_the_file_every_gap_names_the_missing_capture(repo: Path) -> None:
    di = di_of(repo)
    messages = limitations(di, "unresolved_include")
    assert len(messages) == 4
    assert all("(not in .code-intel/library-capture.json; run code-intel capture)" in m for m in messages)
    unresolved = limitations(di, "unresolved_dependency")
    assert unresolved and all("run code-intel capture" in m for m in unresolved)
    assert limitations(di, "library_capture_stale") == []


def test_a_gap_is_found_by_its_router_text_not_its_layout(captured: Path) -> None:
    edit_main(
        captured,
        'app.include_router(extlib.make_status_router(), prefix="/status")',
        'app.include_router(\n    extlib.make_status_router(  ),\n    prefix = "/status",\n)',
    )
    di = di_of(captured)
    assert route(di, "GET", "/status/ping")["library"] is not None
    assert limitations(di, "library_capture_stale") == []


def test_two_includes_of_one_router_text_each_find_their_own_entry(repo: Path) -> None:
    edit_main(
        repo,
        'app.include_router(extlib.make_status_router(), prefix="/status")',
        'app.include_router(extlib.make_status_router(), prefix="/status")\n'
        'app.include_router(extlib.make_status_router(), prefix="/again")',
    )
    assert run_capture(repo)[0] == 0
    di = di_of(repo)
    assert [r["path"] for r in di["routes"] if r["path"].endswith("/ping")] == ["/status/ping", "/again/ping"]
    assert limitations(di, "library_capture_stale") == []


def test_include_sites_number_equal_router_texts_in_source_order() -> None:
    import ast

    tree = ast.parse(
        "app.include_router(a())\napp.include_router(b)\napp.include_router(  a( ) )\nx.include_router(router=a())\n"
    )
    sites = library_capture.include_sites(tree)
    assert [(s.line, s.router, s.occurrence) for s in sites] == [
        (1, "a()", 1),
        (2, "b", 1),
        (3, "a()", 2),
        (4, "a()", 3),
    ]
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno == 3 and n.args)
    assert isinstance(call, ast.Call)
    site = library_capture.include_site_of(tree, call)
    assert site is not None and (site.line, site.occurrence) == (3, 2)


# ── The dependency resolver ──────────────────────────────────────────────


def test_a_captured_binding_keeps_its_id_and_gets_the_runtime_id_and_the_captured_sub_dependencies(
    captured: Path,
) -> None:
    di = di_of(captured)
    entry = di["dependency_functions"]["pkg.main:current"]
    assert entry["kind"] == "external"
    assert entry["runtime_id"] == "extlib:Users.current"
    assert entry["bound_to"] == "users.current(active=True)"
    assert entry["sub_deps"] == ["extlib:audit", "pkg.deps:get_manager"]
    assert entry["library"] == {"dist": "extlib", "version": "1.2.3", "file": "extlib/__init__.py", "line": 35}
    own = route(di, "GET", "/own")
    assert own["all_deps"] == ["pkg.main:current", "extlib:audit", "pkg.deps:get_manager"]


def test_a_library_callable_named_in_the_package_becomes_a_library_target(captured: Path) -> None:
    di = di_of(captured)
    settings = di["dependency_functions"]["extlib:Settings"]
    assert settings["kind"] == "library" and settings["file"] is None and settings["line"] is None
    assert settings["library"]["file"] == "extlib/__init__.py"
    assert settings["sub_deps"] == ["extlib:audit"]
    assert limitations(di, "unresolved_dependency") == []
    assert not [i for i in di["dependency_functions"] if i.startswith("unresolved:")]


def test_a_callable_of_the_package_in_a_captured_tree_is_read_from_source(repo: Path) -> None:
    (repo / "backend" / "pkg" / "deps.py").write_text(
        "from fastapi import Depends\n\n\ndef get_session():\n    return 1\n\n\n"
        "def get_manager(session=Depends(get_session)):\n    return 'manager'\n",
        encoding="utf-8",
    )
    stage(repo)
    assert run_capture(repo)[0] == 0
    di = di_of(repo)
    manager = di["dependency_functions"]["pkg.deps:get_manager"]
    assert manager["kind"] == "function" and manager["sub_deps"] == ["pkg.deps:get_session"]
    assert "pkg.deps:get_session" in route(di, "GET", "/users/me")["all_deps"]
    assert route(di, "GET", "/users/me")["all_deps"].count("pkg.deps:get_session") == 1


def test_two_captured_subtrees_for_one_library_id_that_differ_are_reported_once(captured: Path) -> None:
    def change(data: dict[str, Any]) -> None:
        include = next(i for i in data["includes"] if i["router"].startswith("users."))
        me = next(r for r in include["routes"] if r["path"] == "/me")
        me["param_deps"].append(
            {"id": "extlib:Settings", "param": "s", "sub": [{"id": "extlib:count_hits", "param": "hits", "sub": []}]}
        )

    edit_capture(captured, change)
    shared = limitations(di_of(captured), "shared_dependency_id")
    assert len(shared) == 1 and "extlib:Settings" in shared[0]


# ── Staleness ────────────────────────────────────────────────────────────


def stale(repo: Path) -> list[str]:
    return limitations(di_of(repo), "library_capture_stale")


def test_a_distribution_whose_version_differs_from_the_lock_is_applied_with_a_stale_limitation(
    captured: Path,
) -> None:
    (captured / "backend" / "uv.lock").write_text(lock_text(extlib="1.2.4"), encoding="utf-8")
    di = di_of(captured)
    [message] = limitations(di, "library_capture_stale")
    assert "distribution extlib was captured at 1.2.3 but backend/uv.lock locks 1.2.4" in message
    assert "code-intel capture" in message
    assert route(di, "GET", "/users/me")["library"] is not None
    assert limitations(di, "unresolved_include") == [m for m in limitations(di, "unresolved_include") if "mixed" in m]


def test_a_distribution_the_lock_no_longer_names_is_reported_stale_and_still_applied(captured: Path) -> None:
    (captured / "backend" / "uv.lock").write_text(
        lock_text().replace('name = "extlib"', 'name = "other"'), encoding="utf-8"
    )
    di = di_of(captured)
    [message] = limitations(di, "library_capture_stale")
    assert "distribution extlib cannot be checked against the lock" in message
    assert route(di, "GET", "/users/me")["library"] is not None


def test_a_capture_for_another_app_is_not_applied(captured: Path) -> None:
    edit_capture(captured, lambda data: data.update(app="pkg.other:app"))
    di = di_of(captured)
    [message] = limitations(di, "library_capture_stale")
    assert "it was made for app pkg.other:app, not pkg.main:app" in message
    assert di["library"]["routes"] == 0
    assert {r["path"] for r in di["routes"]} == {"/own", "/signed"}
    assert len(limitations(di, "unresolved_include")) == 4


def test_an_include_entry_that_matches_no_unresolved_include_is_reported_and_the_include_stays_a_gap(
    captured: Path,
) -> None:
    edit_main(captured, "extlib.make_status_router()", "extlib.make_status_router(None)")
    di = di_of(captured)
    [message] = limitations(di, "library_capture_stale")
    assert "include extlib.make_status_router() #1 at backend/pkg/main.py is not applied" in message
    assert "no unresolved include_router call matches it" in message
    assert not [r for r in di["routes"] if r["path"].startswith("/status")]
    assert any(
        "make_status_router(None)" in m and "run code-intel capture" in m for m in limitations(di, "unresolved_include")
    )


def test_an_include_entry_that_names_a_callable_the_package_lacks_is_not_applied(captured: Path) -> None:
    deps = captured / "backend" / "pkg" / "deps.py"
    deps.write_text(deps.read_text(encoding="utf-8").replace("get_manager", "get_users_manager"), encoding="utf-8")
    edit_main(captured, "get_manager", "get_users_manager")
    di = di_of(captured)
    [message] = [m for m in limitations(di, "library_capture_stale") if "include users.get_router(dict)" in m]
    assert "it names pkg.deps:get_manager, which the analysis cannot find in the package" in message
    assert not [r for r in di["routes"] if r["path"].startswith("/users")]
    gap = [m for m in limitations(di, "unresolved_include") if "users.get_router" in m]
    assert gap and "run code-intel capture" not in gap[0]


def test_a_dependency_entry_naming_a_callable_the_package_lacks_is_not_applied(captured: Path) -> None:
    deps = captured / "backend" / "pkg" / "deps.py"
    deps.write_text(deps.read_text(encoding="utf-8").replace("get_manager", "get_users_manager"), encoding="utf-8")
    edit_main(captured, "get_manager", "get_users_manager")
    di = di_of(captured)
    messages = limitations(di, "library_capture_stale")
    assert any(
        m.startswith(".code-intel/library-capture.json: dependency pkg.main:current is not applied") for m in messages
    )
    assert any(
        "Depends(current)" in m and "run code-intel capture" not in m for m in limitations(di, "unresolved_dependency")
    )
    assert di["dependency_functions"]["pkg.main:current"]["kind"] == "external"
    assert "runtime_id" not in di["dependency_functions"]["pkg.main:current"]


def test_a_binding_rebound_to_another_expression_is_not_applied_and_is_reported_stale(captured: Path) -> None:
    edit_main(captured, "current = users.current(active=True)", "current = users.current(active=False)")
    di = di_of(captured)
    [message] = [m for m in limitations(di, "library_capture_stale") if "dependency pkg.main:current" in m]
    assert "is not applied" in message
    assert "users.current(active=True)" in message and "users.current(active=False)" in message
    entry = di["dependency_functions"]["pkg.main:current"]
    assert entry["kind"] == "external" and "runtime_id" not in entry and entry["sub_deps"] == []
    assert any("Depends(current)" in m and "not in" not in m for m in limitations(di, "unresolved_dependency"))


def test_the_stale_findings_of_the_graph_and_of_the_dependencies_are_reported_once_each(captured: Path) -> None:
    deps = captured / "backend" / "pkg" / "deps.py"
    deps.write_text(deps.read_text(encoding="utf-8").replace("get_manager", "get_users_manager"), encoding="utf-8")
    edit_main(captured, "get_manager", "get_users_manager")
    model = model_of(captured)
    assert [p.message for p in model.limitations if p.kind == "library_capture_stale"] == [
        m for m in limitations(di_of(captured), "library_capture_stale") if "include users.get_router(dict)" in m
    ]
    messages = limitations(di_of(captured), "library_capture_stale")
    assert len(messages) == len(set(messages)) == 2


def test_a_dependency_entry_nothing_uses_is_reported(captured: Path) -> None:
    edit_main(
        captured,
        "def signed(values=Depends(extlib.signed), settings=Depends(extlib.Settings)):",
        "def signed(settings=Depends(extlib.Settings)):",
    )
    messages = stale(captured)
    assert any(
        "external extlib.signed is not applied: no unresolved dependency of that name is used" in m for m in messages
    )


def test_a_malformed_file_is_a_parse_failed_diagnostic_on_the_file(captured: Path) -> None:
    (captured / CAPTURE_FILE).write_text('{"format": 2}\n', encoding="utf-8")
    di = di_of(captured)
    [found] = di["diagnostics"]
    assert (found["kind"], found["category"], found["step"], found["file"]) == (
        "parse_failed",
        "content_lost",
        "read library capture",
        CAPTURE_FILE,
    )
    assert "is 2; this code-intel reads format 1" in found["message"] and "run code-intel capture" in found["message"]
    assert di["complete"] is False


def test_a_route_located_in_a_distribution_the_capture_does_not_list_is_a_parse_failed_diagnostic(
    captured: Path,
) -> None:
    def change(data: dict[str, Any]) -> None:
        data["includes"][0]["routes"][0]["location"]["dist"] = "extlib-userz"

    edit_capture(captured, change)
    [found] = di_of(captured)["diagnostics"]
    assert found["kind"] == "parse_failed" and "no version for 'extlib-userz'" in found["message"]


def test_a_file_that_is_not_json_is_a_parse_failed_diagnostic(captured: Path) -> None:
    (captured / CAPTURE_FILE).write_text("{", encoding="utf-8")
    [found] = di_of(captured)["diagnostics"]
    assert found["kind"] == "parse_failed" and "not valid JSON" in found["message"]


def test_the_view_reads_what_static_checks_can_and_applies_nothing_it_cannot(captured: Path) -> None:
    config = config_of(captured).backend
    view = library_view.read(captured, CAPTURE_FILE, app=config.app, lock=config.lock)
    assert view.capture is not None and view.problems == ()
    assert view.include("backend/pkg/main.py", "extlib.make_status_router()", 1) is not None
    assert view.include("backend/pkg/main.py", "extlib.make_status_router()", 2) is None
    assert view.binding("pkg.main:current") is not None and view.external("extlib.signed") is not None
    missing = library_view.read(captured, "nowhere.json", app=config.app, lock=config.lock)
    assert missing.capture is None and missing.problems == ()
    assert missing.include("backend/pkg/main.py", "extlib.make_status_router()", 1) is None


def test_every_stale_limitation_is_registered_with_the_routes_changed_effect() -> None:
    from code_intel import problems, queries

    assert "library_capture_stale" in problems.LIMITATION_KINDS
    assert queries.BACKEND_ROUTE_GAPS["library_capture_stale"] == frozenset({queries.ROUTES_CHANGED})


# ── The summary and status ───────────────────────────────────────────────


def test_the_di_result_counts_what_was_applied(captured: Path) -> None:
    di = di_of(captured)
    assert di["library"] == {"file": CAPTURE_FILE, "includes": 3, "routes": 5, "dependencies": 3}
    assert di["summary"]["routes"] == 7


# ── The oracle cases ─────────────────────────────────────────────────────


def write_case(repo: Path, case: LibraryCase) -> None:
    edit_main(repo, (repo / "backend/pkg/main.py").read_text(encoding="utf-8"), case.main)
    if case.deps is not None:
        (repo / "backend" / "pkg" / "deps.py").write_text(case.deps, encoding="utf-8")
    stage(repo)


def runtime_of(repo: Path) -> dict[str, Any]:
    backend = config_of(repo).backend
    assert backend.capture is not None
    return runner.run_oracle(
        Path(sys.executable), runtime_check.oracle_arguments(repo, backend, [], None), dict(backend.capture.env)
    )


def differences(repo: Path) -> dict[str, list[str]]:
    return fastapi_oracle.compare(runtime_of(repo), di_of(repo))


@pytest.mark.parametrize("case_id", sorted(COMPLETE))
def test_a_library_case_agrees_with_the_running_app_on_every_route(repo: Path, case_id: str) -> None:
    write_case(repo, COMPLETE[case_id])
    code, _, err = run_capture(repo)
    assert code == 0, err
    assert "not captured" not in err
    assert differences(repo) == {name: [] for name in fastapi_oracle.CLASSES}
    di = di_of(repo)
    assert limitations(di, "unresolved_include") == [] and limitations(di, "unresolved_dependency") == []
    assert limitations(di, "library_capture_stale") == []


@pytest.mark.parametrize("case_id", sorted(GAPS))
def test_a_router_the_capture_cannot_fill_stays_a_gap_that_explains_only_its_installed_routes(
    repo: Path, case_id: str
) -> None:
    write_case(repo, GAPS[case_id])
    code, _, err = run_capture(repo)
    assert code == 0 and "not captured" in err
    di = di_of(repo)
    assert len(limitations(di, "unresolved_include")) == 1
    found = {c.difference.text: c.reason for c in runtime_check.classify(runtime_of(repo), di)}
    assert set(found) == {"GET /mixed/lib", "GET /mixed/own"}
    assert found["GET /mixed/lib"] is not None
    assert found["GET /mixed/own"] is None


def test_the_capture_agrees_with_the_running_app_for_the_whole_fixture(captured: Path) -> None:
    runtime = runtime_of(captured)
    classified = runtime_check.classify(runtime, di_of(captured))
    assert {c.difference.cls for c in classified} == {"route_missing"}
    assert [c.difference.text for c in classified] == ["GET /mixed/lib", "GET /mixed/own"]
    assert all(r["library"] is None or r["library"]["dist"] == "extlib" for r in runtime["routes"])


def test_runtime_rows_attribute_installed_handlers_to_their_distribution(captured: Path) -> None:
    runtime = runtime_of(captured)
    by_path = {r["path"]: r for r in runtime["routes"]}
    assert by_path["/users/me"]["library"] == {"dist": "extlib", "file": "extlib/__init__.py", "line": 45}
    assert by_path["/own"]["library"] is None


def test_the_capture_is_unchanged_by_applying_it_and_check_still_passes(captured: Path) -> None:
    di_of(captured)
    code, out, _ = run_capture(captured, "--check")
    assert code == 0 and "is up to date" in out


# ── verify ───────────────────────────────────────────────────────────────


def outcomes(repo: Path, *names: str) -> dict[str, verify.Outcome]:
    runtime = AnalysisRuntime(repo, ConfigFile.locate(repo), [])
    return {o.name: o for o in verify.verify(runtime, [c for c in verify.CHECKS if c.name in names])}


@pytest.fixture
def verified(captured: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain: ts_bridge.Toolchain) -> Path:
    monkeypatch.setenv(ts_bridge.TS_DIR_ENV, str(typescript_toolchain.typescript_dir))
    return captured


def test_the_relations_hold_for_library_routes(verified: Path) -> None:
    results = outcomes(
        verified, "handler-locations", "di-matches-routes", "matched-backends-are-live", "backend-content"
    )
    assert {name: (o.status, o.rows) for name, o in results.items()} == {
        "backend-content": ("pass", ()),
        "handler-locations": ("pass", ()),
        "di-matches-routes": ("pass", ()),
        "matched-backends-are-live": ("pass", ()),
    }
    assert results["handler-locations"].detail == "7 routes"
    assert results["di-matches-routes"].detail == "7 routes in both"


def install_interpreter(repo: Path) -> None:
    """The configured interpreter: a script that starts the interpreter running the tests."""
    script = repo / "backend" / ".venv" / "bin" / "python"
    script.parent.mkdir(parents=True)
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def test_the_library_capture_check_passes_when_the_file_equals_a_fresh_capture(verified: Path) -> None:
    install_interpreter(verified)
    result = outcomes(verified, "library-capture")["library-capture"]
    assert (result.status, result.rows) == ("pass", ()), result.detail
    assert result.detail == f"{CAPTURE_FILE}: 3 includes (5 routes), 3 dependencies"


def test_the_library_capture_check_fails_naming_the_entry_that_differs(verified: Path, tmp_path: Path) -> None:
    install_interpreter(verified)
    library = tmp_path / "site" / "extlib" / "__init__.py"
    library.write_text(library.read_text(encoding="utf-8").replace('"/ping"', '"/pong"'), encoding="utf-8")
    result = outcomes(verified, "library-capture")["library-capture"]
    assert result.status == "fail", result.detail
    assert result.rows == ("include backend/pkg/main.py extlib.make_status_router() #1: changed",)


def test_the_library_capture_check_cannot_run_without_the_interpreter_and_says_how_to_fix_it(verified: Path) -> None:
    result = outcomes(verified, "library-capture")["library-capture"]
    assert result.status == "error"
    assert "[backend.capture] python backend/.venv/bin/python: no such file" in result.detail
    assert "`uv sync --locked` in backend" in result.detail


def test_handler_locations_check_that_a_library_row_sits_on_an_include_call() -> None:
    lines = {"backend/app/main.py": ["app = x", "app.include_router(users)", "def me(): pass"]}

    def read(file: str, line: int) -> str:
        return lines[file][line - 1]

    row = {
        "method": "GET",
        "path": "/me",
        "handler": "me",
        "handler_id": "lib:me",
        "file": "backend/app/main.py",
        "library": {"dist": "lib", "version": "1", "file": "lib.py", "line": 3},
    }
    assert verify.handler_location_problems([{**row, "line": 2}], read, lambda *_: None) == []
    [wrong] = verify.handler_location_problems([{**row, "line": 1}], read, lambda *_: None)
    assert "backend/app/main.py:1 is not an include_router call" in wrong


def test_a_chained_include_is_found_on_the_next_lines() -> None:
    lines = ["app", "  .include_router(users)"]
    assert verify._holds_include("f.py", 1, lambda _file, line: lines[line - 1])
    assert verify._holds_include("f.py", 2, lambda _file, line: lines[line - 1])
    assert not verify._holds_include("f.py", 1, lambda _file, line: ["app = x", "  .include_router(a)"][line - 1])


def test_the_route_sets_of_the_map_and_the_scan_must_both_hold_the_library_routes() -> None:
    di = [{"method": "GET", "path": "/me", "handler_id": "lib:me"}]
    assert verify.route_set_differences(di, di) == []
    assert verify.route_set_differences(di, []) == ["only in the dependency map: GET /me lib:me"]


def test_a_matched_library_backend_must_be_a_live_library_route() -> None:
    side = {"method": "GET", "path": "/me", "handler": "me", "file": "backend/app/main.py", "line": 170}
    routes: dict[str, Any] = {
        "backend_routes": [],
        "library_routes": [{**side, "library": {"dist": "lib"}}],
        "matches": [{"backend": side}],
    }
    assert verify.stray_matched_backends(routes) == []
    routes["library_routes"] = []
    assert verify.stray_matched_backends(routes) == ["GET /me -> backend/app/main.py:170"]


# ── status ───────────────────────────────────────────────────────────────


def test_status_reports_the_capture_file_what_was_applied_and_why_it_is_stale(verified: Path) -> None:
    runtime = AnalysisRuntime(verified, ConfigFile.locate(verified), [])
    block = runtime.status().library_capture
    assert block is not None
    assert (block["file"], block["present"], block["app"]) == (CAPTURE_FILE, True, "pkg.main:app")
    assert block["entries"] == {"includes": 3, "routes": 5, "dependencies": 3}
    assert block["applied"] is None and block["problems"] == [] and block["stale"] == []
    assert set(block["distributions"]) == {"extlib", "fastapi"}
    runtime.get_di()
    applied = runtime.status().library_capture
    assert applied is not None
    assert applied["applied"] == {"file": CAPTURE_FILE, "includes": 3, "routes": 5, "dependencies": 3}


def test_status_lists_the_stale_findings_of_the_loaded_analysis(verified: Path) -> None:
    (verified / "backend" / "uv.lock").write_text(lock_text(extlib="1.2.4"), encoding="utf-8")
    runtime = AnalysisRuntime(verified, ConfigFile.locate(verified), [])
    runtime.get_di()
    block = runtime.status().library_capture
    assert block is not None
    assert len(block["stale"]) == 1 and "locks 1.2.4" in block["stale"][0]


def test_status_has_no_capture_block_without_the_table(verified: Path) -> None:
    config = verified / ".code-intel" / "config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text[: text.index("[backend.capture]")] + text[text.index("[frontend]") :], encoding="utf-8")
    stage(verified)
    assert AnalysisRuntime(verified, ConfigFile.locate(verified), []).status().library_capture is None
