"""``code-intel setup``: the shared TypeScript install, with a fake npm and a private cache."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_npm import NODE, NPM, FakeNpm, which

from code_intel import cli, setup, ts_bridge


def _env(tmp_path: Path) -> dict[str, str]:
    return {ts_bridge.CACHE_ENV: str(tmp_path / "cache")}


def _entries(target: Path) -> list[str]:
    """What the cache holds besides the lock file."""
    return sorted(p.name for p in target.parent.iterdir() if not p.name.endswith(".lock"))


def test_install_runs_npm_ci_once_in_the_shared_cache(tmp_path: Path) -> None:
    npm = FakeNpm()
    toolchain, ran = setup.install(_env(tmp_path), which=which, run=npm)
    target = ts_bridge.install_dir(_env(tmp_path))
    assert ran and toolchain.available
    assert toolchain.typescript_dir == target / "node_modules" / "typescript"
    [call] = npm.npm_calls
    assert call["cmd"] == [NPM, "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    assert call["files"] == ["package-lock.json", "package.json"]
    assert (target / "package-lock.json").read_bytes() == (ts_bridge.HELPER_DIR / "package-lock.json").read_bytes()
    assert _entries(target) == [target.name]


def test_a_second_run_does_not_call_npm(tmp_path: Path) -> None:
    npm = FakeNpm()
    setup.install(_env(tmp_path), which=which, run=npm)
    toolchain, ran = setup.install(_env(tmp_path), which=which, run=npm)
    assert not ran and toolchain.available
    assert len(npm.npm_calls) == 1


def test_the_install_directory_is_named_by_the_lock_digest(tmp_path: Path) -> None:
    target = ts_bridge.install_dir(_env(tmp_path))
    assert target.parent == tmp_path / "cache" / "ts"
    assert len(target.name) == 16 and set(target.name) <= set("0123456789abcdef")
    assert ts_bridge.cache_dir({}) == Path.home() / ".cache" / "code-intel"


def test_the_typescript_dir_override_does_not_redirect_the_install(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    env = {**_env(tmp_path), ts_bridge.TS_DIR_ENV: str(other)}
    toolchain, ran = setup.install(env, which=which, run=FakeNpm())
    assert ran and toolchain.typescript_dir == ts_bridge.install_dir(env) / "node_modules" / "typescript"


def test_a_failing_npm_leaves_nothing_behind(tmp_path: Path) -> None:
    npm = FakeNpm(npm_exit=1)
    with pytest.raises(setup.SetupError, match="npm ci exited with code 1:\nnpm error 404\nnpm error not found"):
        setup.install(_env(tmp_path), which=which, run=npm)
    assert _entries(ts_bridge.install_dir(_env(tmp_path))) == []


def test_an_unrunnable_npm_is_named(tmp_path: Path) -> None:
    def run(cmd, **kwargs):
        if cmd[0] == NODE:
            return SimpleNamespace(returncode=0, stdout=b"v24.21.0\n", stderr=b"")
        raise subprocess.TimeoutExpired(cmd, 600)

    with pytest.raises(setup.SetupError, match="could not run npm \\(TimeoutExpired"):
        setup.install(_env(tmp_path), which=which, run=run)
    assert _entries(ts_bridge.install_dir(_env(tmp_path))) == []


def test_missing_node_or_npm_stops_before_installing(tmp_path: Path) -> None:
    npm = FakeNpm()
    with pytest.raises(setup.SetupError, match="node not found on PATH. Fix: install Node.js 18"):
        setup.install(_env(tmp_path), which=lambda name: None, run=npm)
    with pytest.raises(setup.SetupError, match="node v16.20.0 is older"):
        setup.install(_env(tmp_path), which=which, run=FakeNpm(node_out=b"v16.20.0\n"))
    with pytest.raises(setup.SetupError, match="npm not found on PATH"):
        setup.install(_env(tmp_path), which=lambda name: NODE if name == "node" else None, run=npm)
    assert npm.npm_calls == []


def test_an_install_that_is_not_usable_is_reported(tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="not usable: .*no compiler API"):
        setup.install(_env(tmp_path), which=which, run=FakeNpm(version="7.0.2"))


def test_a_damaged_install_is_replaced(tmp_path: Path) -> None:
    target = ts_bridge.install_dir(_env(tmp_path))
    (target / "node_modules").mkdir(parents=True)
    (target / "leftover").write_text("x")
    npm = FakeNpm()
    toolchain, ran = setup.install(_env(tmp_path), which=which, run=npm)
    assert ran and toolchain.available and not (target / "leftover").exists()


def test_a_run_that_loses_the_race_keeps_the_winners_install(tmp_path: Path) -> None:
    npm = FakeNpm()
    target = ts_bridge.install_dir(_env(tmp_path))

    def winner_finishes_first(cwd: Path) -> None:
        package = target / "node_modules" / "typescript"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "typescript", "version": "7.1.0-beta"}))

    npm.before_exit = winner_finishes_first
    toolchain, ran = setup.install(_env(tmp_path), which=which, run=npm)
    assert ran and toolchain.available
    assert _entries(target) == [target.name]


def test_two_installs_at_once_run_npm_once(tmp_path: Path) -> None:
    npm = FakeNpm()
    npm.before_exit = lambda cwd: threading.Event().wait(0.3)
    start = threading.Barrier(2)
    results: list[tuple[ts_bridge.Toolchain, bool]] = []

    def install() -> None:
        start.wait()
        results.append(setup.install(_env(tmp_path), which=which, run=npm))

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(npm.npm_calls) == 1
    assert sorted(ran for _, ran in results) == [False, True]
    assert all(toolchain.available for toolchain, _ in results)
    assert _entries(ts_bridge.install_dir(_env(tmp_path))) == [ts_bridge.install_dir(_env(tmp_path)).name]


def test_an_interrupted_install_leaves_nothing_the_probe_accepts(tmp_path: Path) -> None:
    npm = FakeNpm()
    target = ts_bridge.install_dir(_env(tmp_path))

    def interrupted(cwd: Path) -> None:
        raise KeyboardInterrupt

    npm.before_exit = interrupted
    with pytest.raises(KeyboardInterrupt):
        setup.install(_env(tmp_path), which=which, run=npm)
    assert not target.exists() and _entries(target) == []
    assert not setup._toolchain(_env(tmp_path), which, FakeNpm()).available


def test_a_killed_install_s_staging_directory_is_never_accepted_and_is_cleaned_up(tmp_path: Path) -> None:
    target = ts_bridge.install_dir(_env(tmp_path))
    dead = target.parent / ".stage-dead"
    (dead / "node_modules" / "typescript").mkdir(parents=True)
    (dead / "node_modules" / "typescript" / "package.json").write_text(
        json.dumps({"name": "typescript", "version": "7.1.0-beta"})
    )
    assert not setup._toolchain(_env(tmp_path), which, FakeNpm()).available
    setup.install(_env(tmp_path), which=which, run=FakeNpm())
    assert _entries(target) == [target.name]


def test_an_npm_failure_summary_is_the_first_line_npm_printed(tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError) as failed:
        setup.install(_env(tmp_path), which=which, run=FakeNpm(npm_exit=1))
    assert failed.value.summary == "npm ci exited with code 1 (npm error 404)"
    assert setup.SetupError("one\ntwo").summary == "one"


def test_the_command_needs_no_repository_or_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    installed = SimpleNamespace(
        typescript_version="7.1.0-beta", typescript_dir="/cache/typescript", node_version="v24.21.0"
    )
    monkeypatch.setattr(setup, "install", lambda: (installed, True))
    assert cli.main(["setup"]) == 0
    assert capsys.readouterr().out == "ok: typescript 7.1.0-beta installed at /cache/typescript (node v24.21.0)\n"

    def fail():
        raise setup.SetupError("npm not found on PATH (it ships with Node.js)")

    monkeypatch.setattr(setup, "install", fail)
    assert cli.main(["setup"]) == 1
    assert capsys.readouterr().err == "code-intel setup: npm not found on PATH (it ships with Node.js)\n"
