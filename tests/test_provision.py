"""Provisioning TypeScript on first use: the fake npm and a private cache, then the real helper through the CLI and the MCP tools."""

from __future__ import annotations

import functools
import json
import os
import stat
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from fake_npm import NODE, FakeNpm, which
from fixture_repo import FULL_CONFIG, git_init, hermetic_git, make_repo, write

from code_intel import analysis_runtime, cli, provision, setup, toolchain_pins, ts_bridge
from code_intel import server as mcp_server
from code_intel.config import ConfigFile


def _env(tmp_path: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return {ts_bridge.CACHE_ENV: str(tmp_path / "cache"), **(extra or {})}


def _find(
    tmp_path: Path, npm: FakeNpm, extra: Mapping[str, str] | None = None, *, background: bool = False
) -> ts_bridge.Toolchain:
    return provision.find_toolchain(
        tmp_path / "repo", background=background, env=_env(tmp_path, extra), which=which, run=npm
    )


def test_a_missing_toolchain_is_provisioned_once_with_one_stderr_line(tmp_path: Path, capsys) -> None:
    npm = FakeNpm()
    toolchain = _find(tmp_path, npm)
    assert toolchain.available
    assert toolchain.typescript_dir == ts_bridge.install_dir(_env(tmp_path)) / "node_modules" / "typescript"
    [call] = npm.npm_calls
    assert call["cmd"][1:] == ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "installing TypeScript into" in err

    assert _find(tmp_path, npm).available
    assert len(npm.npm_calls) == 1
    assert capsys.readouterr().err == ""


def test_two_provisions_in_one_process_install_once(tmp_path: Path) -> None:
    npm = FakeNpm()
    npm.before_exit = lambda cwd: threading.Event().wait(0.3)
    found: list[ts_bridge.Toolchain] = []
    threads = [threading.Thread(target=lambda: found.append(_find(tmp_path, npm))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(npm.npm_calls) == 1
    assert [toolchain.available for toolchain in found] == [True, True]


def test_the_background_mode_answers_provisioning_then_the_toolchain(tmp_path: Path, capsys) -> None:
    npm = FakeNpm()
    release = threading.Event()
    npm.before_exit = lambda cwd: release.wait(30)
    first = _find(tmp_path, npm, background=True)
    assert first.problem is not None and first.problem.code == "frontend_provisioning"
    assert "shortly" in first.problem.fix or "finishes" in first.problem.fix
    assert _find(tmp_path, npm, background=True).problem == first.problem
    release.set()
    provision._attempts[ts_bridge.install_dir(_env(tmp_path))].done.wait(30)
    assert _find(tmp_path, npm, background=True).available
    assert len(npm.npm_calls) == 1
    assert capsys.readouterr().err == ""


def test_a_failed_attempt_names_the_cause_and_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    npm = FakeNpm(npm_exit=1)
    problem = _find(tmp_path, npm).problem
    assert problem is not None and problem.code == "typescript_not_found"
    assert "npm ci exited with code 1 (npm error 404)" in problem.fix
    assert 'run "code-intel setup" to retry' in problem.fix
    assert _find(tmp_path, npm).problem == problem
    assert len(npm.npm_calls) == 1

    other_lock = tmp_path / "package-lock.json"
    other_lock.write_bytes(toolchain_pins.HELPER_LOCK.read_bytes() + b"\n")
    monkeypatch.setattr(toolchain_pins, "HELPER_LOCK", other_lock)
    _find(tmp_path, npm)
    assert len(npm.npm_calls) == 2


def test_missing_npm_is_the_cause(tmp_path: Path) -> None:
    toolchain = provision.find_toolchain(
        tmp_path / "repo", env=_env(tmp_path), which=lambda name: NODE if name == "node" else None, run=FakeNpm()
    )
    assert toolchain.problem is not None
    assert toolchain.problem.code == "typescript_not_found"
    assert "automatic setup failed: npm not found on PATH" in toolchain.problem.fix
    assert "code-intel setup" in toolchain.problem.fix


def test_missing_node_is_reported_as_node_and_nothing_is_installed(tmp_path: Path) -> None:
    npm = FakeNpm()
    toolchain = provision.find_toolchain(tmp_path / "repo", env=_env(tmp_path), which=lambda name: None, run=npm)
    assert toolchain.problem is not None and toolchain.problem.code == "node_not_found"
    assert npm.npm_calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {provision.NO_AUTO_ENV: "1"},
        {ts_bridge.TS_DIR_ENV: "elsewhere/typescript"},
        {ts_bridge.NODE_ENV: NODE},
    ],
    ids=["opt-out", "typescript-dir", "node"],
)
def test_an_opt_out_or_an_explicit_override_is_never_provisioned_over(tmp_path: Path, extra: dict[str, str]) -> None:
    npm = FakeNpm()
    toolchain = _find(tmp_path, npm, extra)
    assert toolchain.problem is not None and toolchain.problem.code in ("typescript_not_found", "node_not_found")
    assert npm.npm_calls == []
    assert not ts_bridge.install_dir(_env(tmp_path)).exists()


def test_zero_does_not_opt_out(tmp_path: Path) -> None:
    assert _find(tmp_path, FakeNpm(), {provision.NO_AUTO_ENV: "0"}).available


@pytest.fixture
def frontend_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic_git(monkeypatch, tmp_path / "home")
    repo = make_repo(git_init(tmp_path / "repo"), FULL_CONFIG)
    write(repo / "frontend" / "components" / "App.tsx", "export const App = () => <Page />;\n")
    return repo


def _link_script(tmp_path: Path, typescript: Path) -> Path:
    """A ``bin`` directory whose ``npm ci`` links an existing TypeScript instead of downloading one."""
    bin_dir = tmp_path / "bin"
    npm = write(bin_dir / "npm", f'#!/bin/sh\nmkdir node_modules && ln -s "{typescript}" node_modules/typescript\n')
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_the_cli_provisions_on_first_use_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frontend_repo: Path,
    typescript_toolchain: ts_bridge.Toolchain,
    capsys,
) -> None:
    assert typescript_toolchain.node is not None and typescript_toolchain.typescript_dir is not None
    cache = tmp_path / "cache"
    monkeypatch.delenv(provision.NO_AUTO_ENV)
    monkeypatch.delenv(ts_bridge.TS_DIR_ENV, raising=False)
    monkeypatch.delenv(ts_bridge.NODE_ENV, raising=False)
    monkeypatch.setenv(ts_bridge.CACHE_ENV, str(cache))
    node_dir = os.path.dirname(typescript_toolchain.node)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [str(_link_script(tmp_path, typescript_toolchain.typescript_dir)), node_dir, os.environ["PATH"]]
        ),
    )

    assert cli.main(["dump", "jsx", "Page", "--json", "--repo-root", str(frontend_repo)]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["complete"] is True
    assert [row["file"] for row in result["usages"]] == ["frontend/components/App.tsx"]
    assert captured.err.count("installing TypeScript into") == 1
    assert ts_bridge.install_dir({ts_bridge.CACHE_ENV: str(cache)}).is_dir()


def test_the_mcp_server_answers_provisioning_and_then_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frontend_repo: Path, typescript_toolchain: ts_bridge.Toolchain
) -> None:
    assert typescript_toolchain.node is not None and typescript_toolchain.typescript_dir is not None
    node = typescript_toolchain.node
    env = _env(tmp_path)
    npm = FakeNpm(typescript=typescript_toolchain.typescript_dir)
    release = threading.Event()
    npm.before_exit = lambda cwd: release.wait(30)

    def find(name: str) -> str | None:
        return node if name == "node" else which(name)

    def run(cmd, **kwargs):
        return npm(cmd, **kwargs) if cmd[0] != node else npm([NODE, *cmd[1:]], **kwargs)

    runtime = analysis_runtime.AnalysisRuntime(
        frontend_repo,
        ConfigFile.locate(frontend_repo),
        [],
        find_toolchain=functools.partial(provision.find_toolchain, background=True, env=env, which=find, run=run),
    )
    monkeypatch.setattr(mcp_server, "runtime", runtime)

    waiting = json.loads(mcp_server.jsx_usage("Page", output_format="json"))
    assert (waiting["error"], waiting["problem"]) == ("frontend_unavailable", "frontend_provisioning")
    assert "shortly" in waiting["message"] or "background" in waiting["message"]

    both = json.loads(mcp_server.symbol_resolve("Page", output_format="json"))
    assert [d["problem"] for d in both["diagnostics"]] == ["frontend_provisioning"]
    assert [lim["kind"] for lim in both["analysis_limitations"]] == ["frontend_provisioning"]

    di = json.loads(mcp_server.di_deps("anything", output_format="json"))
    assert "frontend_provisioning" not in json.dumps(di)

    release.set()
    assert provision._attempts[ts_bridge.install_dir(env)].done.wait(60)
    done = json.loads(mcp_server.jsx_usage("Page", output_format="json"))
    assert done["complete"] is True
    assert [row["file"] for row in done["usages"]] == ["frontend/components/App.tsx"]
    assert len(npm.npm_calls) == 1
    assert setup.install(env, which=find, run=run)[1] is False
