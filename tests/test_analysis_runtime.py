"""Unit tests for `analysis_runtime`: caches, fingerprints, steps, toolchain and
status, with the analyzers injected as fakes (`tool_fixtures.FakeAnalyzers`).

The last tests drive the real analyzer modules end to end.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest
from fixture_repo import git_init
from mcp_fixtures import (
    E2E_LAYOUT,
    LATE_NS,
    TS_DIR,
    Clock,
    FakeAnalyzers,
    FakeSymbolResult,
    FakeUsage,
    config_toml,
    di_result,
    di_route,
    limitation,
    make_repo,
    make_runtime,
    read_failed,
    repo_files_on_disk,
    routes_result,
    unresolved_app,
    write,
    write_config,
    write_doc_config,
)

from code_intel import analysis_runtime, problems, ts_bridge
from code_intel.analysis_runtime import AnalysisFailed, InvalidRoot
from code_intel.config import BackendConfig, ConfigFile, load_config
from code_intel.result_types import LimitationDict

# The runtime's timeline is fixture mtimes and an injected Clock; the kernel's
# ctime would put every file in the clock's future.
pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def fakes() -> FakeAnalyzers:
    return FakeAnalyzers()


# ── Isolation ────────────────────────────────────────────────────────────


def test_runtimes_do_not_share_cache_state(tmp_path: Path) -> None:
    first_fakes, second_fakes = FakeAnalyzers(), FakeAnalyzers()
    first = make_runtime(make_repo(tmp_path / "a"), first_fakes)
    second = make_runtime(make_repo(tmp_path / "b"), second_fakes)
    first.get_di()
    assert first.last_state == {"di_map": "complete"}
    assert second.last_state == {}
    assert second_fakes.calls == {}


# ── Backend model and DI caching ─────────────────────────────────────────


def test_get_di_reuses_the_model_until_a_backend_file_changes(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    assert rt.get_di() is rt.get_di()
    assert fakes.calls == {"list_repo_files": 2, "build_backend_model": 1, "build_di": 1}

    write(repo / "backend" / "service_backend" / "new.py", "y = 2\n", 2000)
    rt.get_di()
    assert fakes.calls == {"list_repo_files": 3, "build_backend_model": 2, "build_di": 2}
    assert fakes.previous_sources == [None, "sources-1"], "the rebuild gets the previous per-module parse cache"


@pytest.mark.real_ctime
def test_get_di_sees_a_same_size_rewrite_with_its_mtime_restored_after_the_window(
    repo: Path, fakes: FakeAnalyzers, rewrite_keeping_mtime
) -> None:
    """A DI cache built once its sources are past the racy window is reused by
    stamp alone; the kernel's ctime in the stamp still shows a router module
    rewritten in place with its old mtime put back."""
    clock = Clock(time.time_ns() + 3 * analysis_runtime.CodeWatch.RACY_WINDOW_NS)
    rt = make_runtime(repo, fakes, clock=clock)
    assert rt.get_di() is rt.get_di()
    assert (fakes.calls["build_backend_model"], fakes.calls["build_di"]) == (1, 1)

    rewrite_keeping_mtime(repo / "backend" / "service_backend" / "main.py", "app = 1234\n")
    clock.now = time.time_ns() + 3 * analysis_runtime.CodeWatch.RACY_WINDOW_NS
    rt.get_di()
    assert (fakes.calls["build_backend_model"], fakes.calls["build_di"]) == (2, 2)


@pytest.mark.parametrize("rel", [".python-version", "backend/pyproject.toml", "backend/uv.lock"])
def test_interpreter_and_project_files_are_part_of_the_backend_fingerprint(
    repo: Path, fakes: FakeAnalyzers, rel: str
) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    write(repo / rel, "changed = true\n", 3000)
    rt.get_di()
    assert fakes.calls["build_backend_model"] == 2


def test_the_library_capture_file_is_part_of_the_backend_fingerprint_only_when_configured(
    tmp_path: Path, fakes: FakeAnalyzers
) -> None:
    configured = make_repo(tmp_path / "configured")
    write_config(
        configured, config_toml().replace("\n[frontend]", '\n[backend.capture]\npython = "bin/python"\n\n[frontend]', 1)
    )
    rt = make_runtime(configured, fakes)
    rt.get_di()
    write(configured / ".code-intel" / "library-capture.json", "{}\n", 3000)
    rt.get_di()
    assert fakes.calls["build_backend_model"] == 2

    fakes.calls.clear()
    plain = make_repo(tmp_path / "plain")
    other = make_runtime(plain, fakes)
    other.get_di()
    write(plain / ".code-intel" / "library-capture.json", "{}\n", 3000)
    other.get_di()
    assert fakes.calls["build_backend_model"] == 1


def test_frontend_edits_do_not_rebuild_the_backend_model(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    write(repo / "frontend" / "lib" / "hooks.ts", "export {};\n", 3000)
    rt.get_di()
    assert (fakes.calls["build_backend_model"], fakes.calls["build_di"]) == (1, 1)


def test_the_routes_walk_skips_build_output_only_at_the_frontend_root(repo: Path, fakes: FakeAnalyzers) -> None:
    """The runtime hands the routes scan its own walk, so it must skip the same
    generated directories the analyzers skip."""
    write(repo / "frontend" / "coverage" / "lcov.ts", "export {};\n", 3000)
    write(repo / "frontend" / "app" / "build" / "page.ts", "export {};\n", 3000)
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    assert "frontend/app/build/page.ts" in fakes.scan_files
    assert not [rel for rel in fakes.scan_files if rel.startswith("frontend/coverage/")]
    write(repo / "frontend" / "coverage" / "lcov.ts", "export const x = 1;\n", 4000)
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 1, "an edit under build output does not invalidate routes"


def test_deleting_the_newest_source_invalidates_the_model(repo: Path, fakes: FakeAnalyzers) -> None:
    newest = write(repo / "backend" / "service_backend" / "newest.py", "z = 3\n", 5000)
    rt = make_runtime(repo, fakes)
    rt.get_di()
    newest.unlink()  # the max mtime drops; only a per-file fingerprint sees this
    rt.get_di()
    assert fakes.calls["build_backend_model"] == 2


def test_a_racy_fingerprint_is_not_reused(repo: Path, fakes: FakeAnalyzers) -> None:
    """A file written within 2 s of the check may change again without changing
    its stamp, so the model is rebuilt until the window has passed; after that,
    unchanged content confirms the cache (git's racy-clean check)."""
    write(repo / "backend" / "service_backend" / "fresh.py", "x = 1\n", 5000)
    clock = Clock(5000 * 1_000_000_000 + 500_000_000)
    rt = make_runtime(repo, fakes, clock=clock)
    rt.get_di()
    rt.get_di()
    assert fakes.calls["build_backend_model"] == 2
    clock.now += 5_000_000_000
    rt.get_di()
    rt.get_di()
    assert fakes.calls["build_backend_model"] == 2, "confirmed by content, not rebuilt"


def test_a_racy_file_changed_within_its_stamp_is_rebuilt(repo: Path, fakes: FakeAnalyzers) -> None:
    """Same size, same mtime, same inode, new bytes: only the content check sees it."""
    fresh = write(repo / "backend" / "service_backend" / "fresh.py", "x = 1\n", 5000)
    clock = Clock(5000 * 1_000_000_000 + 500_000_000)
    rt = make_runtime(repo, fakes, clock=clock)
    rt.get_di()
    write(fresh, "x = 2\n", 5000)
    clock.now += 5_000_000_000
    assert rt.status().cache["di"] == {"loaded": False, "fresh": False}, "status drops it once the window has passed"
    rt.get_di()
    assert fakes.calls["build_backend_model"] == 2


def test_concurrent_calls_build_once(repo: Path, fakes: FakeAnalyzers) -> None:
    """The runtime lock keeps two concurrent callers from building twice."""
    rt = make_runtime(repo, fakes)
    original = fakes.build_backend_model

    def slow(repo_root, backend, sources, visible):
        time.sleep(0.05)
        return original(repo_root, backend, sources, visible)

    rt.analyzers = dataclasses.replace(rt.analyzers, build_backend_model=slow)
    threads = [threading.Thread(target=rt.get_di) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fakes.calls["build_backend_model"] == 1


# ── Routes ───────────────────────────────────────────────────────────────


def test_di_and_routes_share_one_backend_model(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    rt.get_routes()
    assert fakes.calls["build_backend_model"] == 1
    assert rt.get_routes() is rt.get_routes()
    assert fakes.calls["scan_frontend"] == 1


def test_routes_scan_every_ts_and_js_suffix(repo: Path, fakes: FakeAnalyzers) -> None:
    for name in ("a.jsx", "b.mjs", "c.cjs", "d.mts", "e.cts", "f.js", "notes.md"):
        write(repo / "frontend" / "x" / name, "export {};\n")
    make_runtime(repo, fakes).get_routes()
    scanned = {Path(rel).name for rel in fakes.scan_files}
    assert {"a.jsx", "b.mjs", "c.cjs", "d.mts", "e.cts", "f.js", "api.ts", "Page.tsx"} <= scanned
    assert "notes.md" not in scanned


@pytest.mark.parametrize(
    "rel",
    [
        "frontend/components/Widget.jsx",
        "frontend/tsconfig.json",
        "frontend/package-lock.json",
        "backend/service_backend/x.py",
    ],
)
def test_routes_cache_invalidates_on_any_source_it_reads(repo: Path, fakes: FakeAnalyzers, rel: str) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    write(repo / rel, "// edit\n", 4000)
    rt.get_routes()
    assert fakes.calls["build_routes"] == 2


def test_routes_cache_ignores_unrelated_files(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    write(repo / "frontend" / "README.md", "docs\n", 4000)
    write(repo / "frontend" / "node_modules" / "x" / "index.js", "x\n", 4000)
    rt.get_routes()
    assert fakes.calls["build_routes"] == 1


def test_routes_cache_invalidates_when_the_toolchain_appears(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes, toolchain_available=False)
    assert rt.get_routes()["complete"] is False
    rt.toolchain_state["available"] = True
    assert rt.get_routes()["complete"] is True
    assert fakes.calls["scan_frontend"] == 2


def test_the_routes_helper_runs_beside_the_backend_build(repo: Path, fakes: FakeAnalyzers) -> None:
    """The routes helper runs in a worker thread while this thread builds the model."""
    threads: dict[str, int] = {}
    rt = make_runtime(repo, fakes)
    scan, build = fakes.scan_frontend, fakes.build_backend_model

    def scan_in(*args):
        threads["scan"] = threading.get_ident()
        return scan(*args)

    def build_in(*args):
        threads["build"] = threading.get_ident()
        return build(*args)

    rt.analyzers = dataclasses.replace(rt.analyzers, scan_frontend=scan_in, build_backend_model=build_in)
    rt.get_routes()
    assert threads["build"] == threading.get_ident()
    assert threads["scan"] != threads["build"]


def _record_matched_models(rt: analysis_runtime.AnalysisRuntime, fakes: FakeAnalyzers) -> list[int]:
    """Serials of the backend models `build_routes` matched against, in order."""
    serials: list[int] = []
    build = fakes.build_routes

    def build_in(model, scan):
        serials.append(model.serial)
        return build(model, scan)

    rt.analyzers = dataclasses.replace(rt.analyzers, build_routes=build_in)
    return serials


def test_a_backend_only_edit_reuses_the_frontend_scan(repo: Path, fakes: FakeAnalyzers) -> None:
    """The helper is the slow half of a routes build; a backend edit only
    rebuilds the model and rematches it against the cached scan."""
    rt = make_runtime(repo, fakes)
    matched = _record_matched_models(rt, fakes)
    rt.get_routes()
    write(repo / "backend" / "service_backend" / "x.py", "x = 1\n", 4000)
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 1
    assert fakes.calls["build_backend_model"] == 2
    assert matched == [1, 2], "the rebuilt routes match the edited backend"
    assert rt.status().cache["routes"] == {"loaded": True, "fresh": True}


@pytest.mark.parametrize("rel", ["frontend/lib/api.ts", "frontend/tsconfig.json", "frontend/package-lock.json"])
def test_a_frontend_edit_reruns_the_frontend_scan(repo: Path, fakes: FakeAnalyzers, rel: str) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    write(repo / rel, "// edit\n", 4000)
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2
    assert fakes.calls["build_backend_model"] == 1


def test_a_toolchain_stamp_change_reruns_the_frontend_scan(
    repo: Path, fakes: FakeAnalyzers, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    monkeypatch.setattr(ts_bridge, "helper_digest", lambda: "edited-helper")
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2
    assert fakes.calls["build_backend_model"] == 1


def test_a_racy_frontend_scan_is_not_reused(repo: Path, fakes: FakeAnalyzers) -> None:
    write(repo / "frontend" / "lib" / "fresh.ts", "export {};\n", 5000)
    clock = Clock(5000 * 1_000_000_000 + 500_000_000)
    rt = make_runtime(repo, fakes, clock=clock)
    rt.get_routes()
    write(repo / "backend" / "service_backend" / "x.py", "x = 1\n", 1000)
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2
    clock.now += 5_000_000_000
    rt.get_routes()
    write(repo / "backend" / "service_backend" / "x.py", "x = 22\n", 1000)
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2, "once the window has passed, unchanged content confirms the scan"


def test_a_racy_scan_whose_file_changed_within_its_stamp_is_rescanned(repo: Path, fakes: FakeAnalyzers) -> None:
    fresh = write(repo / "frontend" / "lib" / "fresh.ts", "export {};\n", 5000)
    clock = Clock(5000 * 1_000_000_000 + 500_000_000)
    rt = make_runtime(repo, fakes, clock=clock)
    rt.get_routes()
    write(fresh, "export {}\n;", 5000)  # same size, mtime and inode
    clock.now += 5_000_000_000
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2


def test_a_failed_frontend_scan_is_not_cached(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.raise_in["scan_frontend"] = RuntimeError("boom")
    rt = make_runtime(repo, fakes)
    with pytest.raises(AnalysisFailed):
        rt.get_routes()
    del fakes.raise_in["scan_frontend"]
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2


def test_a_crashed_helper_scan_is_retried_after_a_backend_edit(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.routes = routes_result(complete=False, diagnostics=[_helper_crash_diagnostic()])
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    problem = rt.routes_frontend_problem()
    assert problem is not None and problem.code == "helper_failed"
    fakes.routes = routes_result()
    write(repo / "backend" / "service_backend" / "x.py", "x = 1\n", 4000)
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2
    assert rt.status().toolchain["state"] == "ok"


def test_an_unavailable_frontend_scan_is_cached_until_the_toolchain_changes(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes, toolchain_available=False)
    rt.get_routes()
    write(repo / "backend" / "service_backend" / "x.py", "x = 1\n", 4000)
    assert rt.get_routes()["complete"] is False
    problem = rt.routes_frontend_problem()
    assert problem is not None and problem.code == "typescript_not_found"
    assert fakes.calls["scan_frontend"] == 1
    rt.toolchain_state["available"] = True
    assert rt.get_routes()["complete"] is True
    assert fakes.calls["scan_frontend"] == 2


def test_refresh_reruns_the_frontend_scan(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    rt.refresh()
    assert fakes.calls["scan_frontend"] == 2


def test_missing_toolchain_keeps_backend_routes_and_di(repo: Path, fakes: FakeAnalyzers) -> None:
    """No toolchain is a diagnostic on the routes result, not a failure."""
    rt = make_runtime(repo, fakes, toolchain_available=False)
    routes = rt.get_routes()
    assert routes["complete"] is False
    assert [d["kind"] for d in routes["diagnostics"]] == ["frontend_unavailable"]
    assert rt.get_di()["complete"] is True


# ── Steps and failures ───────────────────────────────────────────────────


def test_failures_name_the_analyzer_step_and_repo_root(repo: Path, fakes: FakeAnalyzers) -> None:
    """The message names the step and says `repo root:`, never `cwd:`."""
    fakes.raise_in["build_backend_model"] = RuntimeError("boom")
    rt = make_runtime(repo, fakes)
    with pytest.raises(AnalysisFailed) as info:
        rt.get_di()
    exc = info.value
    assert (exc.analyzer, exc.step, exc.missing) == ("di_map", "build router graph", None)
    assert str(exc) == f"di_map failed at step 'build router graph': RuntimeError: boom\n  repo root: {repo}"
    assert "cwd" not in str(exc)
    assert rt.last_state["di_map"] == "failed"
    assert rt.status().analyzers["di_map"]["last_step"] == "build router graph"


def test_helper_failures_name_the_routes_helper_step(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.raise_in["scan_frontend"] = ts_bridge.HelperError("routes", "exit", "node exited with code 1: TypeError: x")
    rt = make_runtime(repo, fakes)
    with pytest.raises(AnalysisFailed) as info:
        rt.get_routes()
    assert str(info.value).startswith(
        "route_map failed at step 'run typescript helper (routes mode)': "
        "HelperError: node exited with code 1: TypeError: x\n  repo root: "
    )


@pytest.mark.parametrize(
    ("analyzer", "step"),
    [("build_di", "resolve dependencies"), ("build_routes", "match routes")],
)
def test_each_analyzer_stage_has_its_step(repo: Path, fakes: FakeAnalyzers, analyzer: str, step: str) -> None:
    fakes.raise_in[analyzer] = KeyError("k")
    rt = make_runtime(repo, fakes)
    with pytest.raises(AnalysisFailed, match=f"at step '{step}'"):
        rt.get_routes() if analyzer == "build_routes" else rt.get_di()


def test_missing_paths_are_named(repo: Path, fakes: FakeAnalyzers, tmp_path: Path) -> None:
    fakes.raise_in["build_backend_model"] = FileNotFoundError(2, "No such file", str(repo / "backend" / "x.py"))
    with pytest.raises(AnalysisFailed) as info:
        make_runtime(repo, fakes).get_di()
    assert info.value.missing == "backend/x.py"
    assert str(info.value).endswith("\n  missing: backend/x.py")

    bare = tmp_path / "bare"
    write_config(bare, config_toml())
    with pytest.raises(AnalysisFailed) as missing_pkg:
        make_runtime(bare, FakeAnalyzers()).get_di()
    assert (missing_pkg.value.step, missing_pkg.value.missing) == ("walk backend sources", "backend/service_backend")


# ── Toolchain ────────────────────────────────────────────────────────────


def test_toolchain_is_cached_while_available_and_reprobed_while_not(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes, toolchain_available=False)
    rt.toolchain()
    rt.toolchain()
    assert rt.toolchain_state["probes"] == 2
    rt.toolchain_state["available"] = True
    write(
        repo / TS_DIR / "package.json",
        '{"name": "typescript"}\n',
        1000,
    )
    rt.toolchain()
    rt.toolchain()
    assert rt.toolchain_state["probes"] == 3
    ts_dir = repo / TS_DIR
    write(ts_dir / "package.json", '{"name": "typescript", "exports": {"./sync": "./dist/api/sync/api.js"}}\n', 2000)
    rt.toolchain()
    assert rt.toolchain_state["probes"] == 4, "a changed TypeScript package is re-probed"
    entry = ts_dir / "dist" / "api" / "sync" / "api.js"
    write(entry, "export {};\n", 3000)
    rt.toolchain()
    assert rt.toolchain_state["probes"] == 5
    write(entry, "export const patched = true;\n", 3000)
    rt.toolchain()
    rt.toolchain()
    assert rt.toolchain_state["probes"] == 6, "a replaced compiler is re-probed"


def _node_binary(path: Path, version: str) -> Path:
    write(path, f"#!/bin/sh\necho {version}\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="PATH lookup needs POSIX executables")
def test_a_vanished_or_replaced_node_is_rediscovered(
    repo: Path, fakes: FakeAnalyzers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached toolchain is reused only while the node a probe would choose
    is the same file, so removing, replacing or shadowing it on PATH reaches
    the next helper call without a TypeScript change."""
    write(
        repo / TS_DIR / "package.json",
        '{"name": "typescript", "version": "7.1.0-beta"}\n',
    )
    first, second = tmp_path / "bin-a", tmp_path / "bin-b"
    monkeypatch.delenv(ts_bridge.NODE_ENV, raising=False)
    monkeypatch.setenv("PATH", f"{first}{os.pathsep}{second}")
    _node_binary(second / "node", "v24.1.0")
    old = _node_binary(first / "node", "v24.2.0")
    probes: list[str | None] = []

    def find(root: Path) -> ts_bridge.Toolchain:
        def run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(cmd, capture_output=True, check=False)

        tc = ts_bridge.find_toolchain(root, run=run)
        probes.append(tc.node)
        return tc

    rt = analysis_runtime.AnalysisRuntime(
        repo, ConfigFile.locate(repo), [], clock=Clock(), find_toolchain=find, analyzers=fakes.analyzers()
    )
    assert rt.toolchain().node == str(old)
    stamp = rt.toolchain().stamp()
    assert len(probes) == 1, "an unchanged node is not re-probed"

    old.unlink()
    recovered = rt.toolchain()
    assert recovered.available and recovered.node == str(second / "node")
    assert recovered.node_version == "v24.1.0" and recovered.stamp() != stamp

    _node_binary(first / "node", "v24.3.0")
    shadowed = rt.toolchain()
    assert shadowed.node == str(first / "node") and shadowed.node_version == "v24.3.0"

    (first / "node").unlink()
    # A real reinstall differs in size or time. A same-size rewrite within one
    # clock tick can reuse the inode on Linux and look identical to stat.
    _node_binary(first / "node", "v24.10.0")
    assert rt.toolchain().node_version == "v24.10.0", "a node replaced in place is re-probed"
    rt.toolchain()
    assert len(probes) == 4


# ── JSX and symbols ──────────────────────────────────────────────────────


def test_jsx_queries_walk_their_own_root_every_call(repo: Path, fakes: FakeAnalyzers) -> None:
    """No root-level JSX cache. Every query hands its own root to the
    analyzer, which reads the per-file facts cache, so a `.jsx` edit or a root
    outside `frontend/` shows up on the next call."""
    rt = make_runtime(repo, fakes)
    fakes.usages = [FakeUsage("frontend/a.jsx", 1)]
    first = rt.get_jsx_usage("Widget", repo / "frontend")
    fakes.usages = [FakeUsage("frontend/a.jsx", 1), FakeUsage("frontend/a.jsx", 9)]
    second = rt.get_jsx_usage("Widget", repo / "frontend")
    assert (len(first.usages), len(second.usages)) == (1, 2)
    other = write(repo / "tools" / "web" / "x.jsx", "<Widget/>\n").parent
    rt.get_jsx_usage("Widget", other, include_self_close=False, include_tests=True)
    assert [c["root"] for c in fakes.jsx_calls] == [repo / "frontend", repo / "frontend", other]
    assert fakes.jsx_calls[-1]["include_self_close"] is False
    assert fakes.jsx_calls[-1]["include_tests"] is True
    assert fakes.calls["make_facts_cache"] == 1, "one per-file facts cache serves every query"


def test_jsx_returns_problems_and_needs_the_toolchain(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.jsx_problems = [read_failed("frontend/a.tsx")]
    assert make_runtime(repo, fakes).get_jsx_usage("W", repo / "frontend").problems == fakes.jsx_problems
    with pytest.raises(ts_bridge.ToolchainUnavailable, match="package.json not found"):
        make_runtime(repo, FakeAnalyzers(), toolchain_available=False).get_jsx_usage("W", repo / "frontend")


@pytest.mark.parametrize("root", ["..", "/", "frontend/lib/api.ts", "nope"])
def test_resolve_root_rejects_paths_outside_or_not_directories(repo: Path, fakes: FakeAnalyzers, root: str) -> None:
    with pytest.raises(InvalidRoot) as info:
        make_runtime(repo, fakes).resolve_root(root)
    assert info.value.root == root
    assert repr(root) in str(info.value)


def test_resolve_root_accepts_directories_in_the_repo(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    assert rt.resolve_root("frontend") == (repo / "frontend").resolve()
    assert rt.resolve_root(".") == repo.resolve()


def test_symbol_index_is_owned_by_the_runtime(repo: Path, fakes: FakeAnalyzers) -> None:
    """The runtime owns one symbol index, built on the shared facts cache."""
    rt = make_runtime(repo, fakes)
    fakes.symbols = FakeSymbolResult([])
    rt.resolve_symbol("X", side="both", kind=None, file_hint=None)
    rt.resolve_symbol("Y", side="frontend", kind="component", file_hint="ui")
    assert fakes.calls["make_symbol_index"] == 1
    assert fakes.calls["make_facts_cache"] == 1
    assert fakes.symbol_calls[-1] == {"name": "Y", "side": "frontend", "kind_filter": "component", "file_hint": "ui"}


# ── Refresh ──────────────────────────────────────────────────────────────


def test_refresh_drops_every_cache_and_rebuilds(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    rt.get_routes()
    rt.get_jsx_usage("W", repo / "frontend")
    fakes.symbols = FakeSymbolResult([])
    rt.resolve_symbol("X", side="backend", kind=None, file_hint=None)
    fakes.di = di_result(complete=False, diagnostics=[read_failed()], limitations=[limitation()])
    fakes.routes = routes_result(limitations=[limitation()])
    result = rt.refresh()
    assert fakes.calls["build_backend_model"] == 2
    assert fakes.calls["scan_frontend"] == 2
    assert fakes.facts.cleared == 1
    rt.resolve_symbol("X", side="backend", kind=None, file_hint=None)
    assert fakes.calls["make_symbol_index"] == 2, "refresh drops the symbol index with the facts cache"
    assert result.complete is False
    assert result.analysis["diagnostics"] == [read_failed()]
    assert result.analysis["analysis_limitations"] == [limitation()], "limitations from both analyzers are deduped"


# ── Status ───────────────────────────────────────────────────────────────


def test_status_completeness_needs_both_analyzers_fresh(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    assert rt.status().complete is None, "nothing has run yet: unknown, not healthy"
    rt.get_di()
    assert rt.status().complete is None, "routes never ran"
    rt.get_routes()
    snapshot = rt.status()
    assert snapshot.complete is True
    assert snapshot.cache["di"] == {"loaded": True, "fresh": True}
    assert snapshot.cache["routes"] == {"loaded": True, "fresh": True}


def test_status_drops_stale_caches_with_their_state(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    rt.get_routes()
    write(repo / "frontend" / "lib" / "new.ts", "export {};\n", 4000)
    snapshot = rt.status()
    assert snapshot.complete is None
    assert snapshot.cache["routes"] == {"loaded": False, "fresh": False}
    assert snapshot.analyzers["route_map"]["state"] == "not_run"
    assert snapshot.cache["di"] == {"loaded": True, "fresh": True}, "the reset is scoped to what changed"
    assert snapshot.analyzers["di_map"]["state"] == "complete"


def test_status_incomplete_and_failed_runs(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.di = di_result(complete=False, diagnostics=[read_failed()])
    rt = make_runtime(repo, fakes)
    rt.get_di()
    snapshot = rt.status()
    assert snapshot.complete is False
    assert snapshot.diagnostics == [read_failed()]

    failing = FakeAnalyzers()
    failing.raise_in["build_routes"] = RuntimeError("x")
    rt2 = make_runtime(repo, failing)
    rt2.get_di()
    with pytest.raises(AnalysisFailed):
        rt2.get_routes()
    assert rt2.status().complete is False
    assert rt2.status().analyzers["route_map"]["last_error"].startswith("route_map failed at step 'match routes'")


def test_status_dedupes_limitations_reported_by_both_analyzers(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.di = di_result(limitations=[limitation()])
    fakes.routes = routes_result(limitations=[limitation()])
    rt = make_runtime(repo, fakes)
    rt.get_di()
    rt.get_routes()
    assert rt.status().limitations == [limitation()]


def test_status_counts_are_refused_on_an_old_interpreter_without_analysis(repo: Path, fakes: FakeAnalyzers) -> None:
    """No analysis runs; counts are null with the refusal named."""
    rt = make_runtime(repo, fakes, version_info=(3, 12))
    snapshot = rt.status(load_counts=True)
    assert snapshot.counts is None
    assert snapshot.counts_error is not None
    assert snapshot.counts_error["error"] == "runtime_incompatible"
    assert snapshot.counts_error is not None
    assert (snapshot.counts_error["running"], snapshot.counts_error["required"]) == ("3.12", "3.14")
    assert fakes.calls == {"list_repo_files": 1}, "status lists the files to revalidate, nothing more"


def test_status_counts_survive_one_failing_analyzer(repo: Path, fakes: FakeAnalyzers) -> None:
    """Each analyzer is counted in its own try."""
    fakes.raise_in["build_routes"] = RuntimeError("boom")
    snapshot = make_runtime(repo, fakes).status(load_counts=True)
    assert snapshot.counts is not None
    assert snapshot.counts["di"]["routes"] == 0
    assert snapshot.counts is not None
    assert snapshot.counts["routes"] is None
    assert snapshot.counts_errors["routes"]["error"] == "analysis_failed"
    assert snapshot.counts_errors["routes"]["step"] == "match routes"
    assert snapshot.complete is False


def test_status_counts_without_toolchain_keep_di(repo: Path, fakes: FakeAnalyzers) -> None:
    snapshot = make_runtime(repo, fakes, toolchain_available=False).status(load_counts=True)
    assert snapshot.counts is not None
    assert snapshot.counts["di"] is not None
    assert snapshot.counts is not None
    assert snapshot.counts["routes"] is None
    error = snapshot.counts_errors["routes"]
    assert error["error"] == "frontend_unavailable"
    assert error["missing"] == "ts-cache/node_modules/typescript/package.json"
    assert error["fix"].startswith('run "code-intel setup"')


def test_status_names_the_locked_fastapi_and_its_include_model(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    missing = rt.status().fastapi
    assert (missing["fastapi_version"], missing["include_model"]) == (None, "unknown")
    assert "backend/uv.lock" in missing["include_model_reason"]
    assert str(repo) not in missing["include_model_reason"]

    lock = '[[package]]\nname = "fastapi"\nversion = "{}"\n'
    write(repo / "backend" / "uv.lock", lock.format("0.135.2"), 3000)
    copy = rt.status().fastapi
    assert (copy["fastapi_version"], copy["include_model"], copy["source"]) == ("0.135.2", "copy", "backend/uv.lock")
    write(repo / "backend" / "uv.lock", lock.format("0.143.0"), 4000)
    assert rt.status().fastapi["include_model"] == "live"


def test_status_toolchain_block(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    block = rt.status().toolchain
    assert block["state"] == "ok"
    assert block["node"] == {"path": "/usr/bin/node", "version": "v24.21.0"}
    assert block["typescript"] == {
        "dir": "ts-cache/node_modules/typescript",
        "version": "7.1.0-beta",
        "source": "helper",
        "lockfile_version": "7.1.0-beta",
    }
    assert block["facts_cache"] is None, "the probe never runs the helper"
    rt.get_jsx_usage("W", repo / "frontend")
    assert rt.status().toolchain["facts_cache"]["helper_runs"] == 1

    down = make_runtime(repo, FakeAnalyzers(), toolchain_available=False).status().toolchain
    assert down["state"] == "unavailable"
    assert down["problem"]["code"] == "typescript_not_found"
    assert down["problem"]["missing"] == "ts-cache/node_modules/typescript/package.json"


# ── Code staleness ───────────────────────────────────────────────────────


def test_code_watch_ignores_touch_and_remembers_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a touch whose content matches, later calls compare stat only."""
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    watch = analysis_runtime.CodeWatch([src], clock=Clock())
    hashed: list[int] = []
    real = analysis_runtime.source_digest

    def counting(path):
        hashed.append(1)
        return real(path)

    monkeypatch.setattr(analysis_runtime, "source_digest", counting)
    os.utime(src, (2000, 2000))
    assert watch.stale() is False
    assert watch.stale() is False
    assert watch.stale() is False
    assert len(hashed) == 1


def test_code_watch_flags_content_change_and_deletion(tmp_path: Path) -> None:
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    watch = analysis_runtime.CodeWatch([src], clock=Clock())
    write(src, "A = 2\n", 2000)
    assert watch.stale() is True
    src.unlink()
    assert watch.stale() is True


def test_code_watch_reports_a_file_modified_around_its_load_as_unconfirmed(tmp_path: Path) -> None:
    """A module loaded without a record of its bytes was read before the watch
    hashed it: modified within the racy window of that load, it may have been
    loaded with other bytes, so the watch claims neither stale nor current. A
    real change afterwards is still a confirmed change."""
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    other = write(tmp_path / "other.py", "B = 1\n", 10)
    loaded_at = 1000 * 1_000_000_000 + 1
    recent = analysis_runtime.CodeWatch([src, other], clock=Clock(), loaded_at_ns=loaded_at)
    assert recent.state() == analysis_runtime.CodeState(changed=(), unconfirmed=(src,))
    assert recent.stale() is None
    assert recent.stale() is None, "nothing later can confirm it, so it stays unconfirmed"
    write(src, "A = 2\n", 2000)
    assert recent.state() == analysis_runtime.CodeState(changed=(src,), unconfirmed=())
    assert recent.stale() is True

    settled = write(tmp_path / "settled.py", "A = 1\n", 1000)
    old = analysis_runtime.CodeWatch(
        [settled], clock=Clock(), loaded_at_ns=loaded_at + 10 * analysis_runtime.CodeWatch.RACY_WINDOW_NS
    )
    assert old.stale() is False


def test_code_watch_trusts_a_recorded_import_digest_whatever_the_mtime(tmp_path: Path) -> None:
    """A digest recorded from the bytes the module was compiled from is exact,
    so a file modified around its import is confirmed, and a later edit is a
    change."""
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    loaded_at = 1000 * 1_000_000_000 + 1
    digest = analysis_runtime.source_digest(src)
    assert digest is not None
    imported = {src: digest}
    watch = analysis_runtime.CodeWatch([src], clock=Clock(), loaded_at_ns=loaded_at, imported=imported)
    assert watch.state() == analysis_runtime.CodeState()
    assert watch.stale() is False
    write(src, "A = 2\n", 2000)
    assert watch.stale() is True


def test_code_watch_sees_a_reload_that_replaces_a_recorded_digest(tmp_path: Path) -> None:
    """A reload rewrites a record without adding one, so the cached state must
    follow the record's contents, not its size."""
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    imported = {src: analysis_runtime.source_digest(src) or ""}
    watch = analysis_runtime.CodeWatch([src], clock=Clock(), imported=imported)
    write(src, "A = 2\n", 2000)
    assert watch.stale() is True
    assert watch.stale() is True
    imported[src] = analysis_runtime.source_digest(src) or ""
    assert watch.stale() is False


def test_code_watch_compares_against_the_bytes_imported_not_those_at_start(tmp_path: Path) -> None:
    """An edit between a module's import and the watch's start is a change:
    the server runs the bytes it imported."""
    src = write(tmp_path / "mod.py", "A = 2\n", 1000)
    imported = {src: hashlib.sha256(b"A = 1\n").hexdigest()}
    watch = analysis_runtime.CodeWatch([src], clock=Clock(), imported=imported)
    assert watch.state().changed == (src,)


def test_code_watch_skips_a_module_not_loaded_yet_and_checks_it_once_imported(tmp_path: Path) -> None:
    """Analyzers import lazily: a module with no import record that is not in
    sys.modules runs no old code, so editing it is not stale; once it imports,
    its recorded bytes are what it is compared with."""
    src = write(tmp_path / "code_watch_never_imported_module.py", "A = 1\n", 1000)
    imported: dict[Path, str] = {}
    watch = analysis_runtime.CodeWatch([src], clock=Clock(), imported=imported)
    write(src, "A = 2\n", 2000)
    assert watch.state() == analysis_runtime.CodeState()
    imported[src] = analysis_runtime.source_digest(src) or ""
    assert watch.stale() is False
    write(src, "A = 3\n", 3000)
    assert watch.stale() is True


def test_code_watch_falls_back_to_the_start_digest_for_a_module_loaded_without_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write(tmp_path / "code_watch_preloaded_module.py", "A = 1\n", 1000)
    monkeypatch.setitem(sys.modules, src.stem, types.SimpleNamespace(__file__=str(src)))
    watch = analysis_runtime.CodeWatch([src], clock=Clock(), loaded_at_ns=1000 * 1_000_000_000, imported={})
    assert watch.state() == analysis_runtime.CodeState(unconfirmed=(src,))
    write(src, "A = 2\n", 2000)
    assert watch.stale() is True


def test_runtime_names_each_unconfirmed_file_and_why(repo: Path, fakes: FakeAnalyzers) -> None:
    src = write(repo / "tools" / "mod.py", "A = 1\n", 1000)
    rt = analysis_runtime.AnalysisRuntime(
        repo, ConfigFile.locate(repo), [src], clock=Clock(), analyzers=fakes.analyzers(), code_loaded_at_ns=1000 * 10**9
    )
    state = rt.code_state()
    assert state.stale is None
    assert rt.code_unconfirmed(state) == [
        {
            "file": "tools/mod.py",
            "reason": "modified within 2 s of the server loading it, so the bytes it loaded cannot be compared "
            "with the file on disk",
        }
    ]
    snapshot = rt.status()
    assert snapshot.code_stale is None
    assert snapshot.code_unconfirmed == rt.code_unconfirmed(state)


def test_code_watch_rereads_a_same_size_edit_in_the_snapshot_tick(tmp_path: Path) -> None:
    """A same-size edit within the snapshot's mtime tick keeps the stat signature;
    as in git's racy-clean rule, the content is compared while the stamp is racy."""
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    watch = analysis_runtime.CodeWatch([src], clock=Clock(1000 * 1_000_000_000))
    write(src, "A = 2\n", 1000)
    assert analysis_runtime.stat_signature([src]) == watch._stat
    assert watch.stale() is True


def test_code_watch_trusts_stat_once_the_racy_window_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is driven by the injected clock, not by elapsed time."""
    src = write(tmp_path / "mod.py", "A = 1\n", 1000)
    clock = Clock(1000 * 1_000_000_000)
    watch = analysis_runtime.CodeWatch([src], clock=clock)
    hashed: list[int] = []
    real = analysis_runtime.source_digest

    def counting(path):
        hashed.append(1)
        return real(path)

    monkeypatch.setattr(analysis_runtime, "source_digest", counting)
    assert watch.stale() is False
    assert len(hashed) == 1, "inside the window the content is compared"
    clock.now += 3 * analysis_runtime.CodeWatch.RACY_WINDOW_NS
    assert watch.stale() is False
    assert watch.stale() is False
    assert len(hashed) == 2, "after the window one confirming hash, then stat only"


@pytest.mark.real_ctime
def test_code_watch_sees_a_rewrite_with_its_mtime_restored_after_the_window(
    tmp_path: Path, rewrite_keeping_mtime
) -> None:
    """Once the window has passed the watch compares stat only, and the
    kernel's ctime in it still shows an analyzer file rewritten with the same
    size and its old mtime put back."""
    src = tmp_path / "mod.py"
    src.write_text("A = 1\n", encoding="utf-8")
    clock = Clock(time.time_ns() + 3 * analysis_runtime.CodeWatch.RACY_WINDOW_NS)
    watch = analysis_runtime.CodeWatch([src], clock=clock)
    assert watch.stale() is False
    assert watch.stale() is False
    rewrite_keeping_mtime(src, "A = 2\n")
    clock.now = time.time_ns() + 3 * analysis_runtime.CodeWatch.RACY_WINDOW_NS
    assert watch.stale() is True


def test_watched_files_are_every_package_module_resolved(tmp_path: Path) -> None:
    write(tmp_path / "pkg" / "a.py", "")
    write(tmp_path / "pkg" / "sub" / "b.py", "")
    write(tmp_path / "pkg" / "notes.txt", "")
    (tmp_path / "link").symlink_to(tmp_path / "pkg", target_is_directory=True)
    files = analysis_runtime.watched_files(tmp_path / "link")
    assert [p.relative_to(tmp_path / "pkg").as_posix() for p in files] == ["a.py", "sub/b.py"]
    assert all(p.is_absolute() for p in files)


# ── Timing helper ─────────────────────────────────────────────────────────


def test_scaling_check_times_with_the_collector_paused() -> None:
    import gc

    import scaling_check

    seen: list[bool] = []
    assert gc.isenabled()
    scaling_check.best_time(lambda _: seen.append(gc.isenabled()), None, repeats=2)
    assert seen and not any(seen), "no measurement may run with the collector on"
    assert gc.isenabled(), "the previous state is restored"

    gc.disable()
    try:
        scaling_check.best_time(lambda _: None, None, repeats=2)
        assert not gc.isenabled(), "a caller that had it off keeps it off"
    finally:
        gc.enable()


def test_scaling_check_measures_an_over_bound_ratio_once_more_before_failing() -> None:
    import scaling_check

    noisy = iter([3.4, 2.1])
    assert scaling_check._ratios(lambda: next(noisy), 3.0) == [3.4, 2.1], "a one-off spike is re-measured"
    steady = iter([4.0, 4.1, 9.9])
    assert scaling_check._ratios(lambda: next(steady), 3.0) == [4.0, 4.1], "a real regression fails"
    with pytest.raises(AssertionError, match=r"took \d+\.\d\dx, then \d+\.\d\dx as long"):
        scaling_check.assert_costs_like(2, 1, lambda _: None, max_ratio=0.0)


# ── Real analyzers, end to end ───────────────────────────────────────────


_API_TS = """\
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, init);
}
export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  return (await apiFetch(path, init)).json() as Promise<T>;
}
export async function apiJsonOr204<T>(path: string, init?: RequestInit): Promise<T | null> {
  const res = await apiFetch(path, init);
  return res.status === 204 ? null : ((await res.json()) as T);
}
"""

_DOWNLOAD_TS = """\
import { apiFetch } from "../api";
export async function downloadExport(options: { url: string }): Promise<void> {
  await apiFetch(options.url);
}
"""

_MAIN_PY = """\
from fastapi import FastAPI

app = FastAPI(redirect_slashes=False)


@app.get("/items")
def list_items():
    return []
"""


def _e2e_runtime(repo: Path, typescript_toolchain: ts_bridge.Toolchain) -> analysis_runtime.AnalysisRuntime:
    """The real analyzers over ``repo`` (a git work tree with its config)."""
    return analysis_runtime.AnalysisRuntime(
        repo,
        ConfigFile.locate(repo),
        [],
        clock=Clock(LATE_NS),
        find_toolchain=lambda root: _with_root(typescript_toolchain, root),
    )


def _e2e_repo(root: Path) -> Path:
    """A git work tree (nothing committed) with a FastAPI app, a frontend and their config."""
    git_init(root)
    write_config(root, config_toml(E2E_LAYOUT))
    write(root / ".python-version", "3.14\n", 1000)
    write(root / "backend" / "pyproject.toml", "[project]\nname = 'x'\n", 1000)
    write(root / "backend" / "service_backend" / "__init__.py", "", 1000)
    write(root / "backend" / "service_backend" / "main.py", _MAIN_PY, 1000)
    write(root / "frontend" / "tsconfig.json", '{"compilerOptions": {"jsx": "preserve"}}\n', 1000)
    write(root / "frontend" / "lib" / "api.ts", _API_TS, 1000)
    write(root / "frontend" / "lib" / "utils" / "download-export.ts", _DOWNLOAD_TS, 1000)
    for name in ("a", "b"):
        write(
            root / "frontend" / "lib" / f"{name}.ts",
            f'import {{ apiJson }} from "./api";\nexport const {name} = () => apiJson("/items");\n',
            1000,
        )
    return root


@pytest.mark.usefixtures("module_git")
def test_get_routes_end_to_end_keeps_same_line_calls_in_two_files(tmp_path: Path, typescript_toolchain) -> None:
    """The runtime does not dedupe; two files calling the same route on
    the same line are two frontend calls, and both match."""
    repo = _e2e_repo(tmp_path / "repo")
    rt = _e2e_runtime(repo, typescript_toolchain)
    routes = rt.get_routes()
    calls = [c for c in routes["frontend_calls"] if c["path"] == "/items" and not c["is_test"]]
    assert sorted(c["file"] for c in calls) == ["frontend/lib/a.ts", "frontend/lib/b.ts"]
    assert [c["line"] for c in calls] == [2, 2]
    assert len([m for m in routes["matches"] if m["backend"]["path"] == "/items"]) == 2
    assert routes["diagnostics"] == []


@pytest.mark.usefixtures("module_git")
def test_jsx_edit_in_a_jsx_file_shows_up_on_the_next_call(tmp_path: Path, typescript_toolchain) -> None:
    """A `.jsx` edit, and a root outside the frontend root, are seen on the next call."""
    repo = git_init(tmp_path / "repo")
    write_config(repo, config_toml(E2E_LAYOUT))
    widget = write(repo / "web" / "page.jsx", "export const Page = () => <Widget />;\n", 1000)
    rt = _e2e_runtime(repo, typescript_toolchain)
    assert len(rt.get_jsx_usage("Widget", repo / "web").usages) == 1
    write(widget, "export const Page = () => (\n  <>\n    <Widget />\n    <Widget />\n  </>\n);\n", 2000)
    assert [u.line for u in rt.get_jsx_usage("Widget", repo / "web").usages] == [3, 4]


def _with_root(toolchain: ts_bridge.Toolchain, root: Path) -> ts_bridge.Toolchain:
    return dataclasses.replace(toolchain, repo_root=root)


def test_the_toolchain_probe_never_waits_on_the_runtime_lock(repo: Path, fakes: FakeAnalyzers) -> None:
    """Symbol search probes the toolchain from a worker thread while the calling
    thread holds the runtime lock and waits for that worker."""
    rt = make_runtime(repo, fakes)
    probed: list = []
    with rt._lock:
        worker = threading.Thread(target=lambda: probed.append(rt.toolchain()), daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "toolchain() waited on the runtime lock"
    assert probed and probed[0].available


_SEARCH_BOTH_SIDES = """
import json, sys
from pathlib import Path
from code_intel import analysis_runtime
from code_intel.config import ConfigFile
repo = Path(sys.argv[1])
rt = analysis_runtime.AnalysisRuntime(repo, ConfigFile.locate(repo), [])
result = rt.resolve_symbol("list_items", side="both", kind=None, file_hint=None)
print(json.dumps({"complete": result.complete, "rows": sorted([c.language, c.file] for c in result.candidates)}))
"""


@pytest.mark.usefixtures("module_git")
def test_symbol_search_on_both_sides_runs_with_the_real_index(tmp_path: Path, typescript_toolchain) -> None:
    """End to end with the real index and facts cache; a subprocess with a
    timeout, so a deadlock fails the test instead of hanging the suite."""
    repo = _e2e_repo(tmp_path / "repo")
    write(repo / "backend" / "service_backend" / "items.py", "def list_items():\n    return []\n", 1000)
    write(repo / "frontend" / "lib" / "items.ts", "export function list_items() {\n  return [];\n}\n", 1000)
    env = {**os.environ, ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)}
    proc = subprocess.run(
        [sys.executable, "-c", _SEARCH_BOTH_SIDES, str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "complete": True,
        "rows": [
            ["python", "backend/service_backend/items.py"],
            ["python", "backend/service_backend/main.py"],
            ["typescript", "frontend/lib/items.ts"],
        ],
    }


@pytest.mark.usefixtures("module_git")
def test_backend_rebuilds_reanalyze_only_changed_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real backend analyzer keeps its per-module router facts between
    builds; a reset (no previous sources) or another backend config starts afresh."""
    from code_intel import router_bases

    repo = _e2e_repo(tmp_path / "repo")
    write(
        repo / "backend" / "service_backend" / "extra.py",
        'from fastapi import APIRouter\nrouter = APIRouter()\n@router.get("/x")\ndef x(): ...\n',
        1000,
    )
    analyzed: list[str] = []
    real = router_bases.analyze
    monkeypatch.setattr(router_bases, "analyze", lambda mod: analyzed.append(mod.name) or real(mod))
    real_build = analysis_runtime.Analyzers.real().build_backend_model
    backend = load_config(repo).backend
    visible = frozenset(path.relative_to(repo).as_posix() for path in repo.rglob("*.py"))

    def build(sources: Any, config: BackendConfig = backend) -> Any:
        return real_build(repo, config, sources, visible)

    first = build(None)
    assert sorted(analyzed) == ["service_backend.extra", "service_backend.main"]
    analyzed.clear()
    write(repo / "backend" / "service_backend" / "main.py", _MAIN_PY + "\n# edited\n", 2000)
    second = build(first.sources)
    assert analyzed == ["service_backend.main"]
    assert [r.path for r in second.routes] == ["/items"]
    analyzed.clear()
    third = build(None)
    assert sorted(analyzed) == ["service_backend.extra", "service_backend.main"]
    analyzed.clear()
    build(third.sources, dataclasses.replace(backend, app="service_backend.extra:router"))
    assert sorted(analyzed) == ["service_backend.extra", "service_backend.main"], "a new backend config starts afresh"


# ── Status right after an edit, frontend causes, helper crashes ──────────


def test_status_right_after_an_edit_keeps_the_caches_it_built(repo: Path, fakes: FakeAnalyzers) -> None:
    """Sources modified within the racy window: status keeps the caches it just
    built (not fresh, so the next call checks them again) instead of reporting
    "not loaded" beside the run's timing."""
    write(repo / "backend" / "service_backend" / "fresh.py", "x = 1\n", 5000)
    clock = Clock(5000 * 1_000_000_000 + 500_000_000)
    rt = make_runtime(repo, fakes, clock=clock)
    snapshot = rt.status(load_counts=True)
    assert snapshot.cache["di"] == {"loaded": True, "fresh": False}
    assert snapshot.cache["routes"] == {"loaded": True, "fresh": False}
    assert snapshot.analyzers["di_map"]["state"] == "complete"
    assert snapshot.analyzers["di_map"]["last_runtime_ms"] is not None
    assert snapshot.complete is None, "an unconfirmed cache is not proof of a complete run"

    builds = fakes.calls["build_backend_model"]
    clock.now += 5_000_000_000
    later = rt.status()
    assert later.cache["di"] == {"loaded": True, "fresh": True}, "unchanged content confirms it once the window passed"
    assert later.cache["routes"] == {"loaded": True, "fresh": True}
    assert later.complete is True
    assert fakes.calls["build_backend_model"] == builds, "status confirms without rebuilding"


def test_git_sha_hides_the_console_window_on_windows(repo: Path, fakes: FakeAnalyzers, monkeypatch) -> None:
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(analysis_runtime.subprocess, "run", fake_run)
    assert make_runtime(repo, fakes).git_sha() == "abc123"
    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    assert seen["creationflags"] == expected


def test_an_unusable_toolchain_without_a_problem_names_the_missing_part(repo: Path) -> None:
    broken = ts_bridge.Toolchain(
        node=None,
        node_version=None,
        typescript_dir=repo / "ts",
        typescript_version="7.1.0-beta",
        typescript_source="env",
        lockfile_typescript_version=None,
        problem=None,
        warnings=(),
    )
    problem = ts_bridge.incomplete_toolchain_problem(broken)
    assert (problem.code, problem.missing, problem.fix) == ("toolchain_incomplete", None, ts_bridge.NODE_FIX)
    assert problem.message.startswith("toolchain incomplete: node not resolved")
    assert "node=None" in problem.message and "typescript_dir=" in problem.message
    assert analysis_runtime.toolchain_status(broken, repo, None)["problem"]["code"] == "toolchain_incomplete"


def test_routes_frontend_problem_names_each_cause(repo: Path) -> None:
    from mcp_fixtures import frontend_unavailable_diagnostic, toolchain

    ok, down = toolchain(repo), toolchain(repo, available=False)
    diag = {"kind": "frontend_unavailable", "message": "x failed. Fix: do y", "fix": "do y", "missing": None}
    assert analysis_runtime.routes_frontend_problem(diag, ok, frontend_present=True).code == "helper_failed"
    assert analysis_runtime.routes_frontend_problem(diag, ok, frontend_present=True).message == "x failed"
    assert analysis_runtime.routes_frontend_problem(diag, ok, frontend_present=False).code == "frontend_not_found"
    from_probe = analysis_runtime.routes_frontend_problem(
        frontend_unavailable_diagnostic(), down, frontend_present=True
    )
    assert (from_probe.code, from_probe.missing) == (
        "typescript_not_found",
        "ts-cache/node_modules/typescript/package.json",
    )
    explicit = analysis_runtime.routes_frontend_problem({**diag, "problem": "node_too_old"}, ok, frontend_present=True)
    assert explicit.code == "node_too_old", "an explicit code on the diagnostic wins"


def _helper_crash_diagnostic() -> dict:
    return {
        "kind": "frontend_unavailable",
        "category": "content_lost",
        "stage": "route_map",
        "step": "analyze frontend",
        "file": None,
        "line": None,
        "message": "helper.cjs: typescript helper (routes mode) failed at step 'exit': boom",
        "fix": analysis_runtime.HELPER_FAILED_FIX,
    }


def test_a_routes_helper_crash_is_remembered_for_status(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.routes = routes_result(complete=False, diagnostics=[_helper_crash_diagnostic()])
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    problem = rt.routes_frontend_problem()
    assert problem is not None and problem.code == "helper_failed"
    assert rt.status().toolchain["state"] == "helper_failed"
    fakes.routes = routes_result()
    write(repo / "frontend" / "lib" / "more.ts", "export {};\n", 4000)
    rt.get_routes()
    assert rt.routes_frontend_problem() is None
    assert rt.status().toolchain["state"] == "ok"


def test_routes_recover_from_a_helper_crash_without_a_file_edit(repo: Path, fakes: FakeAnalyzers) -> None:
    """A crashed routes helper is not a settled answer: the next call reruns the
    helper on unchanged sources and returns the recovered result."""
    fakes.routes = routes_result(complete=False, diagnostics=[_helper_crash_diagnostic()])
    rt = make_runtime(repo, fakes)
    assert rt.get_routes()["complete"] is False
    problem = rt.routes_frontend_problem()
    assert problem is not None and problem.code == "helper_failed"
    fakes.routes = routes_result()
    recovered = rt.get_routes()
    assert recovered["complete"] is True
    assert rt.routes_frontend_problem() is None
    assert fakes.calls["scan_frontend"] == 2
    assert rt.status().toolchain["state"] == "ok"
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2, "a recovered result is cached like any other"


def test_a_jsx_success_does_not_clear_a_routes_helper_failure(repo: Path, fakes: FakeAnalyzers) -> None:
    """Each helper mode keeps its own failure until a run in that mode succeeds."""
    fakes.routes = routes_result(complete=False, diagnostics=[_helper_crash_diagnostic()])
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    rt.get_jsx_usage("W", repo / "frontend")
    block = rt.status().toolchain
    assert block["state"] == "helper_failed"
    assert list(block["helper_failures"]) == ["routes"]
    assert block["problem"] == block["helper_failures"]["routes"]
    assert "(routes mode)" in block["problem"]["message"]

    fakes.routes = routes_result()
    rt.get_routes()
    block = rt.status().toolchain
    assert (block["state"], block["problem"], block["helper_failures"]) == ("ok", None, {})


def test_a_routes_success_does_not_clear_a_facts_helper_failure(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.raise_in["find_jsx_usage"] = ts_bridge.HelperError("facts", "exit", "node exited with code 1: boom")
    rt = make_runtime(repo, fakes)
    with pytest.raises(analysis_runtime.HelperFailed):
        rt.get_jsx_usage("W", repo / "frontend")
    rt.get_routes()
    fakes.routes = routes_result(complete=False, diagnostics=[_helper_crash_diagnostic()])
    write(repo / "frontend" / "lib" / "more.ts", "export {};\n", 4000)
    rt.get_routes()
    assert sorted(rt.status().toolchain["helper_failures"]) == ["facts", "routes"]

    del fakes.raise_in["find_jsx_usage"]
    rt.get_jsx_usage("W", repo / "frontend")
    block = rt.status().toolchain
    assert (block["state"], list(block["helper_failures"])) == ("helper_failed", ["routes"])


def test_a_symbol_helper_crash_on_side_both_is_remembered(repo: Path, fakes: FakeAnalyzers) -> None:
    fakes.symbols = FakeSymbolResult(
        [],
        complete=False,
        frontend_problem=analysis_runtime.FrontendProblem("helper_failed", "helper crashed", None, "rerun"),
    )
    rt = make_runtime(repo, fakes)
    rt.resolve_symbol("X", side="both", kind=None, file_hint=None)
    assert rt.status().toolchain["problem"]["message"] == "helper crashed"
    fakes.symbols = FakeSymbolResult([])
    rt.resolve_symbol("X", side="both", kind=None, file_hint=None)
    assert rt.status().toolchain["state"] == "ok"


def test_a_relative_typescript_dir_resolves_against_the_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    """Not against the server's working directory, which an MCP client chooses."""
    repo = make_repo(tmp_path / "repo")
    vendored = repo / "vendor" / "typescript"
    vendored.parent.mkdir(parents=True)
    vendored.symlink_to(typescript_toolchain.typescript_dir, target_is_directory=True)
    monkeypatch.setenv(ts_bridge.TS_DIR_ENV, "vendor/typescript")
    monkeypatch.chdir(tmp_path)
    rt = analysis_runtime.AnalysisRuntime(repo, ConfigFile.locate(repo), [])
    toolchain = rt.toolchain()
    assert toolchain.available, toolchain.problem
    assert toolchain.typescript_dir is not None
    assert toolchain.typescript_dir.resolve() == typescript_toolchain.typescript_dir.resolve()
    assert rt.status().toolchain["typescript"]["source"] == "env"


# ── Docs report ──────────────────────────────────────────────────────────


def doc_repo(root: Path) -> Path:
    make_repo(root)
    write_doc_config(root)
    write(root / "docs" / "README.md", "# Docs\n\n[A](a.md)\n")
    write(root / "docs" / "a.md", "# A\n\nSee `backend/service_backend/main.py` and `GET /items`.\n")
    return root


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    return doc_repo(tmp_path / "repo")


def doc_fakes() -> FakeAnalyzers:
    fakes = FakeAnalyzers()
    fakes.di = di_result(routes=[di_route("list_items", "/items", file="backend/service_backend/main.py", line=1)])
    return fakes


def count_calls(monkeypatch: pytest.MonkeyPatch, module, name: str) -> list[str]:
    """Record the ``rel`` argument of every call to ``module.name``
    (``read_doc_facts(path, rel)`` or ``code_words_from_bytes(rel, data)``)."""
    calls: list[str] = []
    real = getattr(module, name)
    position = 1 if name == "read_doc_facts" else 0

    def wrapper(*args, **kwargs):
        calls.append(args[position])
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return calls


def test_get_doc_report_runs_on_the_injected_analyzers(docs: Path) -> None:
    fakes = doc_fakes()
    fakes.py_symbols = [
        {"name": "app", "kind": "variable", "container": None, "file": "backend/service_backend/main.py", "line": 1}
    ]
    rt = make_runtime(docs, fakes)
    report = rt.get_doc_report().report
    assert set(report.docs) == {"docs/README.md", "docs/a.md"}
    assert report.findings() == []
    assert report.complete and report.limitations == []
    assert [(r.kind, r.status) for r in report.docs["docs/a.md"].references] == [("path", "ok"), ("route", "ok")]
    assert [s.name for s in report.symbols] == ["app"]
    assert fakes.calls == {
        "make_doc_index": 1,
        "list_repo_files": 1,
        "build_backend_model": 1,
        "build_di": 1,
        "make_facts_cache": 1,
        "make_symbol_index": 1,
        "python_symbols": 1,
        "typescript_decls": 1,
        "make_pages": 1,
    }


def test_the_page_provider_is_kept_until_the_pages_table_changes(docs: Path) -> None:
    fakes = doc_fakes()
    rt = make_runtime(docs, fakes)
    rt.get_doc_report()
    rt.get_doc_report()
    assert fakes.calls["make_pages"] == 1
    write_config(docs, config_toml() + '[frontend.pages]\nkind = "none"\npublic_dir = "frontend/public"\n', 2000)
    rt.get_doc_report()
    assert fakes.calls["make_pages"] == 2
    rt.reset()
    rt.get_doc_report()
    assert fakes.calls["make_pages"] == 3


def test_the_di_cache_is_shared_with_the_di_tools(docs: Path) -> None:
    fakes = doc_fakes()
    rt = make_runtime(docs, fakes)
    rt.get_di()
    rt.get_doc_report()
    rt.get_doc_report()
    assert (fakes.calls["build_backend_model"], fakes.calls["build_di"]) == (1, 1)
    assert fakes.calls["make_doc_index"] == 1


def test_docs_are_parsed_once_until_one_is_edited(docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_intel import doc_refs

    parsed = count_calls(monkeypatch, doc_refs, "read_doc_facts")
    rt = make_runtime(docs, doc_fakes())
    rt.get_doc_report()
    assert sorted(parsed) == ["docs/README.md", "docs/a.md"]
    rt.get_doc_report()
    assert len(parsed) == 2
    write(docs / "docs" / "a.md", "# A\n\nEdited.\n", 2000)
    rt.get_doc_report()
    assert parsed[2:] == ["docs/a.md"]


def test_code_words_are_read_once_until_a_code_file_is_edited(docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_intel import doc_refs

    write(docs / "docs" / "a.md", "# A\n\nUses `_private_helper()`.\n")
    read = count_calls(monkeypatch, doc_refs, "code_words_from_bytes")
    rt = make_runtime(docs, doc_fakes())
    first = rt.get_doc_report().report
    assert [f.message for f in first.findings()] == [
        "symbol not found: _private_helper() (no definition in the symbol index and no use in tracked code)"
    ]
    words_read = len(read)
    assert "backend/service_backend/main.py" in read
    rt.get_doc_report()
    assert len(read) == words_read
    write(docs / "backend" / "service_backend" / "main.py", "app = _private_helper()\n", 2000)
    assert rt.get_doc_report().report.findings() == []
    assert read[words_read:] == ["backend/service_backend/main.py"]


def test_a_doc_edited_inside_the_racy_window_is_checked_by_content(docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_intel import doc_refs

    parsed = count_calls(monkeypatch, doc_refs, "read_doc_facts")
    # The clock sits on the fixture mtime, so every stamp is racy.
    rt = make_runtime(docs, doc_fakes(), clock=Clock(1000 * 1_000_000_000))
    rt.get_doc_report()
    rt.get_doc_report()
    assert len(parsed) == 2, "unchanged content is confirmed by its digest, not parsed again"
    a_md = docs / "docs" / "a.md"
    same_size = a_md.read_text(encoding="utf-8").replace("# A", "# B")
    write(a_md, same_size, 1000)
    rt.get_doc_report()
    assert parsed[2:] == ["docs/a.md"], "same stamp, new content: parsed again"


def test_read_failures_are_never_cached(docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_intel import code_paths

    real = code_paths.read_file_bytes
    failing = {"docs/a.md"}

    def read(path: Path) -> bytes:
        if path.relative_to(docs).as_posix() in failing:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(code_paths, "read_file_bytes", read)
    rt = make_runtime(docs, doc_fakes())
    report = rt.get_doc_report().report
    assert [(p.kind, p.file) for p in report.diagnostics] == [("read_failed", "docs/a.md")]
    failing.clear()
    assert rt.get_doc_report().report.complete


def test_reset_drops_the_doc_caches(docs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from code_intel import doc_refs

    parsed = count_calls(monkeypatch, doc_refs, "read_doc_facts")
    words = count_calls(monkeypatch, doc_refs, "code_words_from_bytes")
    write(docs / "docs" / "a.md", "# A\n\nUses `_private_helper()`.\n")
    fakes = doc_fakes()
    rt = make_runtime(docs, fakes)
    rt.get_doc_report()
    first_parsed, first_words = len(parsed), len(words)
    rt.reset()
    rt.get_doc_report()
    assert (len(parsed), len(words)) == (2 * first_parsed, 2 * first_words)
    assert fakes.calls["make_doc_index"] == 2


def test_a_git_failure_is_an_analysis_failure_naming_the_step(docs: Path) -> None:
    from code_intel.repo_files import RepoFilesError

    fakes = doc_fakes()
    fakes.raise_in["list_repo_files"] = RepoFilesError(("git", "ls-files", "-z"), "fatal: not a git repository", 128)
    rt = make_runtime(docs, fakes)
    with pytest.raises(AnalysisFailed) as caught:
        rt.get_doc_report()
    exc = caught.value
    assert (exc.analyzer, exc.step, exc.missing) == ("doc_refs", "list repository files", None)
    assert "fatal: not a git repository" in exc.message
    assert "safe.directory" in exc.message


def test_an_invalid_config_names_its_file_and_key(docs: Path) -> None:
    write_config(docs, config_toml() + "bogus = 1\n", 2000)
    rt = make_runtime(docs, doc_fakes())
    with pytest.raises(AnalysisFailed) as caught:
        rt.get_doc_report()
    exc = caught.value
    assert (exc.analyzer, exc.step, exc.missing) == ("doc_refs", "read config", None)
    assert ".code-intel/config.toml" in exc.message and "bogus" in exc.message


def test_a_missing_config_names_the_missing_file(docs: Path) -> None:
    (docs / ".code-intel" / "config.toml").unlink()
    rt = make_runtime(docs, doc_fakes())
    with pytest.raises(AnalysisFailed) as caught:
        rt.get_doc_report()
    assert (caught.value.step, caught.value.missing) == ("read config", ".code-intel/config.toml")


def test_a_config_without_docs_is_an_analysis_failure_of_the_docs_only(docs: Path) -> None:
    from mcp_fixtures import LAYOUT

    write_config(docs, LAYOUT, 2000)
    fakes = doc_fakes()
    rt = make_runtime(docs, fakes)
    with pytest.raises(AnalysisFailed) as caught:
        rt.get_doc_report()
    assert (caught.value.analyzer, caught.value.step) == ("doc_refs", "read docs config")
    assert "missing required table [docs]" in caught.value.message
    assert "build_di" not in fakes.calls, "fails before the backend analysis runs"
    assert rt.get_di()["complete"] is True


def test_a_missing_toolchain_is_a_limitation_of_the_report_only(docs: Path) -> None:
    from code_intel.ts_bridge import ToolchainProblem

    fakes = doc_fakes()
    fakes.ts_error = ToolchainProblem(
        "typescript_not_found", "typescript not found", "ts-cache/node_modules", "code-intel setup"
    )
    rt = make_runtime(docs, fakes)
    found = rt.get_doc_report()
    report = found.report
    assert report.complete
    assert [p.kind for p in report.limitations] == ["frontend_definitions_unavailable"]
    assert [(p.kind, p.problem, p.fix) for p in found.frontend_problems] == [
        ("frontend_unavailable", "typescript_not_found", "code-intel setup")
    ]
    assert "facts" not in rt._helper_failures, "a missing toolchain is not a helper crash"


def test_a_facts_helper_crash_is_remembered_until_a_run_succeeds(docs: Path) -> None:
    fakes = doc_fakes()
    fakes.ts_error = ts_bridge.HelperError("facts", "exit", "node exited with code 1: boom")
    rt = make_runtime(docs, fakes)
    crashed = rt.get_doc_report()
    assert crashed.report.complete
    assert [p.kind for p in crashed.report.limitations] == ["frontend_definitions_unavailable"]
    assert rt._helper_failures["facts"].code == "helper_failed"
    assert rt.status().toolchain["state"] == "helper_failed"
    assert [p.problem for p in crashed.frontend_problems] == ["helper_failed"]

    fakes.ts_error = None
    recovered = rt.get_doc_report()
    assert recovered.report.limitations == []
    assert "facts" not in rt._helper_failures
    assert recovered.frontend_problems == []
    # Each result keeps its own run's problems: nothing shared is overwritten.
    assert [p.problem for p in crashed.frontend_problems] == ["helper_failed"]


def test_typescript_facts_problems_stay_out_of_the_check(docs: Path) -> None:
    fakes = doc_fakes()
    fakes.ts_problems = [
        problems.diagnostic(
            "typescript_syntax_error",
            stage="symbol_resolve",
            step="extract facts",
            file="frontend/lib/api.ts",
            message="frontend/lib/api.ts: 1 syntax error",
        )
    ]
    found = make_runtime(docs, fakes).get_doc_report()
    assert found.report.complete
    assert [p.kind for p in found.frontend_problems] == ["typescript_syntax_error"]


def test_tracked_di_and_symbol_problems_make_the_report_incomplete(docs: Path) -> None:
    """Problems of an untracked file are dropped, DI ones included: CI's
    checkout has no such file, so they would make the local report differ."""
    fakes = doc_fakes()
    untracked = "backend/service_backend/generated_pb2.py"
    fakes.di = di_result(
        complete=False, diagnostics=[read_failed("backend/service_backend/main.py"), read_failed(untracked)]
    )
    write(docs / untracked, "x = (\n")
    fakes.repo_files = repo_files_on_disk(docs, untracked=[untracked])
    fakes.py_symbol_problems = [
        problems.diagnostic("parse_failed", stage="symbol_resolve", step="parse file", file=untracked, message="bad"),
        problems.diagnostic(
            "parse_failed", stage="symbol_resolve", step="parse file", file="frontend/lib/api.ts", message="tracked"
        ),
    ]
    report = make_runtime(docs, fakes).get_doc_report().report
    assert not report.complete
    assert sorted((p.kind, p.file) for p in report.diagnostics) == [
        ("parse_failed", "frontend/lib/api.ts"),
        ("read_failed", "backend/service_backend/main.py"),
    ]


def test_problems_not_tied_to_an_untracked_path_are_kept(docs: Path) -> None:
    """Only a problem whose ``file`` is a real untracked or ignored path is
    dropped: `unresolved_app` names a module, not a file, and a lost app hides
    every route, so it must still make the report incomplete."""
    fakes = doc_fakes()
    ignored = "backend/service_backend/ignored_pb2.py"
    fakes.repo_files = repo_files_on_disk(docs)
    # Written after the listing: on disk, but git does not list it (ignored).
    write(docs / ignored, "x = (\n")
    fakes.di = di_result(routes=[], complete=False, diagnostics=[unresolved_app(), read_failed(ignored)])
    fakes.py_symbol_problems = [
        problems.diagnostic("parse_failed", stage="symbol_resolve", step="parse file", file=None, message="no file"),
        problems.diagnostic("parse_failed", stage="symbol_resolve", step="parse file", file="../x.py", message="out"),
    ]
    report = make_runtime(docs, fakes).get_doc_report().report
    assert not report.complete
    assert sorted((p.kind, p.file or "") for p in report.diagnostics) == [
        ("parse_failed", ""),
        ("parse_failed", "../x.py"),
        ("unresolved_app", "service_backend.main"),
    ]


def _gap(kind: str, file: str = "backend/service_backend/main.py") -> LimitationDict:
    return problems.limitation(kind, stage="router_graph", file=file, line=1, message=kind).as_limitation()


def test_only_tracked_route_gaps_reach_the_report_and_its_route_hint(docs: Path) -> None:
    """A "route not found" points at the limitations only when one of them
    can hide a live route: a DI limitation of another kind, or one in an
    untracked file, says nothing about the route."""
    write(docs / "docs" / "a.md", "# A\n\nRemove one with `DELETE /items/{item_id}`.\n")
    untracked = "backend/service_backend/scratch.py"
    write(docs / untracked, "x = 1\n")
    fakes = doc_fakes()
    fakes.repo_files = repo_files_on_disk(docs, untracked=[untracked])
    fakes.di["analysis_limitations"] = [_gap("unresolved_dependency"), _gap("opaque_router_call", untracked)]
    quiet = make_runtime(docs, fakes).get_doc_report().report
    assert quiet.route_limitations == ()
    assert [f.message for f in quiet.findings()] == [
        "route not found: DELETE /items/{item_id} (not in the analyzed backend routes)"
    ]

    fakes.di["analysis_limitations"] = [_gap("unresolved_dependency"), _gap("opaque_router_call")]
    hinted = make_runtime(docs, fakes).get_doc_report().report
    assert [row["kind"] for row in hinted.route_limitations] == ["opaque_router_call"]
    assert [f.message for f in hinted.findings()] == [
        "route not found: DELETE /items/{item_id} (not in the analyzed backend routes; "
        "see the limitation below if the route is mounted by code the analysis cannot see)"
    ]


def test_the_runtime_keeps_no_frontend_state_between_doc_reports(docs: Path) -> None:
    rt = make_runtime(docs, doc_fakes())
    rt.get_doc_report()
    assert not hasattr(rt, "doc_frontend_problems")
    assert not hasattr(rt, "_doc_frontend")


# ── Config edits, pin files and git-visible walks ────────────────────────


def test_a_config_edit_changes_the_next_answer_without_a_restart(repo: Path, fakes: FakeAnalyzers) -> None:
    """The config is part of every fingerprint by content: an edit rebuilds
    with the new settings; rewriting the same bytes rebuilds nothing."""
    rt = make_runtime(repo, fakes)
    rt.get_di()
    rt.get_routes()
    assert [b.app for b in fakes.backends] == ["service_backend.main:app"]

    same = (repo / ".code-intel" / "config.toml").read_text(encoding="utf-8")
    write_config(repo, same, 5000)
    rt.get_di()
    rt.get_routes()
    assert (fakes.calls["build_backend_model"], fakes.calls["scan_frontend"]) == (1, 1), "same bytes, same caches"

    write_config(repo, same.replace("service_backend.main:app", "service_backend.main:application"), 6000)
    rt.get_di()
    assert [b.app for b in fakes.backends] == ["service_backend.main:app", "service_backend.main:application"]
    rt.get_routes()
    assert fakes.calls["scan_frontend"] == 2, "the routes scan follows the config too"
    assert rt.status().complete is True


def test_a_config_edit_reaches_the_frontend_and_the_symbol_layout(repo: Path, fakes: FakeAnalyzers) -> None:
    write(repo / "web" / "lib" / "api.ts", "export {};\n", 1000)
    rt = make_runtime(repo, fakes)
    rt.get_routes()
    rt.resolve_symbol("X", side="backend", kind=None, file_hint=None)
    assert fakes.scan_files == ["frontend/components/Page.tsx", "frontend/lib/api.ts"]

    text = (repo / ".code-intel" / "config.toml").read_text(encoding="utf-8")
    write_config(repo, text.replace('root = "frontend"', 'root = "web"'), 6000)
    rt.get_routes()
    rt.resolve_symbol("X", side="backend", kind=None, file_hint=None)
    assert fakes.scan_files == ["web/lib/api.ts"]
    assert [c.frontend.root for c in fakes.symbol_configs] == ["frontend", "web"], "a new layout, a new index"
    assert fakes.calls["make_facts_cache"] == 1, "the facts cache does not depend on the config"


def test_a_broken_config_is_reported_by_status_and_refused_by_the_analyzers(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    write_config(repo, "[backend]\n", 6000)
    with pytest.raises(AnalysisFailed) as caught:
        rt.get_di()
    assert (caught.value.analyzer, caught.value.step) == ("di_map", "read config")
    snapshot = rt.status()
    assert snapshot.config["path"] == ".code-intel/config.toml"
    assert ".code-intel/config.toml" in snapshot.config["error"]
    assert snapshot.runtime_compatible is None and snapshot.python_required is None
    assert snapshot.cache["di"] == {"loaded": False, "fresh": False}, "nothing can be compared without a config"


def test_the_backend_floor_follows_the_version_file(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes, version_info=(3, 14))
    assert rt.runtime_compatible() is True
    write(repo / ".python-version", "3.15\n", 2000)
    assert rt.runtime_compatible() is False
    assert rt.runtime_versions() == ("3.14", "3.15")


def test_a_pin_bump_after_start_marks_the_code_stale(tmp_path: Path) -> None:
    """The pin file is never loaded as code: its content at start is the
    reference, and only a content change (or its appearing) is stale."""
    pin = write(tmp_path / ".code-intel" / "requirements.txt", "code-intel @ git+https://example.invalid/x@aaa\n")
    missing = tmp_path / "other" / "requirements.txt"
    watch = analysis_runtime.CodeWatch([], pins=[pin, missing], clock=Clock(), imported={})
    assert watch.state() == analysis_runtime.CodeState()
    os.utime(pin, (2000, 2000))
    assert watch.stale() is False, "a touch is not a bump"
    write(pin, "code-intel @ git+https://example.invalid/x@bbb\n", 3000)
    assert watch.state().changed == (pin,)
    write(pin, "code-intel @ git+https://example.invalid/x@aaa\n", 4000)
    assert watch.stale() is False, "back to the pinned bytes"
    write(missing, "x\n", 5000)
    assert watch.state().changed == (missing,)


def test_the_runtime_watches_the_pin_file(repo: Path, fakes: FakeAnalyzers) -> None:
    pin = write(repo / ".code-intel" / "requirements.txt", "code-intel @ git+https://example.invalid/x@aaa\n")
    rt = make_runtime(repo, fakes, pin_files=[pin])
    assert rt.code_state().stale is False
    write(pin, "code-intel @ git+https://example.invalid/x@bbb\n", 3000)
    assert rt.code_state().changed == (pin,)
    assert rt.status().code_stale is True


@pytest.mark.usefixtures("module_git")
def test_ignored_files_are_never_fingerprinted_or_read(repo: Path, fakes: FakeAnalyzers) -> None:
    """Every walk is limited to the git-visible files, so ignored build output
    neither reaches the analyzers nor makes a cache stale when it changes."""
    git_init(repo)
    write(repo / ".gitignore", "/backend/build/\n/frontend/gen/\n")
    write(repo / "backend" / "build" / "service_backend" / "copy.py", "x = 1\n")
    ignored_py = write(repo / "backend" / "service_backend" / "generated.py", "x = 1\n")
    write(repo / ".git" / "info" / "exclude", "backend/service_backend/generated.py\n")
    write(repo / "frontend" / "gen" / "client.ts", "export {};\n")
    real = analysis_runtime.Analyzers.real().list_repo_files
    rt = make_runtime(repo, fakes)
    rt.analyzers = dataclasses.replace(rt.analyzers, list_repo_files=real)

    rt.get_routes()
    assert "backend/service_backend/main.py" in fakes.visible[-1]
    assert not {"backend/service_backend/generated.py", "frontend/gen/client.ts"} & fakes.visible[-1]
    assert fakes.scan_files == ["frontend/components/Page.tsx", "frontend/lib/api.ts"]

    write(ignored_py, "x = 2\n", 3000)
    write(repo / "frontend" / "gen" / "client.ts", "export const x = 2;\n", 3000)
    rt.get_routes()
    rt.get_di()
    assert (fakes.calls["build_backend_model"], fakes.calls["scan_frontend"]) == (1, 1)
    assert rt.status().complete is True


def test_a_new_untracked_file_is_visible_on_the_next_call(repo: Path, fakes: FakeAnalyzers) -> None:
    rt = make_runtime(repo, fakes)
    rt.get_di()
    write(repo / "backend" / "service_backend" / "new.py", "x = 1\n", 1000)
    rt.get_di()
    assert "backend/service_backend/new.py" in fakes.visible[-1]
    assert fakes.calls["build_backend_model"] == 2


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ('kind = "none"\npublic_dir = "frontend/public"', ("NoPages", ["/robots.txt"], [])),
        ('kind = "next-app"\npublic_dir = "frontend/public"', ("NextAppPages", ["/x", "/robots.txt"], [])),
        (
            'kind = "react-router"\nmodules = ["frontend/App.tsx"]\npublic_dir = "frontend/public"',
            ("ReactRouterPages", ["/robots.txt"], ["read_failed"]),
        ),
    ],
)
def test_the_page_provider_follows_the_pages_kind(tmp_path: Path, pages: str, expected: tuple) -> None:
    write_config(tmp_path, config_toml() + f"[frontend.pages]\n{pages}\n")
    config = load_config(tmp_path)
    toolchain = ts_bridge.find_toolchain(tmp_path)
    provider = analysis_runtime.Analyzers.real().make_pages(tmp_path, config, lambda: toolchain, time.time_ns)
    found = provider.pages(frozenset({"frontend/app/x/page.tsx", "frontend/public/robots.txt"}))
    name, displays, kinds = expected
    assert type(provider).__name__ == name
    assert found.available
    assert [page.display for page in found.pages] == displays
    assert [p.kind for p in found.problems] == kinds
