"""``code-intel capture`` over a checkout whose app uses an installed library
(``library_fixture``), the clean environment the app is imported in, and the
guarantee that nothing but the three commands runs repository code."""

from __future__ import annotations

import io
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import test_verify
from fixture_repo import hermetic_git
from library_fixture import checkout, lock_text
from mcp_fixtures import write

from code_intel import cli, library_capture, runner, ts_bridge, verify
from code_intel import server as mcp_server
from code_intel.analysis_runtime import AnalysisRuntime
from code_intel.config import ConfigFile

pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")

CAPTURE_FILE = ".code-intel/library-capture.json"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("fastapi")
    hermetic_git(monkeypatch, tmp_path / "home")
    return checkout(tmp_path / "repo", tmp_path / "site")


def run_capture(repo: Path, *args: str, python: str | None = sys.executable) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    option = ["--python", python] if python is not None else []
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["capture", "--repo-root", str(repo), *option, *args])
    return code, out.getvalue(), err.getvalue()


def captured(repo: Path) -> library_capture.LibraryCapture:
    return library_capture.read(repo / CAPTURE_FILE)


def edit_main(repo: Path, old: str, new: str) -> None:
    path = repo / "backend" / "pkg" / "main.py"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    test_verify.git(repo, "add", "-A")


# ── What is captured ─────────────────────────────────────────────────────


def test_the_includes_and_dependencies_the_analysis_cannot_see_are_captured(repo: Path) -> None:
    code, out, err = run_capture(repo)
    assert code == 0, err
    assert out == f"capture: wrote {CAPTURE_FILE}: 3 includes (5 routes), 3 dependencies\n"
    capture = captured(repo)
    assert capture["app"] == "pkg.main:app"
    assert capture["distributions"] == {"extlib": "1.2.3", "fastapi": capture["distributions"]["fastapi"]}
    assert [(i["router"], [(r["method"], r["path"]) for r in i["routes"]]) for i in capture["includes"]] == [
        ("users.get_router(dict)", [("GET", "/me"), ("POST", "/rename")]),
        ("extlib.make_status_router()", [("GET", "/ping"), ("WS", "/live")]),
        ("extlib.make_nested_router()", [("GET", "/v1/inner/item")]),
    ]
    assert [(d.get("binding") or d.get("external"), d.get("bound_to"), d["id"]) for d in capture["dependencies"]] == [
        ("pkg.main:current", "users.current(active=True)", "extlib:Users.current"),
        ("extlib.Settings", None, "extlib:Settings"),
        ("extlib.signed", None, "extlib:signed"),
    ]


def test_a_router_that_is_not_wholly_from_installed_code_stays_a_gap_with_its_reason(repo: Path) -> None:
    code, _, err = run_capture(repo)
    assert code == 0
    assert "include_router at backend/pkg/main.py:28 (extlib.make_mixed_router(own_handler))" in err
    assert "pkg.main:own_handler is defined in the backend package" in err
    assert all("make_mixed_router" not in i["router"] for i in captured(repo)["includes"])


def test_a_route_records_its_handler_location_its_layers_and_its_dependency_trees(repo: Path) -> None:
    run_capture(repo)
    capture = captured(repo)
    by_key = {(i["router"], r["path"]): r for i in capture["includes"] for r in i["routes"]}
    me = by_key["users.get_router(dict)", "/me"]
    assert me["handler_id"] == "extlib:Users.get_router.<locals>.me"
    assert me["location"] == {"dist": "extlib", "file": "extlib/__init__.py", "line": 45}
    assert me["route_deps"] == [{"id": "extlib:audit", "via": "router", "sub": []}]
    assert me["param_deps"] == [{"id": "pkg.deps:get_manager", "param": "user"}]
    rename = by_key["users.get_router(dict)", "/rename"]
    assert [(d["id"], d["via"]) for d in rename["route_deps"]] == [
        ("extlib:audit", "router"),
        ("extlib:count_hits", "route decorator"),
    ]
    live = by_key["extlib.make_status_router()", "/live"]
    assert (live["method"], live["kind"]) == ("WS", "websocket")


def test_a_nested_library_router_keeps_its_prefixes_and_the_layers_of_every_include(repo: Path) -> None:
    run_capture(repo)
    (item,) = (r for i in captured(repo)["includes"] for r in i["routes"] if r["path"].endswith("/item"))
    assert item["path"] == "/v1/inner/item"
    assert [d["id"] for d in item["route_deps"]] == ["extlib:count_hits", "extlib:signed", "extlib:audit"]
    assert {d["via"] for d in item["route_deps"]} == {"router"}


def test_a_library_dependency_has_its_subtree_and_a_package_callable_is_a_leaf(repo: Path) -> None:
    run_capture(repo)
    by_key = {d.get("binding") or d["external"]: d for d in captured(repo)["dependencies"]}
    assert by_key["pkg.main:current"]["sub"] == [
        {"id": "extlib:audit", "param": "token", "sub": []},
        {"id": "pkg.deps:get_manager", "param": "manager"},
    ]
    assert by_key["extlib.signed"]["sub"] == [{"id": "extlib:audit", "param": "audit_value", "sub": []}]
    assert by_key["extlib.Settings"]["sub"] == [{"id": "extlib:audit", "param": "audit_value", "sub": []}]


def test_every_installed_callable_has_a_location_and_a_closure_is_located_at_its_factory(repo: Path) -> None:
    run_capture(repo)
    callables = captured(repo)["callables"]
    assert set(callables) == {
        "extlib:Settings",
        "extlib:Users.current",
        "extlib:audit",
        "extlib:count_hits",
        "extlib:signed",
    }
    assert callables["extlib:Users.current"] == {"dist": "extlib", "file": "extlib/__init__.py", "line": 35}
    assert callables["extlib:Settings"]["line"] == 17


def test_two_includes_of_one_router_text_are_told_apart_by_occurrence(repo: Path) -> None:
    edit_main(
        repo,
        'app.include_router(extlib.make_status_router(), prefix="/status")',
        'app.include_router(extlib.make_status_router(), prefix="/status")\n'
        'app.include_router(extlib.make_status_router(), prefix="/again")',
    )
    code, _, err = run_capture(repo)
    assert code == 0, err
    keys = [(i["router"], i["occurrence"]) for i in captured(repo)["includes"]]
    assert ("extlib.make_status_router()", 1) in keys and ("extlib.make_status_router()", 2) in keys


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            'app.include_router(extlib.make_status_router(), prefix="/status")',
            "if os.environ.get('NEVER'):\n    app.include_router(extlib.make_status_router(), prefix=\"/status\")",
            "the call did not run while the app was imported",
        ),
        (
            'app.include_router(extlib.make_status_router(), prefix="/status")',
            "for part in ('/a', '/b'):\n    app.include_router(extlib.make_status_router(), prefix=part)",
            "the call ran 2 times",
        ),
    ],
)
def test_an_include_that_ran_not_once_stays_a_gap(repo: Path, old: str, new: str, reason: str) -> None:
    edit_main(repo, "import extlib\n", "import os\n\nimport extlib\n")
    edit_main(repo, old, new)
    code, _, err = run_capture(repo)
    assert code == 0, err
    assert reason in err
    assert all(i["router"] != "extlib.make_status_router()" for i in captured(repo)["includes"])


# ── Reproducibility and --check ──────────────────────────────────────────


def test_a_second_capture_writes_the_same_bytes_and_check_passes(repo: Path) -> None:
    assert run_capture(repo)[0] == 0
    first = (repo / CAPTURE_FILE).read_bytes()
    assert run_capture(repo)[0] == 0
    assert (repo / CAPTURE_FILE).read_bytes() == first
    code, out, _ = run_capture(repo, "--check")
    assert code == 0
    assert out == f"capture: {CAPTURE_FILE} is up to date (3 includes (5 routes), 3 dependencies)\n"
    assert (repo / CAPTURE_FILE).read_bytes() == first


def test_check_writes_nothing_and_exits_1_when_there_is_no_file(repo: Path) -> None:
    code, out, err = run_capture(repo, "--check")
    assert code == 1
    assert "library-capture.json does not exist" in out and "run code-intel capture" in err
    assert not (repo / CAPTURE_FILE).exists()


def test_check_exits_1_and_names_the_entry_that_changed(repo: Path, tmp_path: Path) -> None:
    run_capture(repo)
    library = tmp_path / "site" / "extlib" / "__init__.py"
    library.write_text(library.read_text(encoding="utf-8").replace('"/ping"', '"/pong"'), encoding="utf-8")
    before = (repo / CAPTURE_FILE).read_bytes()
    code, out, _ = run_capture(repo, "--check")
    assert code == 1
    assert "include backend/pkg/main.py extlib.make_status_router() #1: changed" in out
    assert (repo / CAPTURE_FILE).read_bytes() == before


def test_check_exits_1_for_a_committed_file_that_is_not_a_capture(repo: Path) -> None:
    write(repo / CAPTURE_FILE, "{}\n")
    code, out, _ = run_capture(repo, "--check")
    assert code == 1 and "the committed file is not valid" in out


def test_check_exits_2_when_the_app_cannot_be_run(repo: Path) -> None:
    run_capture(repo)
    code, out, err = run_capture(repo, "--check", python=str(repo / "no-python"))
    assert (code, out) == (2, "")
    assert "no such file" in err


# ── Refusals ─────────────────────────────────────────────────────────────


def test_an_environment_that_differs_from_the_lock_is_refused_and_nothing_is_written(
    repo: Path,
) -> None:
    (repo / "backend" / "uv.lock").write_text(lock_text(fastapi="0.141.1", extlib="1.2.0"), encoding="utf-8")
    code, out, err = run_capture(repo)
    assert (code, out) == (2, "")
    assert "fastapi " in err and "is installed but backend/uv.lock locks 0.141.1" in err
    assert "extlib 1.2.3 is installed but backend/uv.lock locks 1.2.0" in err
    assert "`uv sync --locked` in backend" in err
    assert not (repo / CAPTURE_FILE).exists()
    assert run_capture(repo, "--check")[0] == 2


def test_a_distribution_the_lock_does_not_name_is_refused(repo: Path) -> None:
    (repo / "backend" / "uv.lock").write_text(
        lock_text().replace('name = "extlib"', 'name = "other"'), encoding="utf-8"
    )
    code, _, err = run_capture(repo)
    assert code == 2 and "package 'extlib' not found" in err


def test_an_app_that_does_not_import_exits_2_with_its_error(repo: Path) -> None:
    edit_main(repo, "app = FastAPI()", 'app = FastAPI()\nraise RuntimeError("settings missing")')
    code, out, err = run_capture(repo)
    assert (code, out) == (2, "")
    assert "did not import" in err and "settings missing" in err


def test_capture_needs_the_config_table(repo: Path) -> None:
    config = repo / ".code-intel" / "config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text[: text.index("[backend.capture]")] + text[text.index("[frontend]") :], encoding="utf-8")
    code, _, err = run_capture(repo)
    assert code == 2 and "needs a [backend.capture] table" in err


def test_the_configured_python_is_used_without_the_option_and_must_exist(repo: Path) -> None:
    code, _, err = run_capture(repo, python=None)
    assert code == 2
    assert "[backend.capture] python backend/.venv/bin/python: no such file" in err


def test_the_lock_is_required_to_check_versions(repo: Path) -> None:
    config = repo / ".code-intel" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace('lock = "backend/uv.lock"\n', ""), encoding="utf-8")
    code, _, err = run_capture(repo)
    assert code == 2 and "[backend] lock is not set" in err


# ── The clean environment ────────────────────────────────────────────────


def test_the_app_is_imported_with_the_configured_variables_and_none_of_the_parents(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMBIENT_SECRET", "leaks")
    edit_main(
        repo,
        "app = FastAPI()",
        "import os\n\nassert 'AMBIENT_SECRET' not in os.environ, 'the parent environment leaked'\n"
        "assert os.environ['SYNTHETIC'] == '1'\napp = FastAPI()",
    )
    code, _, err = run_capture(repo)
    assert code == 0, err


def test_the_clean_environment_keeps_only_the_inherited_names_and_the_configured_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in runner.INHERITED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("TOKEN", "secret")
    assert runner.clean_environment({"KEY": "v", "LANG": "en"}) == {"PATH": "/bin", "LANG": "en", "KEY": "v"}


@pytest.mark.skipif(os.name == "nt", reason="the fake interpreter is a shell script")
def test_a_timeout_kills_the_whole_process_group_so_a_child_holding_the_pipe_cannot_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    fake = tmp_path / "python"
    fake.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nsleep 30\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(runner, "TIMEOUT_S", 1)
    began = time.monotonic()
    with pytest.raises(runner.OracleRunError, match="TimeoutExpired"):
        runner.run_oracle(fake, [], {})
    assert time.monotonic() - began < 10
    child = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the child that held the pipe outlived the timeout")


# ── Nothing else runs repository code ────────────────────────────────────

NEVER_IMPORTED_BY_THE_SERVER = (
    "code_intel.runner",
    "code_intel.capture",
    "code_intel.verify",
    "code_intel.runtime_check",
)


def test_importing_the_mcp_server_never_imports_a_module_that_starts_the_app() -> None:
    probe = (
        "import sys, code_intel.server\n"
        f"print([name for name in {NEVER_IMPORTED_BY_THE_SERVER!r} if name in sys.modules])"
    )
    done = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"


MARKER_CONFIG_TABLE = '\n[backend.capture]\npython = "bin/python"\n'


@pytest.fixture
def marker_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain: ts_bridge.Toolchain) -> Path:
    """A checkout whose configured interpreter is a script that leaves a marker file when it starts."""
    hermetic_git(monkeypatch, tmp_path / "home")
    monkeypatch.setenv(ts_bridge.TS_DIR_ENV, str(typescript_toolchain.typescript_dir))
    root = test_verify.checkout(tmp_path / "repo")
    config = root / ".code-intel" / "config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("\n[frontend]", MARKER_CONFIG_TABLE + "\n[frontend]", 1), encoding="utf-8")
    script = root / "bin" / "python"
    script.parent.mkdir()
    script.write_text('#!/bin/sh\ntouch "$(dirname "$0")/started"\nexit 1\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    test_verify.git(root, "add", "-A")
    return root


def started(root: Path) -> bool:
    return (root / "bin" / "started").exists()


def run_quietly(*argv: str) -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return cli.main(list(argv))


@pytest.mark.skipif(os.name == "nt", reason="the marker interpreter is a shell script")
def test_the_marker_interpreter_shows_when_the_app_is_started(marker_repo: Path) -> None:
    assert not started(marker_repo)
    assert run_capture(marker_repo, python=None)[0] == 2
    assert started(marker_repo)


@pytest.mark.skipif(os.name == "nt", reason="the marker interpreter is a shell script")
@pytest.mark.parametrize(
    "argv",
    [
        ["config-check"],
        ["dump", "di"],
        ["dump", "routes"],
        ["dump", "symbols", "Items"],
        ["doc-check"],
        ["summary"],
        ["verify", *(c.name for c in verify.CHECKS if c.name != "library-capture")],
    ],
    ids=lambda argv: " ".join(argv[:3]),
)
def test_no_other_command_starts_the_interpreter(marker_repo: Path, argv: list[str]) -> None:
    assert run_quietly(*argv, "--repo-root", str(marker_repo)) == 0
    assert not started(marker_repo)


@pytest.mark.skipif(os.name == "nt", reason="the marker interpreter is a shell script")
def test_only_the_library_capture_check_of_verify_starts_the_interpreter(marker_repo: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = cli.main(["verify", "library-capture", "--repo-root", str(marker_repo)])
    assert code == 2 and started(marker_repo)
    assert "ERROR   library-capture" in out.getvalue() and "the app did not import" in out.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="the marker interpreter is a shell script")
def test_reading_the_capture_file_never_starts_the_interpreter(marker_repo: Path) -> None:
    write(marker_repo / CAPTURE_FILE, "{}\n")
    test_verify.git(marker_repo, "add", "-A")
    assert run_quietly("dump", "di", "--repo-root", str(marker_repo)) == 2
    assert not started(marker_repo)


@pytest.mark.skipif(os.name == "nt", reason="the marker interpreter is a shell script")
def test_no_mcp_tool_starts_the_interpreter(marker_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = AnalysisRuntime(marker_repo, ConfigFile.locate(marker_repo), [])
    monkeypatch.setattr(mcp_server, "runtime", runtime)
    assert "Route matching summary:" in mcp_server.route_match()
    assert mcp_server.route_match(path="/api/items")
    assert mcp_server.di_deps(path="/api/items")
    assert mcp_server.status(load_counts=True)
    assert not started(marker_repo)
