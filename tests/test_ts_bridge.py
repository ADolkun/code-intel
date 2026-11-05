"""Tests for the TypeScript toolchain bridge: discovery, the helper protocol and the real helper."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_intel import ts_bridge


def _write(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── ts_bridge: discovery with fakes ──────────────────────────────────────


def _env(tmp_path: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """An environment whose cache is private to the test."""
    return {ts_bridge.CACHE_ENV: str(tmp_path / "cache"), **(extra or {})}


def _cached_typescript(tmp_path: Path) -> Path:
    """Where ``code-intel setup`` would have put the helper's TypeScript."""
    return ts_bridge.install_dir(_env(tmp_path)) / "node_modules" / "typescript"


def _fake_repo(tmp_path: Path, ts_version: str | None = "7.1.0-beta") -> Path:
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    if ts_version is not None:
        _write(
            _cached_typescript(tmp_path) / "package.json",
            json.dumps({"name": "typescript", "version": ts_version}),
        )
    return repo


def _fake_run(stdout: bytes = b"v24.21.0\n", returncode: int = 0, stderr: bytes = b"", raises=None):
    calls: list[dict] = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _which(path: str | None = "/usr/local/bin/node"):
    return lambda name: path if name == "node" else None


def test_find_toolchain_ok(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    run = _fake_run()
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=run)
    assert tc.available and tc.problem is None
    assert tc.node == "/usr/local/bin/node" and tc.node_version == "v24.21.0"
    assert tc.typescript_dir == _cached_typescript(tmp_path)
    assert (tc.typescript_version, tc.typescript_source, tc.lockfile_typescript_version) == (
        "7.1.0-beta",
        "helper",
        "7.1.0-beta",
    )
    assert tc.warnings == ()
    assert run.calls[0]["cmd"] == ["/usr/local/bin/node", "--version"]  # type: ignore[attr-defined]
    assert run.calls[0]["timeout"] == 5.0  # type: ignore[attr-defined]
    stamp = tc.stamp()
    assert stamp[:2] == ("v24.21.0", "/usr/local/bin/node")
    # No "./sync" export: the compiler is stamped by its package.json.
    assert tc.compiler_stamp is not None and tc.compiler_stamp
    assert stamp[3:6] == (str(tc.typescript_dir), "7.1.0-beta", tc.compiler_stamp) and len(stamp[6]) == 64


def test_a_node_replaced_in_place_changes_the_stamp(tmp_path: Path) -> None:
    """A reinstall or an upgrade behind the same path, even one that reports
    the same version, changes every cache keyed on the toolchain."""
    repo = _fake_repo(tmp_path)
    node = tmp_path / "bin" / "node"
    _write(node, "old\n")
    before = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(str(node)), run=_fake_run())
    node.unlink()
    _write(node, "new build\n")
    after = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(str(node)), run=_fake_run())
    assert before.node == after.node and before.node_version == after.node_version
    assert before.node_stamp is not None and before.stamp() != after.stamp()
    assert ts_bridge.node_identity(repo, env=_env(tmp_path), which=_which(str(node))) == (str(node), after.node_stamp)
    node.unlink()
    assert ts_bridge.node_identity(repo, env=_env(tmp_path), which=_which(str(node))) == (str(node), None)


def test_a_same_version_compiler_replacement_changes_the_stamp(tmp_path: Path) -> None:
    """A reinstall or a patch keeps package.json's version, but the compiler
    entry the helper loads changes, and so must every cache keyed on it."""
    repo = _fake_repo(tmp_path)
    ts_dir = _cached_typescript(tmp_path)
    _write(
        ts_dir / "package.json",
        json.dumps({"name": "typescript", "version": "7.1.0-beta", "exports": {"./sync": "./dist/api/sync/api.js"}}),
    )
    entry = ts_dir / "dist" / "api" / "sync" / "api.js"
    _write(entry, "export {};\n")
    before = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run())
    entry.unlink()
    _write(entry, "export const patched = true;\n")
    after = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run())
    assert ts_bridge.typescript_entry(ts_dir) == entry
    assert before.typescript_version == after.typescript_version == "7.1.0-beta"
    assert before.compiler_stamp is not None and before.stamp() != after.stamp()


def test_a_package_without_a_sync_export_is_stamped_by_its_package_json(tmp_path: Path) -> None:
    ts_dir = tmp_path / "typescript"
    _write(ts_dir / "package.json", json.dumps({"name": "typescript", "version": "7.0.2"}))
    assert ts_bridge.typescript_entry(ts_dir) == ts_dir / "package.json"
    assert ts_bridge.typescript_entry(tmp_path / "missing") == tmp_path / "missing" / "package.json"


@pytest.mark.parametrize("version", ["6.0.3", "7.0.2", "5.9.3"])
def test_a_typescript_without_the_stable_api_is_unsupported_and_says_how_to_fix_it(
    tmp_path: Path, version: str
) -> None:
    repo = _fake_repo(tmp_path, ts_version=version)
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run())
    shown = (_cached_typescript(tmp_path) / "package.json").as_posix()
    assert not tc.available and tc.typescript_dir is None
    assert tc.problem == ts_bridge.ToolchainProblem(
        "typescript_unsupported",
        f"{shown}: typescript {version} has no compiler API the helper can use; it needs 7.1 or newer",
        shown,
        ts_bridge.FRONTEND_TS_FIX,
    )
    other = tmp_path / "shared" / "typescript"
    _write(other / "package.json", json.dumps({"name": "typescript", "version": version}))
    via_env = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: str(other)}), which=_which(), run=_fake_run()
    )
    assert via_env.problem is not None and via_env.problem.code == "typescript_unsupported"
    assert via_env.problem.fix == f"set {ts_bridge.TS_DIR_ENV} to a directory holding TypeScript 7.1 or newer"


@pytest.mark.parametrize("version", ["7.1.0-beta", "7.1.0", "7.2.0-dev.20270101", "8.0.0"])
def test_typescript_7_1_and_newer_is_supported(tmp_path: Path, version: str) -> None:
    repo = _fake_repo(tmp_path, ts_version=version)
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run())
    assert tc.available and tc.typescript_version == version


def test_a_missing_typescript_install_says_to_run_setup(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, ts_version=None)
    problem = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run()).problem
    assert problem is not None and problem.fix.startswith('run "code-intel setup"')
    incomplete = ts_bridge.Toolchain("/usr/bin/node", "v24.0.0", None, None, None, None, None, ())
    assert 'run "code-intel setup"' in ts_bridge.incomplete_toolchain_problem(incomplete).fix


def test_the_frontend_root_problem_names_the_configured_directory(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    assert ts_bridge.frontend_not_found_problem(tmp_path, "web") is None
    problem = ts_bridge.frontend_not_found_problem(tmp_path, "client/app")
    assert problem is not None
    assert (problem.code, problem.message, problem.missing) == (
        "frontend_not_found",
        "client/app: frontend root not found",
        "client/app",
    )
    assert "frontend.root" in problem.fix


def test_find_toolchain_node_problems(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    missing = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(None), run=_fake_run())
    assert not missing.available
    assert missing.problem is not None and missing.problem.code == "node_not_found"
    assert missing.problem.missing == "node"

    old = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run(stdout=b"v16.20.0\n"))
    assert old.problem is not None and old.problem.code == "node_too_old" and "v16.20.0" in old.problem.message

    failed = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path), which=_which(), run=_fake_run(returncode=3, stderr=b"boom\n")
    )
    assert failed.problem is not None and failed.problem.code == "node_failed"
    assert "exited with code 3: boom" in failed.problem.message

    crashed = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path), which=_which(), run=_fake_run(raises=PermissionError("denied"))
    )
    assert crashed.problem is not None and crashed.problem.code == "node_failed"
    assert "could not run node --version (PermissionError: denied)" in crashed.problem.message

    garbage = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run(stdout=b"hello\n"))
    assert garbage.problem is not None and garbage.problem.code == "node_failed"


@pytest.mark.parametrize("printed", [b"v20.0.0-invalid\n", b"v24.21.0 extra\n", b"v24.21.0\nv18.0.0\n", b"v24.21\n"])
def test_find_toolchain_needs_the_whole_node_version_output_to_be_a_version(tmp_path: Path, printed: bytes) -> None:
    """A valid-looking prefix is not enough: the problem names the node binary,
    what it printed and the operation that failed."""
    repo = _fake_repo(tmp_path)
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run(stdout=printed))
    assert not tc.available
    assert tc.problem is not None and tc.problem.code == "node_failed"
    shown = printed.decode().strip()
    assert tc.problem.message == (
        f"/usr/local/bin/node: reading the Node version failed: node --version printed {shown!r}, "
        "expected exactly vMAJOR.MINOR.PATCH"
    )
    assert tc.problem.missing == "/usr/local/bin/node"


def test_find_toolchain_accepts_a_release_node_version_with_surrounding_whitespace(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run(stdout=b"  v22.11.0\r\n"))
    assert tc.available and tc.problem is None
    assert tc.node_version == "v22.11.0"


def test_find_toolchain_typescript_not_found_names_path_and_fix(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, ts_version=None)
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(), run=_fake_run())
    shown = (_cached_typescript(tmp_path) / "package.json").as_posix()
    assert not tc.available
    assert tc.problem == ts_bridge.ToolchainProblem(
        "typescript_not_found",
        f"{shown} not found",
        shown,
        ts_bridge.FRONTEND_TS_FIX,
    )
    assert tc.typescript_dir is None and tc.node_version == "v24.21.0"


def test_find_toolchain_env_override(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, ts_version=None)
    other = tmp_path / "shared" / "typescript"
    _write(other / "package.json", json.dumps({"name": "typescript", "version": "7.1.1"}))
    tc = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: str(other)}), which=_which(), run=_fake_run()
    )
    assert tc.available
    assert (tc.typescript_dir, tc.typescript_source, tc.typescript_version) == (other, "env", "7.1.1")
    assert len(tc.warnings) == 1 and "differs from the packaged lockfile (7.1.0-beta)" in tc.warnings[0]

    impostor = tmp_path / "impostor"
    _write(impostor / "package.json", json.dumps({"name": "not-typescript", "version": "1.0.0"}))
    bad = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: str(impostor)}), which=_which(), run=_fake_run()
    )
    assert bad.problem is not None and bad.problem.code == "typescript_unreadable"
    assert "package name is 'not-typescript', expected 'typescript'" in bad.problem.message

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    none = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: str(empty_dir)}), which=_which(), run=_fake_run()
    )
    # A missing package is typescript_not_found wherever the directory came from.
    assert none.problem is not None and none.problem.code == "typescript_not_found"
    assert none.problem.missing == (empty_dir / "package.json").as_posix()
    assert none.problem.message.startswith(f"{ts_bridge.TS_DIR_ENV}={empty_dir}: ")

    # A relative value is read from the repo root; the message quotes both.
    rel = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: "vendor/ts"}), which=_which(), run=_fake_run()
    )
    assert rel.problem is not None and rel.problem.code == "typescript_not_found"
    assert rel.problem.message == (
        f"{ts_bridge.TS_DIR_ENV}=vendor/ts ({repo / 'vendor' / 'ts'}): vendor/ts/package.json not found"
    )
    _write(repo / "vendor" / "ts" / "package.json", json.dumps({"name": "typescript", "version": "7.1.0-beta"}))
    found = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: "vendor/ts"}), which=_which(), run=_fake_run()
    )
    assert found.available and found.typescript_dir == repo / "vendor" / "ts"

    # An empty value counts as unset.
    unset = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: ""}), which=_which(), run=_fake_run()
    )
    assert unset.problem is not None and unset.problem.code == "typescript_not_found"


def test_find_toolchain_node_override(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    node = _write(tmp_path / "bin" / "node", "#!/bin/sh\n")
    node.chmod(0o755)
    run = _fake_run()
    never = lambda name: None  # noqa: E731
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: str(node)}), which=never, run=run)
    assert tc.available and (tc.node, tc.node_source) == (str(node), "env")
    assert run.calls[0]["cmd"] == [str(node), "--version"]  # type: ignore[attr-defined]

    # A relative path is taken relative to the repo root, not the working
    # directory, and comes back absolute because the helper runs from the repo.
    local = _write(repo / "bin" / "node", "#!/bin/sh\n")
    local.chmod(0o755)
    relative = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: "bin/node"}), which=never, run=_fake_run()
    )
    assert relative.available and relative.node == str(local.resolve())
    missing_rel = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: "bin/nope"}), which=never, run=_fake_run()
    )
    assert missing_rel.problem is not None
    assert (
        missing_rel.problem.message
        == f"{ts_bridge.NODE_ENV}=bin/nope ({repo.resolve() / 'bin' / 'nope'}): no such file"
    )

    # A bare command name is looked up on PATH.
    named = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: "node24"}), which=_which_named(), run=_fake_run()
    )
    assert (named.node, named.node_source) == ("/opt/node24/bin/node24", "env")

    gone = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: str(tmp_path / "nope")}), which=_which(), run=run
    )
    assert gone.problem is not None and gone.problem.code == "node_not_found"
    assert gone.problem.message == f"{ts_bridge.NODE_ENV}={tmp_path / 'nope'}: no such file"
    assert gone.problem.fix == ts_bridge.NODE_ENV_FIX and gone.node is None

    plain = _write(tmp_path / "bin" / "plain", "x")
    plain.chmod(0o644)
    blocked = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: str(plain)}), which=_which(), run=run
    )
    assert blocked.problem is not None and blocked.problem.message.endswith(": file is not executable")

    missing_cmd = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: "nodez"}), which=never, run=run
    )
    assert missing_cmd.problem is not None
    assert missing_cmd.problem.message == f"{ts_bridge.NODE_ENV}=nodez: command not found on PATH"

    old = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: str(node)}), which=never, run=_fake_run(stdout=b"v16.20.0\n")
    )
    assert old.problem is not None and old.problem.code == "node_too_old"
    assert old.problem.message.startswith(f"{ts_bridge.NODE_ENV}={node}: ")
    old_rel = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.NODE_ENV: "bin/node"}), which=never, run=_fake_run(stdout=b"v16.20.0\n")
    )
    assert old_rel.problem is not None
    assert old_rel.problem.message.startswith(f"{ts_bridge.NODE_ENV}=bin/node ({local.resolve()}): ")
    assert old.problem.fix == ts_bridge.NODE_ENV_FIX


def _which_named():
    return lambda name: "/opt/node24/bin/node24" if name == "node24" else None


def test_an_incomplete_toolchain_names_the_missing_piece() -> None:
    tc = ts_bridge.Toolchain("/usr/bin/node", "v24.0.0", None, None, None, None, None, ())
    problem = ts_bridge.incomplete_toolchain_problem(tc)
    assert problem.code == "toolchain_incomplete"
    assert problem.message.startswith("toolchain incomplete: typescript directory not resolved")
    assert problem.fix == ts_bridge.FRONTEND_TS_FIX
    with pytest.raises(ts_bridge.ToolchainUnavailable, match="typescript directory not resolved"):
        ts_bridge.run_helper(tc, "facts", {"files": []}, timeout_s=1)


def test_a_helper_failure_becomes_a_named_problem() -> None:
    exc = ts_bridge.HelperError("routes", "exit", "node exited with code 1: boom")
    problem = exc.problem()
    assert problem.code == "helper_failed" and problem.missing is None and problem.fix == ts_bridge.HELPER_FIX
    assert problem.message == (
        f"{ts_bridge.HELPER.as_posix()}: typescript helper (routes mode) failed at step 'exit': "
        "node exited with code 1: boom"
    )


def test_find_toolchain_reports_node_before_typescript(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path, ts_version=None)
    tc = ts_bridge.find_toolchain(repo, env=_env(tmp_path), which=_which(None), run=_fake_run())
    assert tc.problem is not None and tc.problem.code == "node_not_found"


def test_helper_files_and_digest() -> None:
    names = [p.name for p in ts_bridge.helper_files()]
    for required in ("common.cjs", "decl_facts.cjs", "facts.cjs", "jsx_facts.cjs", "helper.cjs"):
        assert required in names
    assert names == sorted(names)
    assert ts_bridge.helper_digest() == ts_bridge.helper_digest()


# ── ts_bridge: run_helper failure steps ──────────────────────────────────


def _available_toolchain(tmp_path: Path) -> ts_bridge.Toolchain:
    return ts_bridge.Toolchain(
        node="/usr/local/bin/node",
        node_version="v24.21.0",
        typescript_dir=tmp_path / "typescript",
        typescript_version="7.1.0-beta",
        typescript_source="helper",
        lockfile_typescript_version="7.1.0-beta",
        problem=None,
        warnings=(),
        repo_root=tmp_path,
    )


def _ok_response(mode: str = "facts") -> bytes:
    return json.dumps({"protocol": 1, "mode": mode, "files": []}).encode()


def test_run_helper_sends_request_over_stdin(tmp_path: Path) -> None:
    run = _fake_run(stdout=_ok_response())
    tc = _available_toolchain(tmp_path)
    out = ts_bridge.run_helper(tc, "facts", {"files": ["/x.tsx"], "protocol": 99}, timeout_s=7, run=run)
    assert out == {"protocol": 1, "mode": "facts", "files": []}
    [call] = run.calls  # type: ignore[attr-defined]
    assert call["cmd"] == ["/usr/local/bin/node", str(ts_bridge.HELPER)]
    assert call["timeout"] == 7 and call["cwd"] == str(tmp_path) and call["capture_output"]
    sent = json.loads(call["input"])
    assert sent == {"files": ["/x.tsx"], "protocol": 1, "mode": "facts", "typescript_dir": str(tmp_path / "typescript")}
    assert call["creationflags"] == 0


def test_run_helper_uses_create_no_window_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts_bridge, "_IS_WINDOWS", True)
    run = _fake_run(stdout=_ok_response())
    ts_bridge.run_helper(_available_toolchain(tmp_path), "facts", {}, timeout_s=1, run=run)
    assert run.calls[0]["creationflags"] == 0x08000000  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "run,step,detail",
    [
        (_fake_run(raises=FileNotFoundError(2, "No such file")), "start", "FileNotFoundError"),
        (_fake_run(raises=subprocess.TimeoutExpired(["node"], 3)), "timeout", "no response within 3 s"),
        (_fake_run(returncode=1, stderr=b"\nTypeError: boom\n  at x\n"), "exit", "code 1: TypeError: boom"),
        (_fake_run(returncode=2, stdout=b'{"protocol_error": "unsupported protocol 2"}\n'), "protocol", "unsupported"),
        (_fake_run(stdout=b"\xff\xfe"), "decode output", "UnicodeDecodeError"),
        (_fake_run(stdout=b"not json"), "decode output", "JSONDecodeError"),
        (_fake_run(stdout=b"[1]"), "decode output", "expected a JSON object"),
        (_fake_run(stdout=_ok_response("routes")), "protocol", "mode 'routes'"),
    ],
)
def test_run_helper_failures_name_the_step(tmp_path: Path, run, step: str, detail: str) -> None:
    with pytest.raises(ts_bridge.HelperError) as info:
        ts_bridge.run_helper(_available_toolchain(tmp_path), "facts", {}, timeout_s=3, run=run)
    assert info.value.step == step
    assert info.value.mode == "facts"
    assert detail in info.value.detail
    assert str(info.value).startswith(f"typescript helper (facts mode) failed at step '{step}': ")


def test_run_helper_unencodable_request_is_a_write_request_error(tmp_path: Path) -> None:
    with pytest.raises(ts_bridge.HelperError) as info:
        ts_bridge.run_helper(_available_toolchain(tmp_path), "facts", {"files": {1, 2}}, timeout_s=1, run=_fake_run())
    assert info.value.step == "write request"


def test_run_helper_refuses_an_unavailable_toolchain(tmp_path: Path) -> None:
    problem = ts_bridge.ToolchainProblem("node_not_found", "node not found on PATH", "node", ts_bridge.NODE_FIX)
    tc = ts_bridge.Toolchain(None, None, None, None, None, None, problem, ())
    with pytest.raises(ts_bridge.ToolchainUnavailable) as info:
        ts_bridge.run_helper(tc, "facts", {}, timeout_s=1, run=_fake_run())
    assert info.value.problem is problem
    assert str(info.value) == f"node not found on PATH. Fix: {ts_bridge.NODE_FIX}"


# ── The real helper ──────────────────────────────────────────────────────


def _node_raw(toolchain: ts_bridge.Toolchain, body: bytes) -> subprocess.CompletedProcess:
    assert toolchain.node is not None
    return subprocess.run([toolchain.node, str(ts_bridge.HELPER)], input=body, capture_output=True, timeout=60)


def test_helper_answers_a_facts_request(facts_helper, typescript_toolchain) -> None:
    run = facts_helper(
        {
            "components/Table.tsx": "export function Table() {\r\n  return <div />;\r\n}\r\n",
            "lib/broken.ts": "const x = ;\nexport {};\n",
            "lib/plain.mjs": "export const a = 1;\n",
            "lib/bom.ts": "\ufeffexport const b = 2;\n",
        }
    )
    response = run.response
    assert (response["protocol"], response["mode"], response["facts_schema"]) == (1, "facts", 1)
    assert response["typescript_version"] == typescript_toolchain.typescript_version
    table = run.by_rel["components/Table.tsx"]
    assert table == {
        "path": str(run.root / "components" / "Table.tsx"),
        "status": "ok",
        "error": None,
        "syntax_errors": 0,
        "first_syntax_error": None,
        "elements": [
            {
                "tag": "div",
                "line": 2,
                "column": 10,
                "kind": "self_close",
                "parent": "Table",
                "snippet": "return <div />;",
            }
        ],
        "decls": [
            {
                "name": "Table",
                "kind": "component",
                "line": 1,
                "end_line": 3,
                "column": 17,
                "scope": "module",
                "container": None,
                "exported": True,
                "signature": "export function Table()",
            }
        ],
    }
    broken = run.by_rel["lib/broken.ts"]
    assert broken["status"] == "ok" and broken["syntax_errors"] >= 1
    assert broken["first_syntax_error"]["line"] == 1
    assert broken["first_syntax_error"]["column"] == 11
    assert broken["first_syntax_error"]["message"]
    assert run.by_rel["lib/plain.mjs"]["status"] == "ok"
    assert run.by_rel["lib/bom.ts"]["syntax_errors"] == 0


def test_helper_reports_unreadable_files_per_file(typescript_toolchain, tmp_path: Path) -> None:
    ok = _write(tmp_path / "ok.tsx", "export const A = () => <b />;\n")
    missing = tmp_path / "missing.tsx"
    directory = tmp_path / "dir.ts"
    directory.mkdir()
    response = ts_bridge.run_helper(
        typescript_toolchain,
        "facts",
        {"files": [str(ok), str(missing), str(directory)], "snippet_max": 500, "signature_max": 200},
        timeout_s=ts_bridge.FACTS_TIMEOUT_S,
    )
    statuses = [(f["status"], (f["error"] or "")[:6]) for f in response["files"]]
    assert statuses == [("ok", ""), ("read_failed", "ENOENT"), ("read_failed", "EISDIR")]


@pytest.mark.parametrize(
    "body,reason",
    [
        (b"{not json", "request is not valid JSON"),
        (b"[]", "request must be a JSON object"),
        (b'{"protocol": 2, "mode": "facts"}', "unsupported protocol 2, expected 1"),
        (b'{"protocol": 1, "mode": "lint"}', 'unknown mode "lint"'),
        (b'{"protocol": 1, "mode": "facts", "typescript_dir": ""}', "typescript_dir must be a non-empty string"),
    ],
)
def test_helper_protocol_errors_exit_2_with_one_json_line(typescript_toolchain, body: bytes, reason: str) -> None:
    proc = _node_raw(typescript_toolchain, body)
    assert proc.returncode == 2
    lines = proc.stdout.decode().splitlines()
    assert len(lines) == 1
    assert reason in json.loads(lines[0])["protocol_error"]


def test_helper_validates_typescript_dir_before_require(typescript_toolchain, tmp_path: Path) -> None:
    fake = tmp_path / "fake-typescript"
    _write(fake / "package.json", json.dumps({"name": "left-pad", "main": "index.js"}))
    _write(fake / "index.js", "process.stdout.write('LOADED'); process.exit(0);\n")
    body = json.dumps({"protocol": 1, "mode": "facts", "typescript_dir": str(fake), "files": []}).encode()
    proc = _node_raw(typescript_toolchain, body)
    assert proc.returncode == 2
    assert b"LOADED" not in proc.stdout
    assert 'package name is "left-pad", expected "typescript"' in json.loads(proc.stdout)["protocol_error"]


def test_a_typescript_package_that_will_not_load_is_named_in_one_line(typescript_toolchain, tmp_path: Path) -> None:
    broken = tmp_path / "broken-typescript"
    _write(broken / "package.json", json.dumps({"name": "typescript", "version": "7.1.0"}))
    body = json.dumps({"protocol": 1, "mode": "facts", "typescript_dir": str(broken), "files": []}).encode()
    proc = _node_raw(typescript_toolchain, body)
    reason = json.loads(proc.stdout)["protocol_error"]
    assert proc.returncode == 2 and "does not export typescript/sync" in reason and "7.1 or newer" in reason
    assert "\n" not in reason and "Require stack" not in reason
    _write(broken / "package.json", json.dumps({"name": "typescript", "exports": {"./sync": "./missing.js"}}))
    proc = _node_raw(typescript_toolchain, body)
    reason = json.loads(proc.stdout)["protocol_error"]
    assert proc.returncode == 2 and "has no compiler API the helper can use" in reason and "\n" not in reason


def test_the_helper_names_a_typescript_older_than_7_1_in_one_line(typescript_toolchain, tmp_path: Path) -> None:
    old = tmp_path / "typescript-6"
    _write(old / "package.json", json.dumps({"name": "typescript", "version": "6.0.3", "main": "lib/typescript.js"}))
    body = json.dumps({"protocol": 1, "mode": "facts", "typescript_dir": str(old), "files": []}).encode()
    proc = _node_raw(typescript_toolchain, body)
    reason = json.loads(proc.stdout)["protocol_error"]
    assert proc.returncode == 2 and "typescript 6.0.3 has no compiler API the helper can use" in reason
    assert "it needs typescript 7.1 or newer" in reason and "\n" not in reason


def test_a_multi_line_protocol_error_keeps_its_first_line(tmp_path: Path) -> None:
    stdout = json.dumps({"protocol_error": "bad request\nRequire stack:\n- /abs/x.cjs"}).encode()
    for run in (_fake_run(returncode=2, stdout=stdout), _fake_run(stdout=stdout)):
        with pytest.raises(ts_bridge.HelperError) as info:
            ts_bridge.run_helper(_available_toolchain(tmp_path), "facts", {}, timeout_s=3, run=run)
        assert (info.value.step, info.value.detail) == ("protocol", "bad request")


def test_helper_rejects_bad_facts_fields(typescript_toolchain) -> None:
    assert typescript_toolchain.typescript_dir is not None
    base = {"protocol": 1, "mode": "facts", "typescript_dir": str(typescript_toolchain.typescript_dir)}
    for extra, reason in (
        ({"files": "a.ts"}, "files must be an array"),
        ({"files": [], "snippet_max": 0}, "snippet_max must be a positive integer"),
    ):
        proc = _node_raw(typescript_toolchain, json.dumps({**base, **extra}).encode())
        assert proc.returncode == 2
        assert reason in json.loads(proc.stdout)["protocol_error"]


_COMMON_PROBE = r"""
const path = require("path");
const c = require(process.argv[3]);
const { parseStandalone } = require(path.join(path.dirname(process.argv[3]), "tsapi.cjs"));
(async () => {
const ts = await c.loadTypeScript(process.argv[2]);
const api = new ts.API({ cwd: process.cwd() });
const parse = (file, text, kind) => parseStandalone(ts, api, file, text, kind).sf;
const src = "const rows = items.map((r) => <Row/>);\nconst X = function Y() { return <a/>; };\n" +
  "class K { m() { return <b/>; } }\nconst o = { p: () => <i/> };\nlet q; q = () => <u/>;\n" +
  "export default () => <s/>;\nconst s = '\u{1F600}' + `x`; const z = <Z/>;\n";
const sf = parse("/x.tsx", src, c.scriptKindFor(ts, "/x.tsx"));
const fns = [];
const visit = (n) => { if (ts.isFunctionLike(n) && n.body) fns.push([c.readmeFunctionName(ts, n) ?? null, c.callerName(ts, n) ?? null]); ts.forEachChild(n, visit); };
visit(sf);
let zPos = -1;
const findZ = (n) => { if (ts.isJsxSelfClosingElement(n) && n.tagName.getText(sf) === "Z") zPos = n.getStart(sf); ts.forEachChild(n, findZ); };
findZ(sf);
const kinds = [".tsx", ".ts", ".mts", ".cts", ".jsx", ".js", ".mjs", ".cjs"].map((e) => ts.ScriptKind[c.scriptKindFor(ts, "/a" + e)]);
const crlf = parse("/y.ts", "a\r\nb\u2028c\rd", ts.ScriptKind.TS);
const fnBody = parse("/w.tsx", "function A() { const f = () => <x/>; return 1; }\nfunction B() { return <y/>; }\nconst C = () => () => <z/>;", ts.ScriptKind.TSX);
const fnNodes = [fnBody.statements[0], fnBody.statements[1], fnBody.statements[2].declarationList.declarations[0].initializer];
const bodies = fnNodes.map((s) => c.containsJsxOutsideNestedFunctions(ts, s.body));
const expr = parse("/e.ts", "((a as any)!) satisfies T;", ts.ScriptKind.TS).statements[0].expression;
process.stdout.write(JSON.stringify({
  fns,
  z: c.lineColumn(sf, zPos),
  kinds,
  lines: [1, 2, 3, 4, 5].map((l) => c.lineText(crlf, l)),
  bodies,
  unwrapped: ts.SyntaxKind[c.unwrap(ts, expr).kind],
  components: ["Table", "TABLE", "table", "X1a"].map(c.isComponentName),
  trunc: [c.truncate("abc", 2), c.truncate("\u{1F600}\u{1F600}x", 2), c.truncate("ab", 5)],
  wf: c.wellFormed("a\uD800b"),
}));
api.close();
})();
"""


def test_common_helpers(typescript_toolchain, tmp_path: Path) -> None:
    assert typescript_toolchain.node is not None
    probe = _write(tmp_path / "probe.cjs", _COMMON_PROBE)
    proc = subprocess.run(
        [
            typescript_toolchain.node,
            str(probe),
            str(typescript_toolchain.typescript_dir),
            str(ts_bridge.HELPER_DIR / "common.cjs"),
        ],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    out = json.loads(proc.stdout)
    assert out["fns"] == [
        ["rows", "rows"],
        ["Y", "Y"],
        [None, "m"],
        [None, "p"],
        [None, "q"],
        [None, "default"],
    ]
    # One code point for the emoji: UTF-16 offsets would put <Z/> at column 33.
    assert out["z"] == [7, 32]
    assert out["kinds"] == ["TSX", "TS", "TS", "TS", "JSX", "JS", "JS", "JS"]
    # Lines break at \r\n, \r and \n only; U+2028 stays inside its line, as
    # editors, rg and Python's line counting see it.
    assert out["lines"] == ["a", "b\u2028c", "d", "", ""]
    # A curried arrow's JSX belongs to the inner function, not the outer one.
    assert out["bodies"] == [False, True, False]
    assert out["unwrapped"] == "Identifier"
    assert out["components"] == [True, False, False, True]
    assert out["trunc"] == ["ab", "\U0001f600\U0001f600", "ab"]
    assert out["wf"] == "a\ufffdb"


def test_typescript_dir_env_override_runs_the_helper(typescript_toolchain, tmp_path: Path) -> None:
    """CODE_INTEL_TYPESCRIPT_DIR pointing at a TypeScript package is enough to run the helper."""
    assert typescript_toolchain.typescript_dir is not None
    repo = _fake_repo(tmp_path, ts_version=None)
    tc = ts_bridge.find_toolchain(
        repo, env=_env(tmp_path, {ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)})
    )
    assert tc.available and tc.typescript_source == "env"
    source = _write(repo / "frontend" / "x.tsx", "export const A = () => <b />;\n")
    response = ts_bridge.run_helper(tc, "facts", {"files": [str(source)]}, timeout_s=ts_bridge.FACTS_TIMEOUT_S)
    assert [f["status"] for f in response["files"]] == ["ok"]
