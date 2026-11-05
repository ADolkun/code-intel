"""``code-intel setup``: the shared TypeScript install, with a fake npm and a private cache."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_intel import cli, setup, ts_bridge

NODE = "/usr/local/bin/node"
NPM = "/usr/local/bin/npm"


def _env(tmp_path: Path) -> dict[str, str]:
    return {ts_bridge.CACHE_ENV: str(tmp_path / "cache")}


def _which(name: str) -> str | None:
    return {"node": NODE, "npm": NPM}.get(name)


class FakeNpm:
    """Answers ``node --version`` and ``npm ci``; ``npm ci`` writes what the real one would."""

    def __init__(self, *, npm_exit: int = 0, version: str = "7.1.0-beta", node_out: bytes = b"v24.21.0\n") -> None:
        self.npm_calls: list[dict] = []
        self.npm_exit = npm_exit
        self.version = version
        self.node_out = node_out
        self.before_exit = lambda cwd: None

    def __call__(self, cmd, **kwargs):
        if cmd[0] == NODE:
            return SimpleNamespace(returncode=0, stdout=self.node_out, stderr=b"")
        cwd = Path(kwargs["cwd"])
        self.npm_calls.append({"cmd": cmd, "cwd": cwd, "files": sorted(p.name for p in cwd.iterdir())})
        self.before_exit(cwd)
        if self.npm_exit:
            return SimpleNamespace(returncode=self.npm_exit, stdout=b"", stderr=b"npm error 404\nnpm error not found\n")
        package = cwd / "node_modules" / "typescript"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "typescript", "version": self.version}))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def test_install_runs_npm_ci_once_in_the_shared_cache(tmp_path: Path) -> None:
    npm = FakeNpm()
    toolchain, ran = setup.install(_env(tmp_path), which=_which, run=npm)
    target = ts_bridge.install_dir(_env(tmp_path))
    assert ran and toolchain.available
    assert toolchain.typescript_dir == target / "node_modules" / "typescript"
    [call] = npm.npm_calls
    assert call["cmd"] == [NPM, "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    assert call["files"] == ["package-lock.json", "package.json"]
    assert (target / "package-lock.json").read_bytes() == (ts_bridge.HELPER_DIR / "package-lock.json").read_bytes()
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_a_second_run_does_not_call_npm(tmp_path: Path) -> None:
    npm = FakeNpm()
    setup.install(_env(tmp_path), which=_which, run=npm)
    toolchain, ran = setup.install(_env(tmp_path), which=_which, run=npm)
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
    toolchain, ran = setup.install(env, which=_which, run=FakeNpm())
    assert ran and toolchain.typescript_dir == ts_bridge.install_dir(env) / "node_modules" / "typescript"


def test_a_failing_npm_leaves_nothing_behind(tmp_path: Path) -> None:
    npm = FakeNpm(npm_exit=1)
    with pytest.raises(setup.SetupError, match="npm ci exited with code 1:\nnpm error 404\nnpm error not found"):
        setup.install(_env(tmp_path), which=_which, run=npm)
    assert list((tmp_path / "cache" / "ts").iterdir()) == []


def test_an_unrunnable_npm_is_named(tmp_path: Path) -> None:
    def run(cmd, **kwargs):
        if cmd[0] == NODE:
            return SimpleNamespace(returncode=0, stdout=b"v24.21.0\n", stderr=b"")
        raise subprocess.TimeoutExpired(cmd, 600)

    with pytest.raises(setup.SetupError, match="could not run npm \\(TimeoutExpired"):
        setup.install(_env(tmp_path), which=_which, run=run)
    assert list((tmp_path / "cache" / "ts").iterdir()) == []


def test_missing_node_or_npm_stops_before_installing(tmp_path: Path) -> None:
    npm = FakeNpm()
    with pytest.raises(setup.SetupError, match="node not found on PATH. Fix: install Node.js 18"):
        setup.install(_env(tmp_path), which=lambda name: None, run=npm)
    with pytest.raises(setup.SetupError, match="node v16.20.0 is older"):
        setup.install(_env(tmp_path), which=_which, run=FakeNpm(node_out=b"v16.20.0\n"))
    with pytest.raises(setup.SetupError, match="npm not found on PATH"):
        setup.install(_env(tmp_path), which=lambda name: NODE if name == "node" else None, run=npm)
    assert npm.npm_calls == []


def test_an_install_that_is_not_usable_is_reported(tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="not usable: .*no compiler API"):
        setup.install(_env(tmp_path), which=_which, run=FakeNpm(version="7.0.2"))


def test_a_damaged_install_is_replaced(tmp_path: Path) -> None:
    target = ts_bridge.install_dir(_env(tmp_path))
    (target / "node_modules").mkdir(parents=True)
    (target / "leftover").write_text("x")
    npm = FakeNpm()
    toolchain, ran = setup.install(_env(tmp_path), which=_which, run=npm)
    assert ran and toolchain.available and not (target / "leftover").exists()


def test_a_run_that_loses_the_race_keeps_the_winners_install(tmp_path: Path) -> None:
    npm = FakeNpm()
    target = ts_bridge.install_dir(_env(tmp_path))

    def winner_finishes_first(cwd: Path) -> None:
        package = target / "node_modules" / "typescript"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "typescript", "version": "7.1.0-beta"}))

    npm.before_exit = winner_finishes_first
    toolchain, ran = setup.install(_env(tmp_path), which=_which, run=npm)
    assert ran and toolchain.available
    assert [p.name for p in target.parent.iterdir()] == [target.name]


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
