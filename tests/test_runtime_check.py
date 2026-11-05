"""``code-intel runtime-check``: which differences between the running app and
the static analysis are explained, the report, and the command over a real
checkout that imports a real FastAPI app."""

from __future__ import annotations

import io
import os
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from fixture_repo import hermetic_git
from test_verify import MAIN, checkout, git

from code_intel import cli, runner, runtime_check
from code_intel.config import CaptureConfig
from code_intel.oracles import fastapi_oracle

LIB = "backend/.venv/lib/python3.14/site-packages/extlib/router.py"


def dep(dep_id: str, param: str | None = None, sub: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": dep_id, "param": param, "sub": sub or [], **extra}


def runtime_route(path: str = "/items", **overrides: Any) -> dict[str, Any]:
    return {
        "method": "GET",
        "path": path,
        "kind": "http",
        "handler": "list_items",
        "handler_id": "pkg.api:list_items",
        "file": "backend/pkg/api.py",
        "line": 2,
        "route_deps": [],
        "param_deps": [],
        "closure": [],
        **overrides,
    }


def analyzer_route(path: str = "/items", **overrides: Any) -> dict[str, Any]:
    return {
        "method": "GET",
        "path": path,
        "kind": "http",
        "handler": "list_items",
        "handler_id": "pkg.api:list_items",
        "file": "backend/pkg/api.py",
        "line": 2,
        "order": 0,
        "deps": [],
        "all_deps": [],
        **overrides,
    }


def direct(dep_id: str, param: str) -> dict[str, Any]:
    return {"id": dep_id, "via": "direct", "param": param}


def function(dep_id: str, kind: str = "function", sub_deps: list[str] | None = None) -> dict[str, Any]:
    return {"id": dep_id, "kind": kind, "sub_deps": sub_deps or []}


def di_with(
    routes: list[dict[str, Any]], *functions: dict[str, Any], limitations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "routes": routes,
        "dependency_functions": {f["id"]: f for f in functions},
        "analysis_limitations": limitations or [],
        "diagnostics": [],
        "app": {"redirect_slashes": True, "fastapi_version": "0.162.0", "include_model": "live"},
    }


def classified(runtime_rows: list[dict[str, Any]], di: dict[str, Any]) -> list[tuple[str, str | None]]:
    runtime = {"routes": runtime_rows, "redirect_slashes": True}
    return [(c.difference.cls, c.reason) for c in runtime_check.classify(runtime, di)]


def gap(kind: str = "unresolved_include", line: int = 150) -> dict[str, Any]:
    return {"kind": kind, "file": "backend/app/main.py", "line": line, "message": "m"}


# ── Routes the analysis lacks ────────────────────────────────────────────


def test_a_route_from_installed_code_is_explained_by_a_limitation_that_can_hide_routes() -> None:
    runtime_rows = [runtime_route(), runtime_route("/auth/login", file=LIB, handler_id="extlib.router:login")]
    di = di_with([analyzer_route()], limitations=[gap(line=150), gap("opaque_router_call", 160)])
    [(cls, reason)] = classified(runtime_rows, di)
    assert cls == "route_missing"
    assert reason == (
        "the handler is defined in installed code and the analysis reported "
        "opaque_router_call at backend/app/main.py:160, unresolved_include at backend/app/main.py:150"
    )


def test_a_missing_route_is_unexplained_without_a_limitation_or_when_its_handler_is_the_repositorys() -> None:
    installed = runtime_route("/auth/login", file=LIB)
    assert classified([installed], di_with([])) == [("route_missing", None)]
    own = runtime_route("/own")
    assert classified([own], di_with([], limitations=[gap()])) == [("route_missing", None)]
    harmless = di_with([], limitations=[{"kind": "conditional_import", "file": "a.py", "line": 1, "message": "m"}])
    assert classified([installed], harmless) == [("route_missing", None)]


def test_a_route_with_no_source_file_counts_as_installed() -> None:
    assert runtime_check.is_installed(None)
    assert runtime_check.is_installed("/usr/lib/python3/site-packages/x.py")
    assert runtime_check.is_installed("backend/venv/lib/x.py")
    assert not runtime_check.is_installed("backend/app/api.py")


def test_an_extra_route_a_changed_handler_and_a_moved_def_are_never_explained() -> None:
    di = di_with([analyzer_route("/x"), analyzer_route(handler_id="pkg.api:other", line=9)], limitations=[gap()])
    assert classified([runtime_route()], di) == [
        ("handler_mismatch", None),
        ("location_mismatch", None),
        ("route_extra", None),
    ]


# ── Dependencies the analysis cannot see into ────────────────────────────

LIB_USER = "extlib.auth:Authenticator.current_user"
USER = "app.core.auth:current_active_user"
SESSION = "app.core.database:get_session"
WORKSPACE = "app.core.workspace:current_workspace"


def lib_user_tree() -> dict[str, Any]:
    return dep(
        LIB_USER, "user", [dep("app.core.auth:get_manager", "m", [dep(SESSION, "s")]), dep("extlib:bearer", "jwt")]
    )


def test_an_external_dependency_standing_in_for_a_library_one_is_explained_with_everything_beneath_it() -> None:
    runtime_rows = [
        runtime_route(
            param_deps=[dep(SESSION, "session"), lib_user_tree()],
            closure=[SESSION, LIB_USER, "app.core.auth:get_manager", "extlib:bearer"],
        )
    ]
    di = di_with(
        [analyzer_route(deps=[direct(SESSION, "session"), direct(USER, "user")], all_deps=[SESSION, USER])],
        function(SESSION),
        function(USER, "external"),
    )
    assert classified(runtime_rows, di) == [
        ("param_deps_mismatch", _STANDS_IN),
        ("closure_mismatch", _STANDS_IN),
    ]


_STANDS_IN = "a dependency the analysis cannot see into (external or unresolved) stands in its place"


def test_an_external_dependency_beneath_a_transparent_one_explains_the_closure() -> None:
    runtime_rows = [
        runtime_route(
            param_deps=[dep(WORKSPACE, "ctx", [lib_user_tree(), dep(SESSION, "session")])],
            closure=[WORKSPACE, LIB_USER, "app.core.auth:get_manager", SESSION, "extlib:bearer"],
        )
    ]
    di = di_with(
        [analyzer_route(deps=[direct(WORKSPACE, "ctx")], all_deps=[WORKSPACE, USER, SESSION])],
        function(WORKSPACE, sub_deps=[USER, SESSION]),
        function(USER, "external"),
        function(SESSION),
    )
    assert classified(runtime_rows, di) == [("closure_mismatch", _STANDS_IN)]


def test_an_unresolved_dependency_explains_its_runtime_counterpart() -> None:
    runtime_rows = [
        runtime_route(param_deps=[dep("fastapi.security:Form", "credentials")], closure=["fastapi.security:Form"])
    ]
    unresolved = "unresolved:app.api:Form"
    di = di_with(
        [analyzer_route(deps=[direct(unresolved, "credentials")], all_deps=[unresolved])],
        function(unresolved, "unresolved"),
    )
    assert [reason for _, reason in classified(runtime_rows, di)] == [_STANDS_IN, _STANDS_IN]


def test_a_difference_in_a_dependency_the_analysis_can_see_is_not_explained_by_its_neighbour() -> None:
    runtime_rows = [
        runtime_route(
            param_deps=[dep("app.other:changed", "a"), lib_user_tree()],
            closure=["app.other:changed", LIB_USER, "app.core.auth:get_manager", SESSION, "extlib:bearer"],
        )
    ]
    di = di_with(
        [
            analyzer_route(
                deps=[direct("app.mine:changed", "a"), direct(USER, "user")], all_deps=["app.mine:changed", USER]
            )
        ],
        function("app.mine:changed"),
        function(USER, "external"),
    )
    assert classified(runtime_rows, di) == [("param_deps_mismatch", None), ("closure_mismatch", None)]


def test_a_runtime_dependency_the_analysis_has_no_stand_in_for_is_not_explained() -> None:
    runtime_rows = [
        runtime_route(param_deps=[dep(SESSION, "session"), dep("app:extra", "extra")], closure=[SESSION, "app:extra"])
    ]
    di = di_with([analyzer_route(deps=[direct(SESSION, "session")], all_deps=[SESSION])], function(SESSION))
    assert classified(runtime_rows, di) == [("param_deps_mismatch", None), ("closure_mismatch", None)]


def test_route_level_dependencies_pair_by_position() -> None:
    runtime_rows = [
        runtime_route(
            route_deps=[{**lib_user_tree(), "via": "router"}],
            closure=[LIB_USER, "app.core.auth:get_manager", SESSION, "extlib:bearer"],
        )
    ]
    di = di_with(
        [analyzer_route(deps=[{"id": USER, "via": "router", "param": None}], all_deps=[USER])],
        function(USER, "external"),
    )
    assert classified(runtime_rows, di) == [("route_deps_mismatch", _STANDS_IN), ("closure_mismatch", _STANDS_IN)]
    longer = di_with(
        [
            analyzer_route(
                deps=[{"id": USER, "via": "router", "param": None}, {"id": SESSION, "via": "router", "param": None}],
                all_deps=[USER, SESSION],
            )
        ],
        function(USER, "external"),
        function(SESSION),
    )
    assert classified(runtime_rows, longer)[0] == ("route_deps_mismatch", None)


# ── Report ───────────────────────────────────────────────────────────────


def sample() -> list[runtime_check.Classified]:
    def item(cls: str, text: str, reason: str | None) -> runtime_check.Classified:
        return runtime_check.Classified(fastapi_oracle.Difference(cls, ("GET", "/x", 0), text), reason)

    return [
        item("route_missing", "GET /a", "because"),
        item("route_missing", "GET /b", "because"),
        item("route_extra", "GET /c", None),
        item("handler_mismatch", "x" * 300, "other"),
    ]


def test_render_counts_each_class_and_lists_unexplained_rows_first() -> None:
    lines = runtime_check.render(sample()).splitlines()
    assert lines[0] == "route_missing: 2 (2 explained)"
    assert lines[1] == "  explained (2): because"
    assert lines[2:4] == ["    ~ GET /a", "    ~ GET /b"]
    assert "route_extra: 1" in lines and "  ! GET /c" in lines
    assert lines[-1] == "runtime-check: 4 differences, 3 explained, 1 unexplained"
    clipped = next(line for line in lines if line.startswith("    ~ xxx"))
    assert clipped.endswith("…") and len(clipped) == 6 + runtime_check.CLIP


def test_render_caps_long_lists_and_says_how_many_it_left_out() -> None:
    many = [
        runtime_check.Classified(fastapi_oracle.Difference("route_extra", ("GET", f"/{i}", 0), f"GET /{i}"), None)
        for i in range(runtime_check.SHOWN + 4)
    ]
    assert "  ! ... 4 more" in runtime_check.render(many).splitlines()
    assert runtime_check.render([]).splitlines()[-1] == "runtime-check: 0 differences, 0 explained, 0 unexplained"


def test_a_version_note_appears_only_when_the_versions_differ() -> None:
    di = {"app": {"fastapi_version": "0.162.0", "include_model": "live"}}
    note = runtime_check.version_note({"fastapi_version": "0.141.1"}, di)
    assert note is not None and "FastAPI 0.141.1" in note and "locked 0.162.0" in note
    assert runtime_check.version_note({"fastapi_version": "0.162.0"}, di) is None
    assert runtime_check.version_note({"fastapi_version": "0.141.1"}, {"app": {"fastapi_version": None}}) is None


def test_python_is_taken_from_the_current_directory_then_the_repository_without_following_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    (repo / ".venv" / "bin").mkdir(parents=True)
    elsewhere.mkdir()
    real = tmp_path / "real-python"
    real.write_text("")
    link = repo / ".venv" / "bin" / "python"
    link.symlink_to(real)
    monkeypatch.chdir(elsewhere)
    assert runner.interpreter(Path(".venv/bin/python"), None, repo) == link
    assert runner.interpreter(link, None, repo) == link
    with pytest.raises(runner.OracleRunError, match="--python nope/python: no such file"):
        runner.interpreter(Path("nope/python"), None, repo)


def test_the_configured_python_is_taken_from_the_repository_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    (repo / "bin").mkdir(parents=True)
    (elsewhere / "bin").mkdir(parents=True)
    (repo / "bin" / "python").write_text("")
    (elsewhere / "bin" / "python").write_text("")
    monkeypatch.chdir(elsewhere)
    capture = CaptureConfig(python="bin/python")
    assert runner.interpreter(None, capture, repo) == repo / "bin" / "python"
    assert runner.interpreter(Path("bin/python"), capture, repo) == elsewhere / "bin" / "python"
    (repo / "bin" / "python").unlink()
    with pytest.raises(runner.OracleRunError, match=r"\[backend.capture\] python bin/python: no such file"):
        runner.interpreter(None, capture, repo)


def test_without_an_option_or_a_config_there_is_no_python(tmp_path: Path) -> None:
    with pytest.raises(runner.OracleRunError, match=r"give --python, or set python in \[backend.capture\]"):
        runner.interpreter(None, None, tmp_path)


def test_the_oracle_calls_an_app_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "factory_mod.py").write_text("def make():\n    return 'app'\n\nthing = 'plain'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert fastapi_oracle.load_app("factory_mod:make()") == "app"
    assert fastapi_oracle.load_app("factory_mod:thing") == "plain"


# ── A real checkout and a real FastAPI import ────────────────────────────


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("fastapi")
    hermetic_git(monkeypatch, tmp_path / "home")
    return checkout(tmp_path / "repo")


def run_cli(repo: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["runtime-check", "--repo-root", str(repo), "--python", sys.executable, *args])
    return code, out.getvalue(), err.getvalue()


def test_an_app_the_analysis_models_exactly_has_no_differences(repo: Path) -> None:
    code, out, err = run_cli(repo)
    assert code == 0, out + err
    assert out.splitlines()[-1] == "runtime-check: 0 differences, 0 explained, 0 unexplained"
    assert "2 runtime routes, 2 analyzed" in out


def test_a_route_the_analysis_cannot_see_is_an_unexplained_difference(repo: Path) -> None:
    main = repo / "backend" / "service_backend" / "main.py"
    main.write_text(MAIN + "\nexec(\"@app.get('/hidden')\\ndef hidden():\\n    return 1\\n\")\n", encoding="utf-8")
    git(repo, "add", "-A")
    code, out, _ = run_cli(repo)
    assert code == 1
    assert "route_missing: 1" in out and "  ! GET /hidden" in out
    assert out.splitlines()[-1] == "runtime-check: 1 differences, 0 explained, 1 unexplained"


def test_set_and_env_file_reach_the_app_before_it_is_imported(repo: Path, tmp_path: Path) -> None:
    main = repo / "backend" / "service_backend" / "main.py"
    gated = MAIN.replace(
        "app.include_router(router)",
        'if os.environ.get("EXTRA_ROUTES") == "1":\n'
        '    @app.get("/extra")\n    def extra() -> int:\n        return 1\n\n\n'
        "app.include_router(router)",
    )
    main.write_text("import os\n" + gated, encoding="utf-8")
    git(repo, "add", "-A")
    code, out, _ = run_cli(repo)
    assert code == 1 and "route_extra: 1" in out
    assert run_cli(repo, "--set", "EXTRA_ROUTES=1")[0] == 0
    env_file = tmp_path / "app.env"
    env_file.write_text("# comment\nEXTRA_ROUTES=1\n", encoding="utf-8")
    assert run_cli(repo, "--env-file", str(env_file))[0] == 0


def test_an_app_that_does_not_import_exits_2_with_its_error(repo: Path) -> None:
    main = repo / "backend" / "service_backend" / "main.py"
    main.write_text(MAIN + '\nraise RuntimeError("settings missing")\n', encoding="utf-8")
    git(repo, "add", "-A")
    code, out, err = run_cli(repo)
    assert (code, out) == (2, "")
    assert "did not import" in err and "settings missing" in err


def test_a_python_that_does_not_exist_exits_2(repo: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["runtime-check", "--repo-root", str(repo), "--python", str(repo / "no-python")])
    assert code == 2 and "no such file" in err.getvalue()


def test_the_command_exits_2_without_a_python_from_the_option_or_the_config(repo: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["runtime-check", "--repo-root", str(repo)])
    assert (code, out.getvalue()) == (2, "")
    assert "give --python, or set python in [backend.capture]" in err.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="the configured interpreter is a shell script")
def test_the_configured_python_and_env_are_used_without_the_option(repo: Path) -> None:
    config = repo / ".code-intel" / "config.toml"
    text = config.read_text(encoding="utf-8")
    launcher = repo / "py"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    configured = text.replace(
        "\n[frontend]", '\n[backend.capture]\npython = "py"\nenv = { EXTRA_ROUTES = "1" }\n\n[frontend]', 1
    )
    config.write_text(configured, encoding="utf-8")
    main = repo / "backend" / "service_backend" / "main.py"
    main.write_text(
        "import os\n"
        + MAIN.replace(
            "app.include_router(router)",
            'if os.environ.get("EXTRA_ROUTES") == "1":\n'
            '    @app.get("/extra")\n    def extra() -> int:\n        return 1\n\n\n'
            "app.include_router(router)",
        ),
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["runtime-check", "--repo-root", str(repo)])
    assert code == 0, out.getvalue() + err.getvalue()
    assert "3 runtime routes, 3 analyzed" in out.getvalue()


def test_the_app_is_imported_with_a_clean_environment(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMBIENT_FLAG", "1")
    main = repo / "backend" / "service_backend" / "main.py"
    main.write_text(
        "import os\n"
        + MAIN.replace(
            "app.include_router(router)",
            'if os.environ.get("AMBIENT_FLAG") == "1":\n'
            '    @app.get("/ambient")\n    def ambient() -> int:\n        return 1\n\n\n'
            "app.include_router(router)",
        ),
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    code, out, _ = run_cli(repo)
    assert code == 1
    assert "2 runtime routes, 3 analyzed" in out and "  ! GET /ambient" in out
