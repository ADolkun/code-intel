"""Tests for the MCP tools in `server`: argument validation, error bodies,
the response envelope and each tool's output, against a runtime with fake
analyzers (`mcp_fixtures`)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fixture_repo import git_init, hermetic_git
from mcp import Client
from mcp.types import CallToolResult, TextContent
from mcp_fixtures import (
    DOC_LOOKUP_ROUTE_FILE,
    DOC_LOOKUP_SYMBOLS,
    E2E_LAYOUT,
    TS_FIX,
    TS_MISSING,
    FakeAnalyzers,
    FakeCandidate,
    FakeSymbolResult,
    FakeUsage,
    backend_side,
    config_toml,
    dep,
    di_result,
    di_route,
    doc_lookup_repo,
    doc_lookup_route,
    frontend_side,
    limitation,
    make_repo,
    make_runtime,
    pair,
    read_failed,
    routes_result,
    unresolved_app,
    use,
    write,
    write_config,
)

from code_intel import analysis_runtime, loaded_code, presenters, problems, ts_bridge
from code_intel import server as mcp_server
from code_intel.config import ConfigFile
from code_intel.doc_rules import FrontendPage, PageSet
from code_intel.protocol_match import ProtocolMatch
from code_intel.result_types import DiResult

# The runtime's timeline is fixture mtimes and an injected clock; the kernel's
# ctime would put every file in the clock's future.
pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")

PACKAGE_DIR = loaded_code.PACKAGE_DIR

GET_DB = "service_backend.core.database:get_db"
PERM = "service_backend.core.auth:require_permission"


def _server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **runtime_kwargs) -> SimpleNamespace:
    fakes = FakeAnalyzers()
    repo = make_repo(tmp_path / "repo")
    rt = make_runtime(repo, fakes, **runtime_kwargs)
    monkeypatch.setattr(mcp_server, "runtime", rt)
    return SimpleNamespace(fakes=fakes, repo=repo, rt=rt)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return _server(tmp_path, monkeypatch)


def j(text: str) -> dict:
    return json.loads(text)


# ── Server wiring ────────────────────────────────────────────────────────


def test_configure_watches_the_package_and_the_pin_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setattr(mcp_server, "runtime", None)
    rt = mcp_server.configure(repo, ConfigFile.locate(repo))
    assert mcp_server.runtime is rt
    assert rt.code_watch.modules == tuple(analysis_runtime.watched_files())
    assert rt.code_watch.pins == (repo / ".code-intel" / "requirements.txt",)
    assert rt.config().backend.package == "service_backend"


def test_a_server_without_a_repository_says_how_to_start_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "runtime", None)
    with pytest.raises(RuntimeError, match="code-intel mcp"):
        mcp_server.status()


def test_route_summary(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result([pair("/a", "a", "fa")], backend_only=[backend_side("GET", "/b", "b")])
    text = mcp_server.route_match()
    assert "Backend routes:  2" in text
    assert "Matched routes:  1" in text
    assert "Coverage:        50.0%" in text


def test_tools_run_the_runtime_s_injected_analyzers(server: SimpleNamespace) -> None:
    mcp_server.di_deps()
    mcp_server.route_match()
    assert server.fakes.calls["build_backend_model"] == 1
    assert server.fakes.calls["build_di"] == 1
    assert server.fakes.calls["build_routes"] == 1


def test_code_watch_covers_every_module_the_server_imports() -> None:
    """Every package module loaded by the server is watched, so editing any of
    them raises the restart warning, and each has an exact record."""
    watched = {p.resolve() for p in analysis_runtime.watched_files()}
    probe = (
        "import sys, pathlib; from code_intel import server, loaded_code; "
        "d = loaded_code.PACKAGE_DIR; "
        "loaded = sorted(str(pathlib.Path(m.__file__).resolve()) for m in list(sys.modules.values()) "
        "if getattr(m, '__file__', None) and pathlib.Path(m.__file__).resolve().is_relative_to(d)); "
        "print('\\n'.join(loaded)); print('--'); print('\\n'.join(sorted(str(p) for p in loaded_code.IMPORTED)))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120)
    loaded_text, recorded_text = proc.stdout.split("--\n")
    loaded = {Path(line) for line in loaded_text.split()}
    recorded = {Path(line) for line in recorded_text.split()}
    assert PACKAGE_DIR / "server.py" in loaded
    assert loaded <= watched
    assert loaded <= recorded, "every loaded module has the digest of the bytes it was compiled from"
    assert {p for p in PACKAGE_DIR.rglob("*.py")} <= watched


def _copy_package(tmp_path: Path) -> Path:
    """A fresh copy of the package, importable from the returned directory, so a probe can edit it."""
    site = tmp_path / "site"
    shutil.copytree(PACKAGE_DIR, site / "code_intel", ignore=shutil.ignore_patterns("__pycache__"))
    return site


def _run_probe(script: str, site: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", script, str(site), *args],
        capture_output=True,
        text=True,
        cwd=str(site.parent),
        check=True,
        timeout=120,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


_PROBE_PRELUDE = """
import importlib, json, sys
from pathlib import Path

site = Path(sys.argv[1])
sys.path.insert(0, str(site))
package = site / "code_intel"
from code_intel import loaded_code, server
from code_intel.config import ConfigFile
assert loaded_code.PACKAGE_DIR == package.resolve(), loaded_code.PACKAGE_DIR
repo = site.parent / "repo"
repo.mkdir(exist_ok=True)
runtime = server.configure(repo, ConfigFile.locate(repo))

def state():
    s = runtime.code_state()
    return [s.stale, [p.name for p in s.changed], [p.name for p in s.unconfirmed]]

def recorded(name):
    return (package / name).resolve() in loaded_code.IMPORTED
"""

_IMPORT_FAILURE_PROBE = (
    _PROBE_PRELUDE
    + """
out = {}
target = package / "di_map.py"
good = target.read_bytes()
assert "code_intel.di_map" not in sys.modules, "di_map must load lazily for this probe"
for label, bad in (("syntax", b"def (\\n"), ("runtime", good + b"\\nraise RuntimeError('boom')\\n")):
    target.write_bytes(bad)
    try:
        importlib.import_module("code_intel.di_map")
    except (SyntaxError, RuntimeError) as exc:
        out[label + "_error"] = type(exc).__name__
    out[label + "_recorded"] = recorded("di_map.py")
    target.write_bytes(good)
    out[label + "_fixed"] = state()
importlib.import_module("code_intel.di_map")
out["retried"] = state()
out["retried_recorded"] = recorded("di_map.py")

queries_py = package / "queries.py"
original = queries_py.read_bytes()
queries_py.write_bytes(original + b"\\nRELOADED = 1\\n")
out["edited"] = state()
from code_intel import queries
importlib.reload(queries)
out["reloaded"] = state()
queries_py.write_bytes(original + b"\\nraise RuntimeError('half')\\n")
try:
    importlib.reload(queries)
except RuntimeError:
    pass
queries_py.write_bytes(original)
out["half_reloaded"] = state()
importlib.reload(queries)
out["reloaded_again"] = state()
print(json.dumps(out))
"""
)


def test_a_failed_import_or_reload_leaves_no_false_record(tmp_path: Path) -> None:
    """A lazy import that fails leaves nothing loaded, so neither it nor the
    fixed file is stale, and the retry is recorded; a reload replaces the
    record, and a reload that raised while running is stale until one succeeds."""
    out = _run_probe(_IMPORT_FAILURE_PROBE, _copy_package(tmp_path))
    assert (out["syntax_error"], out["runtime_error"]) == ("SyntaxError", "RuntimeError")
    assert (out["syntax_recorded"], out["runtime_recorded"]) == (False, False)
    assert out["syntax_fixed"] == [False, [], []]
    assert out["runtime_fixed"] == [False, [], []]
    assert out["retried"] == [False, [], []]
    assert out["retried_recorded"] is True
    assert out["edited"] == [True, ["queries.py"], []]
    assert out["reloaded"] == [False, [], []]
    assert out["half_reloaded"] == [True, ["queries.py"], []]
    assert out["reloaded_again"] == [False, [], []]


_FRESH_CHECKOUT_PROBE = (
    _PROBE_PRELUDE
    + """
edit = sys.argv[2]
loaded = sorted(
    Path(m.__file__).name for m in list(sys.modules.values())
    if getattr(m, "__file__", None) and Path(m.__file__).resolve().is_relative_to(package.resolve())
)
before = runtime.code_state()
if edit:
    target = package / edit
    target.write_text(target.read_text(encoding="utf-8") + "\\n# edited\\n", encoding="utf-8")
after = runtime.code_state()
print(json.dumps({
    "recorded": sorted(p.name for p in loaded_code.IMPORTED),
    "loaded": loaded,
    "before": [before.stale, [p.name for p in before.unconfirmed]],
    "after": [after.stale, [p.name for p in after.changed]],
}))
"""
)


@pytest.mark.parametrize("edit", ["queries.py", "__init__.py", "loaded_code.py"])
def test_a_server_started_right_after_a_checkout_is_not_stale(tmp_path: Path, edit: str) -> None:
    """A checkout rewrites every module moments before the server starts. The
    loaded bytes are recorded at import (the package's ``__init__`` and the
    recorder itself by comparing their code), so the watch is current, not
    stale or unconfirmed; a later edit to any of them is stale."""
    site = _copy_package(tmp_path)
    clean = _run_probe(_FRESH_CHECKOUT_PROBE, site, "")
    assert clean["before"] == [False, []]
    assert set(clean["loaded"]) <= set(clean["recorded"])
    assert {"server.py", "__init__.py", "loaded_code.py"} <= set(clean["recorded"])
    edited = _run_probe(_FRESH_CHECKOUT_PROBE, site, edit)
    assert edited["after"] == [True, [edit]]


def test_mcp_tools_return_text_only_without_structured_output(server: SimpleNamespace) -> None:
    """The SDK auto-wraps a `-> str` tool into `structuredContent: {"result": ...}`
    plus an `outputSchema`, so clients show every result twice."""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert tools
    assert [t.name for t in tools if t.output_schema is not None] == []
    result = asyncio.run(mcp_server.mcp.call_tool("jsx_usage", {"component_name": "X", "root": "../.."}))
    assert isinstance(result, CallToolResult)
    assert [c.type for c in result.content] == ["text"]
    assert result.structured_content is None


def _text(result: CallToolResult) -> str:
    """The reply's one text block."""
    (item,) = result.content
    assert isinstance(item, TextContent), item
    return item.text


def _call_over_the_wire(tool: str, arguments: dict) -> CallToolResult:
    """One tool call through a connected in-memory client, as a real client sees it."""

    async def call() -> CallToolResult:
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(call())


def test_error_bodies_reach_the_client_with_is_error_and_the_full_body(server: SimpleNamespace) -> None:
    """A client that only checks isError still sees the failure, and the text
    keeps the error body, JSON included."""
    error = _call_over_the_wire("jsx_usage", {"component_name": "X", "root": "../..", "output_format": "json"})
    assert error.is_error is True
    assert [c.type for c in error.content] == ["text"]
    payload = j(_text(error))
    assert (payload["error"], payload["root"]) == ("invalid_root", "../..")
    assert error.structured_content is None

    ok = _call_over_the_wire("route_match", {})
    assert ok.is_error is False
    assert "Route matching summary:" in _text(ok)


def test_direct_calls_still_return_error_text(server: SimpleNamespace) -> None:
    text = mcp_server.jsx_usage("X", root="../..")
    assert isinstance(text, mcp_server.ErrorText)
    assert text.startswith("root '../..' resolves to")
    assert not isinstance(mcp_server.route_match(), mcp_server.ErrorText)


def test_tool_schemas_advertise_enums_bounds_and_defaults() -> None:
    """The schema names every allowed value, the limit bound and the defaults."""
    tools = {t.name: t.input_schema["properties"] for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert tools["di_deps"]["match"]["enum"] == ["exact", "suffix", "contains", "auto"]
    assert tools["route_unmatched"]["side"]["enum"] == ["backend", "frontend", "both"]
    assert tools["status"]["output_format"]["enum"] == ["text", "json"]
    method = tools["di_deps"]["method"]["anyOf"][0]["enum"]
    assert method == ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "WS"]
    kinds = tools["symbol_resolve"]["kind"]["anyOf"][0]["enum"]
    assert "namespace" in kinds and "none" in kinds
    assert tools["di_reverse"]["limit"]["anyOf"][0]["minimum"] == 0
    assert tools["di_reverse"]["dep_function"]["minLength"] == 1
    for name in (
        "di_reverse",
        "route_match",
        "route_unmatched",
        "jsx_usage",
        "symbol_resolve",
        "doc_refs",
    ):
        assert tools[name]["limit"]["default"] == 50, name
    assert tools["doc_refs"]["limit"]["anyOf"][0]["minimum"] == 0
    assert tools["di_deps"]["limit"]["default"] is None


@pytest.mark.parametrize(
    ("tool", "args", "argument"),
    [
        ("di_deps", {"match": "bogus"}, "match"),
        ("di_deps", {"handler": "h", "method": "get"}, "method"),
        ("di_reverse", {"dep_function": ""}, "dep_function"),
        ("route_match", {"limit": -1}, "limit"),
        ("doc_refs", {"doc": "docs/a.md", "limit": -1}, "limit"),
        ("route_unmatched", {"side": "sideways"}, "side"),
        ("status", {"output_format": "xml"}, "output_format"),
        ("jsx_usage", {"component_name": ""}, "component_name"),
        ("symbol_resolve", {"name": "X", "kind": "struct"}, "kind"),
        ("refresh", {"output_format": "yaml"}, "output_format"),
    ],
)
def test_schema_violations_reach_the_client_as_the_invalid_argument_body(
    server: SimpleNamespace, tool: str, args: dict, argument: str
) -> None:
    """A value the schema refuses gets the documented error body, in JSON when
    asked for, with isError and before any analysis runs."""
    as_json = {**args, "output_format": "json"} if argument != "output_format" else args
    result = _call_over_the_wire(tool, as_json)
    assert result.is_error is True
    text = _text(result)
    if argument == "output_format":
        assert text.startswith(f"Invalid output_format {args['output_format']!r}: ")
        assert text.rstrip().endswith("Allowed: 'text', 'json'.")
    else:
        payload = j(text)
        assert (payload["error"], payload["argument"], payload["value"]) == (
            "invalid_argument",
            argument,
            args[argument],
        )
        assert payload["schema_version"] == 2
    assert server.fakes.calls == {}, "no analysis may run for an invalid argument"


def test_schema_refusals_list_the_allowed_values_and_missing_arguments(server: SimpleNamespace) -> None:
    payload = j(_text(_call_over_the_wire("di_deps", {"match": "bogus", "output_format": "json"})))
    assert payload["allowed"] == ["exact", "suffix", "contains", "auto"]
    optional = j(_text(_call_over_the_wire("di_deps", {"method": "get", "output_format": "json"})))
    assert "POST" in optional["allowed"], "an optional Literal still lists its values"
    free = j(_text(_call_over_the_wire("route_match", {"limit": -1, "output_format": "json"})))
    assert "allowed" not in free and "greater than or equal to 0" in free["message"]
    missing = j(_text(_call_over_the_wire("jsx_usage", {"output_format": "json"})))
    assert (missing["argument"], missing["value"]) == ("component_name", None)
    assert server.fakes.calls == {}


# ── Validation before analysis ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        (lambda: mcp_server.route_match(path="/x", match=cast(Any, "bogus"), output_format="json"), "match"),
        (lambda: mcp_server.di_deps(match=cast(Any, "bogus"), output_format="json"), "match"),
        (lambda: mcp_server.route_unmatched(side=cast(Any, "sideways"), output_format="json"), "side"),
        (lambda: mcp_server.symbol_resolve("X", side=cast(Any, "sideways"), output_format="json"), "side"),
        (lambda: mcp_server.symbol_resolve("X", kind=cast(Any, "struct"), output_format="json"), "kind"),
        (lambda: mcp_server.di_reverse("get_db", limit=-1, output_format="json"), "limit"),
        (lambda: mcp_server.jsx_usage("Widget", limit=-1, output_format="json"), "limit"),
        (lambda: mcp_server.di_deps(handler="h", method=cast(Any, "get"), output_format="json"), "method"),
        (lambda: mcp_server.di_reverse("   ", output_format="json"), "dep_function"),
        (lambda: mcp_server.jsx_usage("", output_format="json"), "component_name"),
        (lambda: mcp_server.symbol_resolve(" \t", output_format="json"), "name"),
        (lambda: mcp_server.di_deps(handler=" ", output_format="json"), "handler"),
        (lambda: mcp_server.route_match(frontend_func="", output_format="json"), "frontend_func"),
        (lambda: mcp_server.symbol_resolve("X", file_hint="", output_format="json"), "file_hint"),
        (lambda: mcp_server.doc_refs(output_format="json"), "file"),
        (lambda: mcp_server.doc_refs(route="FETCH /x", output_format="json"), "route"),
    ],
)
def test_invalid_arguments_are_rejected_before_analysis(server: SimpleNamespace, call, argument: str) -> None:
    """Direct calls skip pydantic, so the tools validate in-function first."""
    payload = j(call())
    assert payload["error"] == "invalid_argument"
    assert payload["argument"] == argument
    assert payload["schema_version"] == 2
    assert server.fakes.calls == {}, "no analysis may run for an invalid argument"


def test_invalid_output_format_is_rejected_first_in_text(server: SimpleNamespace) -> None:
    out = mcp_server.di_deps(match=cast(Any, "bogus"), output_format=cast(Any, "xml"))
    assert out.startswith("Invalid output_format 'xml': unknown output format. Allowed: 'text', 'json'.")
    assert server.fakes.calls == {}


def test_method_without_handler_or_path_is_rejected(server: SimpleNamespace) -> None:
    payload = j(mcp_server.di_deps(method="GET", output_format="json"))
    assert (payload["error"], payload["argument"]) == ("invalid_argument", "method")
    assert payload["message"] == "Invalid method 'GET': method needs handler or path."


@pytest.mark.parametrize("root", ["../..", "/", "frontend/lib/api.ts", "missing-dir"])
def test_jsx_root_outside_the_repo_is_invalid_root(server: SimpleNamespace, root: str) -> None:
    payload = j(mcp_server.jsx_usage("Widget", root=root, output_format="json"))
    assert payload["error"] == "invalid_root"
    assert payload["root"] == root
    assert root in payload["message"]
    assert "find_jsx_usage" not in server.fakes.calls


# ── Failures become error bodies ──────────────────────────────────────────


def test_analysis_failures_return_analysis_failed_with_the_stale_banner(
    server: SimpleNamespace, tmp_path: Path
) -> None:
    server.fakes.raise_in["build_backend_model"] = RuntimeError("boom")
    stale_src = write(tmp_path / "watched.py", "A = 1\n")
    server.rt.code_watch = analysis_runtime.CodeWatch([stale_src])
    write(stale_src, "A = 2\n", 9000)

    payload = j(mcp_server.di_deps(output_format="json"))
    assert payload["error"] == "analysis_failed"
    assert (payload["analyzer"], payload["step"], payload["missing"]) == ("di_map", "build router graph", None)
    assert payload["message"].startswith("di_map failed at step 'build router graph': RuntimeError: boom")
    assert payload["analyzer_code_stale"] is True
    text = mcp_server.di_reverse("get_db")
    assert text.startswith(presenters.STALE_CODE_WARNING)
    assert "di_map failed at step 'build router graph'" in text


def test_unexpected_exceptions_are_internal_errors_naming_the_tool(server: SimpleNamespace, monkeypatch) -> None:
    monkeypatch.setattr(mcp_server.queries, "di_deps", lambda *a, **k: 1 / 0)
    payload = j(mcp_server.di_deps(output_format="json"))
    assert payload["error"] == "internal_error"
    assert payload["tool"] == "di_deps"
    assert "ZeroDivisionError" in payload["message"]


# ── Missing frontend toolchain ────────────────────────────────────────────


@pytest.fixture
def no_toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return _server(tmp_path, monkeypatch, toolchain_available=False)


def test_route_match_returns_the_backend_side_without_a_frontend(no_toolchain: SimpleNamespace) -> None:
    payload = j(mcp_server.route_match(output_format="json"))
    assert payload["complete"] is False
    assert [d["kind"] for d in payload["diagnostics"]] == ["frontend_unavailable"]
    text = mcp_server.route_match()
    assert text.startswith("⚠ INCOMPLETE — the frontend was not analyzed, so frontend-derived results are n/a.")
    assert "undercounts" not in text
    assert "Frontend:        not analyzed: ts-cache/node_modules/typescript/package.json not found" in text


@pytest.mark.parametrize("side", ["backend", "frontend", "both"])
def test_route_unmatched_needs_the_frontend_for_every_side(no_toolchain: SimpleNamespace, side: str) -> None:
    """Without the frontend every backend route would look unused."""
    payload = j(mcp_server.route_unmatched(side=cast(Any, side), output_format="json"))
    assert payload["error"] == "frontend_unavailable"
    assert payload["missing"] == "ts-cache/node_modules/typescript/package.json"
    assert payload["fix"].startswith('run "code-intel setup"')
    assert payload["problem"] == "typescript_not_found"


def test_jsx_and_frontend_symbols_return_frontend_unavailable(no_toolchain: SimpleNamespace) -> None:
    jsx = j(mcp_server.jsx_usage("Widget", output_format="json"))
    assert jsx["error"] == "frontend_unavailable"
    no_toolchain.fakes.symbols = ts_bridge.ToolchainProblem(
        "node_not_found", "node not found on PATH", "node", "install node"
    )
    sym = j(mcp_server.symbol_resolve("DataTable", side="frontend", output_format="json"))
    assert (sym["error"], sym["missing"], sym["problem"]) == ("frontend_unavailable", "node", "node_not_found")


def test_symbol_resolve_both_keeps_backend_candidates_without_a_frontend(server: SimpleNamespace) -> None:
    server.fakes.symbols = FakeSymbolResult(
        [
            FakeCandidate(
                "get_db", "backend/service_backend/core/database.py", 83, 90, "function", "def get_db()", "python"
            )
        ],
        complete=False,
        frontend_problem="node not found on PATH",
    )
    payload = j(mcp_server.symbol_resolve("get_db", output_format="json"))
    assert payload["complete"] is False
    assert payload["candidate_count"] == 1
    assert payload["frontend_problem"] == "node not found on PATH"


def test_symbol_resolve_json_carries_the_protocol_fields(server: SimpleNamespace) -> None:
    gateway = ProtocolMatch("Gateway", "service_backend.gateway:Gateway", "backend/service_backend/gateway.py", 7)
    server.fakes.symbols = FakeSymbolResult(
        [
            FakeCandidate(
                "fetch",
                "backend/service_backend/impl.py",
                3,
                4,
                "method",
                "def fetch(self):",
                "python",
                scope="class",
                container="Impl",
                protocols=[gateway],
            )
        ]
    )
    payload = j(mcp_server.symbol_resolve("fetch", side="backend", output_format="json"))
    [row] = payload["candidates"]
    assert row["protocol_method"] is False
    assert row["protocols"] == [
        {
            "name": "Gateway",
            "id": "service_backend.gateway:Gateway",
            "file": "backend/service_backend/gateway.py",
            "line": 7,
        }
    ]
    assert payload["schema_version"] == 2
    text = mcp_server.symbol_resolve("fetch", side="backend")
    assert "resolve to: Gateway.fetch (backend/service_backend/gateway.py:7)" in text


def test_di_tools_work_without_the_toolchain(no_toolchain: SimpleNamespace) -> None:
    payload = j(mcp_server.di_deps(output_format="json"))
    assert payload["complete"] is True


# ── Old interpreter ───────────────────────────────────────────────────────


def test_backend_tools_refuse_on_an_old_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every backend tool refuses before analysis; status stays available; frontend
    symbols and JSX still work."""
    s = _server(tmp_path, monkeypatch, version_info=(3, 12))
    for call in (
        lambda: mcp_server.route_match(output_format="json"),
        lambda: mcp_server.route_unmatched(output_format="json"),
        lambda: mcp_server.di_deps(output_format="json"),
        lambda: mcp_server.di_reverse("anything", output_format="json"),
        lambda: mcp_server.refresh(output_format="json"),
        lambda: mcp_server.symbol_resolve("get_db", output_format="json"),
        lambda: mcp_server.symbol_resolve("get_db", side="backend", output_format="json"),
    ):
        payload = j(call())
        assert payload["error"] == "runtime_incompatible"
        assert (payload["running"], payload["required"]) == ("3.12", "3.14")
    assert s.fakes.calls == {}

    s.fakes.symbols = FakeSymbolResult(
        [FakeCandidate("DataTable", "frontend/x.tsx", 1, 2, "component", "f", "typescript")]
    )
    assert j(mcp_server.symbol_resolve("DataTable", side="frontend", output_format="json"))["candidate_count"] == 1
    s.fakes.usages = [FakeUsage("frontend/a.tsx", 1)]
    assert j(mcp_server.jsx_usage("Widget", output_format="json"))["usage_count"] == 1

    status = j(mcp_server.status(load_counts=True, output_format="json"))
    assert status["runtime"]["runtime_compatible"] is False
    assert status["counts"] is None
    assert status["counts_error"]["error"] == "runtime_incompatible"
    assert "build_backend_model" not in s.fakes.calls, "status counts must not analyze on an old interpreter"


# ── DI tools ─────────────────────────────────────────────────────────────


def _dep_routes(n: int = 0) -> DiResult:
    routes = [
        di_route("d", "/d", deps=[use(PERM, "require_permission(A)", "direct")], all_deps=[PERM]),
        di_route("g", "/g", deps=[use(PERM, "require_permission(B)", "route decorator")], all_deps=[PERM]),
        di_route("r", "/r", deps=[use(PERM, "require_permission(C)", "router")], all_deps=[PERM]),
        di_route("t", "/t", deps=[use("m:helper", "helper", "direct")], all_deps=["m:helper", PERM]),
        di_route("x", "/x", line=166, file="backend/service_backend/users.py", deps=[use(GET_DB, "get_db", "direct")]),
    ]
    routes += [di_route(f"h{i}", f"/r{i}", deps=[use(GET_DB, "get_db", "direct")]) for i in range(n)]
    return di_result([dep(PERM, kind="factory_result"), dep(GET_DB), dep("m:helper", sub=[PERM])], routes)


def test_di_reverse_labels_how_each_route_gets_the_dependency(server: SimpleNamespace) -> None:
    server.fakes.di = _dep_routes()
    text = mcp_server.di_reverse("require_permission(AUDIT_VIEW)")
    assert "Unknown dependency" not in text
    assert "Matched on name 'require_permission'; factory arguments are not filtered." in text
    f = "backend/service_backend/api/routes/r.py"
    assert f"GET /d  →  d()  [{f}:10]  (direct: require_permission(A))" in text
    assert f"GET /g  →  g()  [{f}:10]  (route decorator: require_permission(B))" in text
    assert f"GET /r  →  r()  [{f}:10]  (router: require_permission(C))" in text
    assert f"GET /t  →  t()  [{f}:10]  (transitive)" in text
    assert "/x" not in text

    payload = j(mcp_server.di_reverse("require_permission", output_format="json"))
    group = payload["groups"][0]
    assert {r["path"]: r["via"] for r in group["routes"]} == {
        "/d": "direct",
        "/g": "route decorator",
        "/r": "router",
        "/t": "transitive",
    }
    assert group["via_counts"] == {"direct": 1, "route decorator": 1, "router": 1, "transitive": 1}


def test_di_reverse_unknown_name_suggests_instead_of_dumping(server: SimpleNamespace) -> None:
    server.fakes.di = di_result([dep(f"m:dep_{i}") for i in range(30)] + [dep(GET_DB)])
    text = mcp_server.di_reverse("get_dbb")
    assert "Did you mean get_db?" in text
    assert "dep_17" not in text, "an unknown name must not dump every dependency target"
    payload = j(mcp_server.di_reverse("zzz_nothing", output_format="json"))
    assert (payload["groups"], payload["suggestions"]) == ([], [])
    assert "di_deps()" in mcp_server.di_reverse("zzz_nothing")


def test_di_reverse_groups_same_name_definitions(server: SimpleNamespace) -> None:
    a, b = "pkg.squad_ws:_get_heartbeat_manager", "pkg.heartbeat_ws:_get_heartbeat_manager"
    server.fakes.di = di_result(
        [dep(a, line=64), dep(b, line=62)],
        [
            di_route("f", "/ws/f", method="WS", deps=[use(a, "_get_heartbeat_manager", "direct")]),
            di_route("v", "/ws/v", method="WS", deps=[use(b, "_get_heartbeat_manager", "direct")]),
        ],
    )
    payload = j(mcp_server.di_reverse("_get_heartbeat_manager", output_format="json"))
    assert [(g["dependency"]["id"], g["route_count"]) for g in payload["groups"]] == sorted([(a, 1), (b, 1)])
    assert "'_get_heartbeat_manager' matches 2 dependency targets" in mcp_server.di_reverse("_get_heartbeat_manager")


def test_di_reverse_takes_the_exact_id_of_an_instance_or_a_bound_method(server: SimpleNamespace) -> None:
    instance, bound = "pkg.svc:Svc()", "pkg.svc:Svc().get_items"
    server.fakes.di = di_result(
        [dep(instance), dep(bound)],
        [
            di_route("a", "/a", deps=[use(instance, "Svc()", "direct")]),
            di_route("b", "/b", deps=[use(bound, "svc.get_items", "direct")]),
        ],
    )
    for dep_id, path in ((instance, "/a"), (bound, "/b")):
        payload = j(mcp_server.di_reverse(dep_id, output_format="json"))
        assert (payload["matched_on"], [g["dependency"]["id"] for g in payload["groups"]]) == ("id", [dep_id])
        assert [r["path"] for r in payload["groups"][0]["routes"]] == [path]
    unknown = j(mcp_server.di_reverse("pkg.svc:Svc().get_item", output_format="json"))
    assert unknown["groups"] == []
    assert unknown["suggestions"] == [bound, instance], "an unknown id is suggested ids, closest first, not names"


def test_di_deps_shows_route_decorator_guards_with_alias(server: SimpleNamespace) -> None:
    server.fakes.di = di_result(
        [dep(PERM, kind="factory_result", aliases=["_require_list_access"]), dep(GET_DB)],
        [
            di_route(
                "list_locations",
                "/locations",
                deps=[
                    use("m:require_active_user", "require_active_user", "router"),
                    use(PERM, "require_permission(…)", "route decorator", alias="_require_list_access"),
                    use(GET_DB, "get_db", "direct"),
                ],
            )
        ],
    )
    text = mcp_server.di_deps(handler="list_locations")
    assert "  Route deps:   require_permission(…) [as _require_list_access]" in text
    assert "  Router deps:  require_active_user" in text
    payload = j(mcp_server.di_deps(handler="list_locations", output_format="json"))
    assert payload["matches"][0]["decorator_deps"] == ["require_permission(…)"]
    assert payload["matches"][0]["deps"][1]["alias"] == "_require_list_access"


def test_di_reverse_and_deps_print_handler_def_lines(server: SimpleNamespace) -> None:
    server.fakes.di = _dep_routes()
    assert "GET /x  →  x()  [backend/service_backend/users.py:166]  (direct)" in mcp_server.di_reverse("get_db")
    reverse = j(mcp_server.di_reverse("get_db", output_format="json"))
    row = reverse["groups"][0]["routes"][0]
    assert (row["file"], row["line"]) == ("backend/service_backend/users.py", 166)
    assert "x()  [backend/service_backend/users.py:166]" in mcp_server.di_deps(handler="x")
    deps = j(mcp_server.di_deps(handler="x", output_format="json"))
    assert deps["matches"][0]["line"] == 166
    assert "def_line" not in deps["matches"][0], "backend rows carry `line` (the def line) only"


def test_di_deps_method_hint(server: SimpleNamespace) -> None:
    """A handler that exists for other methods says so."""
    server.fakes.di = di_result([], [di_route("list_articles", "/v"), di_route("list_articles", "/v", method="POST")])
    text = mcp_server.di_deps(handler="list_articles", method="PUT")
    assert "list_articles exists for GET, POST; none for PUT." in text


def test_unknown_handler_hint_suggests_name_not_suffix(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result([pair("/r", "create_appointment", "createAppointment")])
    server.fakes.di = di_result([], [di_route("create_appointment", "/r")])
    route_text = mcp_server.route_match(handler="create_appointmet")
    assert "did you mean create_appointment" in route_text
    assert "suffix" not in route_text
    assert "did you mean createAppointment" in mcp_server.route_match(frontend_func="createAppointmet")
    di_text = mcp_server.di_deps(handler="create_appointmet")
    assert "did you mean create_appointment" in di_text
    assert "suffix" not in di_text
    assert "match='suffix'" in mcp_server.route_match(path="/nope")
    assert "match='suffix'" in mcp_server.di_deps(path="/nope")


# ── Route tools ──────────────────────────────────────────────────────────


def test_route_match_default_is_exact_not_suffix(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result([pair("/admin/audit/users", "list_audit_users", "listAuditUsers")])
    assert j(mcp_server.route_match(path="/users", output_format="json"))["match_count"] == 0
    assert j(mcp_server.route_match(path="/users", match="suffix", output_format="json"))["match_count"] == 1


def test_route_match_falls_back_to_orphans(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        backend_only=[backend_side("GET", "/admin/failed-emails", "list_dead_letter_entries", line=34)]
    )
    payload = j(mcp_server.route_match(handler="list_dead_letter_entries", output_format="json"))
    assert payload["match_count"] == 0
    assert [r["handler"] for r in payload["backend_only"]] == ["list_dead_letter_entries"]


def test_route_match_json_limit_and_contains(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        [
            pair("/api/appointments", "list_appointments", "listAppointments"),
            pair("/api/appointment-reports", "list_appointment_reports", "listAppointmentReports"),
        ]
    )
    payload = j(mcp_server.route_match(path="/appointments", match="suffix", output_format="json"))
    assert [m["backend"]["handler"] for m in payload["matches"]] == ["list_appointments"]
    contains = j(mcp_server.route_match(path="appointment", match="contains", limit=1, output_format="json"))
    assert (contains["match_count"], contains["omitted"]) == (2, 1)


def test_route_unmatched_has_the_same_keys_for_every_side(server: SimpleNamespace) -> None:
    """The side not asked for is null, so a reply's keys never depend on ``side``."""
    server.fakes.routes = routes_result(
        backend_only=[backend_side("GET", "/api/a", "a")],
        frontend_only=[frontend_side("GET", "/api/gone", "gone")],
    )
    replies = {
        side: j(mcp_server.route_unmatched(side=side, output_format="json")) for side in ("backend", "frontend", "both")
    }
    assert set(replies["backend"]) == set(replies["frontend"]) == set(replies["both"])
    assert (replies["backend"]["frontend_only"], replies["backend"]["frontend_only_count"]) == (None, None)
    assert replies["backend"]["frontend_omitted"] is None
    assert (replies["frontend"]["backend_only"], replies["frontend"]["backend_only_count"]) == (None, None)
    assert replies["frontend"]["backend_omitted"] is None
    assert (replies["both"]["backend_only_count"], replies["both"]["frontend_only_count"]) == (1, 1)


def test_unmatched_and_reverse_counts_when_limited(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        backend_only=[backend_side("GET", "/api/a", "a"), backend_side("GET", "/api/b", "b")]
    )
    unmatched = j(mcp_server.route_unmatched(side="backend", limit=1, output_format="json"))
    assert (unmatched["backend_only_count"], unmatched["backend_omitted"]) == (2, 1)
    server.fakes.di = di_result(
        [dep("m:get_policy_context")],
        [
            di_route("a", "/api/a", deps=[use("m:get_policy_context", "get_policy_context", "direct")]),
            di_route("b", "/api/b", all_deps=["m:get_policy_context"]),
        ],
    )
    reverse = j(mcp_server.di_reverse("get_policy_context", limit=1, output_format="json"))
    assert (reverse["route_count"], reverse["omitted"]) == (2, 1)


def test_orphan_fallback_uses_and_semantics(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        backend_only=[backend_side("GET", "/foo", "list_foo"), backend_side("GET", "/bar", "list_bar", line=2)]
    )
    payload = j(mcp_server.route_match(handler="list_foo", path="/bar", output_format="json"))
    assert payload["match_count"] == 0
    assert payload["backend_only"] is None, "orphans matching only one filter must not surface"
    both = j(mcp_server.route_match(handler="list_foo", path="/foo", output_format="json"))
    assert [r["handler"] for r in both["backend_only"]] == ["list_foo"]
    one = j(mcp_server.route_match(handler="list_bar", output_format="json"))
    assert [r["handler"] for r in one["backend_only"]] == ["list_bar"]


def test_orphan_fallback_says_when_rows_were_cut(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        backend_only=[backend_side("GET", f"/admin/x{i}", f"x{i}", line=i) for i in range(12)]
    )
    text = mcp_server.route_match(path="/admin", match="contains", limit=10)
    assert "Backend routes with no frontend consumer (12):" in text
    assert "showing 10 of 12 routes; pass limit=12 (or null) to see all" in text
    payload = j(mcp_server.route_match(path="/admin", match="contains", limit=10, output_format="json"))
    assert (len(payload["backend_only"]), payload["backend_only_count"], payload["backend_omitted"]) == (10, 12, 2)


def test_path_match_ignores_param_names_in_every_mode(server: SimpleNamespace) -> None:
    """The tool docstrings promise parameter names are ignored; `contains` keeps that promise too."""
    server.fakes.routes = routes_result([pair("/articles/{article_id}", "get_article", "getArticle")])
    for match in ("exact", "suffix", "auto", "contains"):
        assert (
            j(mcp_server.route_match(path="/articles/{id}", match=match, output_format="json"))["match_count"] == 1
        ), match
    assert mcp_server.queries.path_matches("/{id}", "/articles/{article_id}", match="suffix")
    assert mcp_server.queries.path_matches("{id}", "/articles/{article_id}", match="contains")
    assert mcp_server.queries.path_matches("{article_id}", "/articles/{article_id}", match="contains")
    assert not mcp_server.queries.path_matches("/articles/{id}/x", "/articles/{article_id}", match="contains")


def test_route_match_summary_splits_routes_and_calls_and_marks_possible(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        [
            pair("/a", "a", "fa"),
            pair(
                "/x/changes/export",
                "export_changes",
                "useAuditFilterTab",
                line=211,
                confidence="possible",
                reason="hole_on_literal",
            ),
            pair("/x/changes/export", "export_changes", "useAuditFilterTab", line=211, file="frontend/b.ts"),
        ]
    )
    summary = mcp_server.route_match()
    assert "Matched routes:  2" in summary
    assert "Matched calls:   3" in summary
    assert "Match rows:      3" in summary
    text = mcp_server.route_match(path="/x/changes/export")
    assert "GET /x/changes/export  (possible: hole_on_literal)" in text
    assert "(possible" not in mcp_server.route_match(path="/a")


def test_route_match_shows_frontend_def_and_call_lines(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result(
        [
            pair("/user-files", "upload_user_file", "uploadUserFile", line=2348, def_line=2328),
            pair("/one", "one", "oneLiner", line=4, def_line=4),
            pair("/ws/x", "ws", None, method="WS", line=9, client=None),
        ],
        frontend_only=[frontend_side("GET", "/gone", "gone", line=30, def_line=28)],
    )
    text = mcp_server.route_match(frontend_func="uploadUserFile")
    assert "uploadUserFile()  [frontend/lib/api.ts def 2328 · call 2348] via apiJson" in text
    payload = j(mcp_server.route_match(frontend_func="uploadUserFile", output_format="json"))
    assert (payload["matches"][0]["frontend"]["def_line"], payload["matches"][0]["frontend"]["line"]) == (2328, 2348)
    assert "oneLiner()  [frontend/lib/api.ts:4]" in mcp_server.route_match(frontend_func="oneLiner")
    assert "(module)()  [frontend/lib/api.ts:9]" in mcp_server.route_match(path="/ws/x")
    assert "gone()  [frontend/lib/api.ts def 28 · call 30]" in mcp_server.route_unmatched(side="frontend")
    assert "gone()  [frontend/lib/api.ts def 28 · call 30]" in mcp_server.route_match(frontend_func="gone")


def test_list_tools_default_to_fifty_rows_with_explicit_note(server: SimpleNamespace) -> None:
    """Every list tool defaults to 50 rows and says when it cut."""
    n = 60
    server.fakes.di = di_result(
        [dep(GET_DB)], [di_route(f"h{i}", f"/r{i}", deps=[use(GET_DB, "get_db", "direct")]) for i in range(n)]
    )
    server.fakes.routes = routes_result(
        [pair(f"/api/m{i}", f"m{i}", f"fm{i}", line=i) for i in range(n)],
        backend_only=[backend_side("GET", f"/b{i}", f"b{i}", line=i) for i in range(n)],
    )
    server.fakes.usages = [FakeUsage(f"frontend/f{i}.tsx", 1) for i in range(n)]
    server.fakes.symbols = FakeSymbolResult(
        [FakeCandidate("div", f"f{i}.ts", 1, 1, "const", "const div", "typescript") for i in range(n)]
    )

    reverse = j(mcp_server.di_reverse("get_db", output_format="json"))
    assert (len(reverse["groups"][0]["routes"]), reverse["route_count"], reverse["omitted"]) == (50, n, 10)
    assert f"showing 50 of {n} routes; pass limit={n}" in mcp_server.di_reverse("get_db")
    assert len(j(mcp_server.di_reverse("get_db", limit=None, output_format="json"))["groups"][0]["routes"]) == n

    contains = j(mcp_server.route_match(path="/api", match="contains", output_format="json"))
    assert (len(contains["matches"]), contains["match_count"], contains["omitted"]) == (50, n, 10)
    unmatched = j(mcp_server.route_unmatched(side="backend", output_format="json"))
    assert (len(unmatched["backend_only"]), unmatched["backend_omitted"]) == (50, 10)
    jsx = j(mcp_server.jsx_usage("div", output_format="json"))
    assert (len(jsx["usages"]), jsx["usage_count"], jsx["omitted"]) == (50, n, 10)
    assert f"showing 50 of {n} usages" in mcp_server.jsx_usage("div")
    sym = j(mcp_server.symbol_resolve("div", output_format="json"))
    assert (len(sym["candidates"]), sym["candidate_count"], sym["omitted"]) == (50, n, 10)
    assert len(j(mcp_server.di_deps(path="/r", match="contains", output_format="json"))["matches"]) == n, (
        "di_deps has no default limit"
    )


def test_limitations_are_one_line_in_text_and_full_in_json_and_status(server: SimpleNamespace) -> None:
    lims = [limitation("conditional_router_mount", "m" * 200), limitation("unresolved_http_method", "n" * 200)]
    server.fakes.routes = routes_result(limitations=lims)
    server.fakes.di = di_result(limitations=[lims[0]])
    text = mcp_server.route_unmatched()
    first = text.splitlines()[0]
    assert first.startswith("ⓘ 2 analysis limitation(s) (conditional_router_mount, unresolved_http_method)")
    assert "m" * 200 not in text, "the full limitation text belongs in status and JSON"
    assert j(mcp_server.route_unmatched(output_format="json"))["analysis_limitations"] == lims
    mcp_server.di_deps()
    assert "m" * 200 in mcp_server.status()
    status = j(mcp_server.status(output_format="json"))
    assert [lim["kind"] for lim in status["analysis_limitations"]] == [
        "conditional_router_mount",
        "unresolved_http_method",
    ]


@pytest.mark.parametrize(
    ("tool", "attr"),
    [("di_deps", "di"), ("route_match", "routes"), ("di_reverse", "di")],
)
def test_limitations_reach_every_analyzer_tool(server: SimpleNamespace, tool: str, attr: str) -> None:
    """A limitation survives the envelope into JSON and text without flipping
    `complete` or raising the incomplete banner."""
    if attr == "di":
        server.fakes.di = di_result([dep(GET_DB)], limitations=[limitation("conditional_router_mount")])
    else:
        server.fakes.routes = routes_result(limitations=[limitation("conditional_router_mount")])
    call = getattr(mcp_server, tool)
    args = ("get_db",) if tool == "di_reverse" else ()
    payload = j(call(*args, output_format="json"))
    assert payload["complete"] is True
    assert [lim["kind"] for lim in payload["analysis_limitations"]] == ["conditional_router_mount"]
    text = call(*args)
    assert "conditional_router_mount" in text
    assert "INCOMPLETE" not in text


# ── JSX and symbols ──────────────────────────────────────────────────────


def test_jsx_usage_json_carries_complete_and_problems(server: SimpleNamespace) -> None:
    """JSX bodies carry `complete`; a file problem makes them incomplete."""
    server.fakes.usages = [FakeUsage("frontend/a.tsx", 4, column=9, tag_kind="open")]
    ok = j(mcp_server.jsx_usage("Widget", output_format="json"))
    assert (ok["complete"], ok["usage_count"], ok["usages"][0]["column"]) == (True, 1, 9)
    server.fakes.jsx_problems = [read_failed("frontend/b.tsx")]
    partial = j(mcp_server.jsx_usage("Widget", output_format="json"))
    assert partial["complete"] is False
    assert partial["diagnostics"][0]["file"] == "frontend/b.tsx"
    assert mcp_server.jsx_usage("Widget").startswith("⚠ INCOMPLETE — 1 file could not be read")


def test_jsx_usage_passes_filters_and_root(server: SimpleNamespace) -> None:
    mcp_server.jsx_usage("Widget", root="frontend/components", include_self_close=False, include_tests=True)
    call = server.fakes.jsx_calls[-1]
    assert call["root"] == (server.repo / "frontend" / "components").resolve()
    assert (call["include_self_close"], call["include_tests"]) == (False, True)


def test_symbol_kind_argument_is_every_symbol_kind_plus_none(server: SimpleNamespace) -> None:
    """The tool's kind enum and the index's kinds cannot drift apart, and an
    unknown kind is answered with the full list."""
    from code_intel import symbol_resolve

    assert mcp_server._SYMBOL_KINDS == (*symbol_resolve.SYMBOL_KINDS, "none")
    payload = j(mcp_server.symbol_resolve("X", kind=cast(Any, "struct"), output_format="json"))
    assert payload["allowed"] == list(mcp_server._SYMBOL_KINDS)


def test_symbol_resolve_surfaces_file_problems(server: SimpleNamespace) -> None:
    server.fakes.symbols = FakeSymbolResult(
        [
            FakeCandidate(
                "get_db", "backend/service_backend/core/database.py", 83, 90, "function", "def get_db()", "python"
            )
        ],
        complete=False,
        problems=[read_failed("backend/service_backend/broken.py")],
    )
    payload = j(mcp_server.symbol_resolve("get_db", side="backend", output_format="json"))
    assert payload["complete"] is False
    assert [d["file"] for d in payload["diagnostics"]] == ["backend/service_backend/broken.py"]
    text = mcp_server.symbol_resolve("get_db", side="backend")
    assert "INCOMPLETE" in text and "1 file could not be read" in text


def test_symbol_resolve_kind_none_means_any_and_rows_carry_complete(server: SimpleNamespace) -> None:
    server.fakes.symbols = FakeSymbolResult(
        [FakeCandidate("X", "frontend/x.ts", 1, 3, "namespace", "namespace X", "typescript")]
    )
    payload = j(mcp_server.symbol_resolve("X", kind="none", output_format="json"))
    assert server.fakes.symbol_calls[-1]["kind_filter"] is None
    assert payload["complete"] is True
    assert payload["candidates"][0]["kind"] == "namespace"
    mcp_server.symbol_resolve("X", kind="namespace")
    assert server.fakes.symbol_calls[-1]["kind_filter"] == "namespace"


# ── Status and refresh ───────────────────────────────────────────────────


def test_status_reports_caches_git_sha_runtime_and_toolchain(server: SimpleNamespace) -> None:
    payload = j(mcp_server.status(output_format="json"))
    assert payload["repo_root"] == str(server.repo)
    assert "git_sha" in payload
    assert payload["schema_version"] == 2
    assert payload["complete"] is None
    assert set(payload["cache"]) == {"backend_model", "di", "routes"}
    assert payload["runtime"]["runtime_compatible"] is True
    assert payload["toolchain"]["state"] == "ok"
    assert payload["analyzers"]["di_map"]["state"] == "not_run"
    assert payload["analyzers"]["route_map"]["state"] == "not_run"


def test_status_complete_is_null_until_both_ran_then_true(server: SimpleNamespace) -> None:
    mcp_server.di_deps()
    assert j(mcp_server.status(output_format="json"))["complete"] is None
    mcp_server.route_match()
    assert j(mcp_server.status(output_format="json"))["complete"] is True


def test_status_renders_structured_diagnostics(server: SimpleNamespace) -> None:
    server.fakes.di = di_result(
        complete=False, diagnostics=[read_failed("backend/service_backend/api/routes/squads.py")]
    )
    mcp_server.di_deps()
    text = mcp_server.status()
    assert text.startswith("⚠ INCOMPLETE — 1 file could not be read; counts may be undercounts.")
    assert "[read_failed] backend/service_backend/api/routes/squads.py:" in text
    payload = j(mcp_server.status(output_format="json"))
    assert payload["complete"] is False
    assert payload["diagnostics"][0]["step"] == "read file"
    assert "parse_errors" not in json.dumps(payload), "bodies carry no bare-file parse_errors list"


def test_status_never_blames_the_interpreter_for_a_resolver_diagnostic(server: SimpleNamespace) -> None:
    """A compatible interpreter is never named, whatever the diagnostic."""
    resolver = {
        "kind": "unparsed_frontend_callsite",
        "category": "resolver",
        "stage": "route_map",
        "step": "resolve client call",
        "file": "frontend/lib/x.ts",
        "line": 3,
        "message": "frontend/lib/x.ts:3: apiJson is referenced but not called",
    }
    server.fakes.routes = routes_result(complete=False, diagnostics=[resolver])
    mcp_server.route_match()
    text = mcp_server.status()
    assert text.startswith("⚠ INCOMPLETE — 1 API client reference could not be read; results may be imprecise.")
    assert "older than" not in text
    assert "interpreter" not in text
    assert "[unparsed_frontend_callsite] frontend/lib/x.ts:3" in text


def test_status_names_a_failed_analyzer_instead_of_zero_diagnostics(server: SimpleNamespace) -> None:
    server.fakes.raise_in["scan_frontend"] = FileNotFoundError(
        2, "No such file", str(server.repo / "frontend" / "lib" / "api.ts")
    )
    mcp_server.di_deps()
    assert j(mcp_server.route_match(output_format="json"))["error"] == "analysis_failed"
    text = mcp_server.status()
    assert text.startswith(
        "⚠ INCOMPLETE — route_map failed (route_map failed at step 'run typescript helper (routes mode)': FileNotFoundError"
    )
    assert "its results are missing" in text
    assert "0 diagnostic" not in text
    payload = j(mcp_server.status(output_format="json"))
    assert payload["complete"] is False
    assert payload["analyzers"]["route_map"]["last_step"] == "run typescript helper (routes mode)"


def test_status_and_refresh_use_split_route_counts(server: SimpleNamespace) -> None:
    """status `matched_routes` counts routes, refresh's "frontend calls matched" counts calls."""
    server.fakes.routes = routes_result(
        [pair("/a", "a", "fa"), pair("/a", "a", "fb", line=11), pair("/b", "b", "fc", line=12)],
        backend_only=[backend_side("GET", "/c", "c")],
        frontend_only=[frontend_side("GET", "/z", "z", line=99)],
    )
    status = j(mcp_server.status(load_counts=True, output_format="json"))
    routes = status["counts"]["routes"]
    assert (routes["matched_routes"], routes["matched_calls"], routes["match_rows"]) == (2, 3, 3)
    assert status["counts"]["di"]["routes"] == 0
    refreshed = mcp_server.refresh()
    assert "Routes: 3/4 frontend calls matched, 2/3 backend routes matched" in refreshed


def test_status_load_counts_keeps_di_when_routes_fail(server: SimpleNamespace) -> None:
    """One failing analyzer does not fail the call."""
    server.fakes.raise_in["build_routes"] = RuntimeError("boom")
    payload = j(mcp_server.status(load_counts=True, output_format="json"))
    assert payload["counts"]["di"] is not None
    assert payload["counts"]["routes"] is None
    assert payload["counts_errors"]["routes"]["error"] == "analysis_failed"


# ── Analyzer code staleness ──────────────────────────────────────────────


def test_warns_when_analyzer_code_changed_since_start(server: SimpleNamespace, tmp_path: Path) -> None:
    """After a `git pull` rewrites an analyzer, every response says so, errors
    included. A touch that leaves the content unchanged is not an alarm."""
    src = write(tmp_path / "di_map.py", "VERSION = 1\n", 1000)
    server.rt.code_watch = analysis_runtime.CodeWatch([src])

    def tool_text() -> str:
        return mcp_server.symbol_resolve("Foo", side=cast(Any, "sideways"))

    assert j(mcp_server.status(output_format="json"))["analyzer_code_stale"] is False
    assert "restart the code-intel MCP server" not in tool_text()
    os.utime(src, (5000, 5000))
    assert "restart the code-intel MCP server" not in tool_text()

    write(src, "VERSION = 2\n", 9000)
    assert j(mcp_server.status(output_format="json"))["analyzer_code_stale"] is True
    assert mcp_server.status().startswith(presenters.STALE_CODE_WARNING)
    assert mcp_server.status().count("restart the code-intel MCP server") == 1
    assert tool_text().startswith(presenters.STALE_CODE_WARNING)
    assert (
        j(mcp_server.symbol_resolve("Foo", side=cast(Any, "sideways"), output_format="json"))["analyzer_code_stale"]
        is True
    )


@pytest.mark.parametrize("module_file", ["analysis_runtime.py", "queries.py", "presenters.py", "server.py"])
def test_editing_a_server_module_marks_the_server_stale(
    server: SimpleNamespace, tmp_path: Path, module_file: str
) -> None:
    """Watches copies of the real watched files, so the repo is never written."""
    copies = []
    for path in analysis_runtime.watched_files():
        if not path.is_file():
            continue
        copy = write(tmp_path / "copies" / path.name, path.read_text(encoding="utf-8"), 1000)
        copies.append(copy)
    server.rt.code_watch = analysis_runtime.CodeWatch(copies)
    assert "restart the code-intel MCP server" not in mcp_server.route_unmatched(side=cast(Any, "bogus"))
    edited = tmp_path / "copies" / module_file
    write(edited, edited.read_text(encoding="utf-8") + "\n# edited\n", 2000)
    assert mcp_server.route_unmatched(side=cast(Any, "bogus")).startswith(presenters.STALE_CODE_WARNING)
    assert j(mcp_server.status(output_format="json"))["analyzer_code_stale"] is True


def test_server_instructions_name_no_other_tool_layer() -> None:
    """The instructions defer to AGENTS.md instead of naming other servers."""
    text = mcp_server.mcp.instructions or ""
    assert text.splitlines()[-1] == (
        "For general references, call hierarchy and text search, use the repository's other "
        "code-intelligence tools (see AGENTS.md)."
    )
    for name in ("probe", "LSP", "mcp__"):
        assert name not in text
    assert "caches refresh themselves on file and config changes" in text
    assert "- doc_refs: docs that mention a file, route or symbol" in text


# ── Frontend not analyzed: no orphan claims ──────────────────────────────


def test_route_match_query_without_a_frontend_makes_no_orphan_claim(no_toolchain: SimpleNamespace) -> None:
    """Without the frontend every backend route would look unused, so a query
    lists the routes that fit under a heading that names the missing path and the fix."""
    no_toolchain.fakes.routes = routes_result(backend_only=[backend_side("GET", "/articles", "list_articles")])
    text = mcp_server.route_match(handler="list_articles")
    assert "no frontend consumer" not in text
    assert (f"Frontend not analyzed: {TS_MISSING} not found. Fix: {TS_FIX}") in text
    assert "GET /articles  →  list_articles()" in text
    assert text.count("Fix:") == 1

    payload = j(mcp_server.route_match(handler="list_articles", output_format="json"))
    assert (payload["backend_only"], payload["frontend_only"], payload["match_count"]) == (None, None, None)
    assert [r["handler"] for r in payload["backend_routes"]] == ["list_articles"]
    assert payload["complete"] is False

    summary = j(mcp_server.route_match(output_format="json"))["summary"]
    assert (summary["backend_routes"], summary["backend_only"], summary["frontend_only"]) == (1, None, None)
    assert "Backend-only:    n/a (frontend not analyzed)" in mcp_server.route_match()


def test_route_match_frontend_func_without_a_frontend_says_it_cannot_look_it_up(no_toolchain: SimpleNamespace) -> None:
    text = mcp_server.route_match(frontend_func="listArticles")
    assert "frontend_func='listArticles' cannot be looked up without the frontend." in text
    assert "no backend route" not in text.lower()


def test_a_missing_frontend_root_states_its_fix_once(server: SimpleNamespace) -> None:
    """The routes diagnostic message already ends in the fix; the error adds it once."""
    import shutil

    shutil.rmtree(server.repo / "frontend")
    server.fakes.routes = routes_result(
        complete=False,
        diagnostics=[
            problems.diagnostic(
                "frontend_unavailable",
                stage="route_map",
                step="analyze frontend",
                file="frontend",
                message="frontend: frontend root not found. Fix: run the analysis from a checkout that has frontend/",
                missing="frontend",
                fix="run the analysis from a checkout that has frontend/",
            ).as_dict()
        ],
    )
    payload = j(mcp_server.route_unmatched(output_format="json"))
    assert (payload["error"], payload["problem"], payload["missing"]) == (
        "frontend_unavailable",
        "frontend_not_found",
        "frontend",
    )
    assert payload["message"].count("Fix:") == 1
    assert mcp_server.route_unmatched().count("Fix:") == 1


# ── Helper crashes: one error code everywhere ────────────────────────────

_HELPER_CRASH = ts_bridge.HelperError(
    "facts", "exit", "node exited with code 1: Error: Cannot find module 'lib/tsc.js'"
)


def _routes_helper_crash() -> dict[str, Any]:
    return dict(
        problems.diagnostic(
            "frontend_unavailable",
            stage="route_map",
            step="analyze frontend",
            file=None,
            message=(
                "ts/helper.cjs: typescript helper (routes mode) failed at step 'exit': "
                "node exited with code 1. Fix: " + analysis_runtime.HELPER_FAILED_FIX
            ),
            fix=analysis_runtime.HELPER_FAILED_FIX,
        ).as_dict()
    )


def test_a_helper_crash_is_frontend_unavailable_helper_failed_in_every_tool(server: SimpleNamespace) -> None:
    server.fakes.raise_in["find_jsx_usage"] = _HELPER_CRASH
    jsx = j(mcp_server.jsx_usage("Widget", output_format="json"))
    assert (jsx["error"], jsx["problem"], jsx["missing"]) == ("frontend_unavailable", "helper_failed", None)
    assert "typescript helper (facts mode) failed at step 'exit'" in jsx["message"]
    assert jsx["fix"] == analysis_runtime.HELPER_FAILED_FIX

    server.fakes.raise_in["resolve"] = _HELPER_CRASH
    sym = j(mcp_server.symbol_resolve("Widget", side="frontend", output_format="json"))
    assert (sym["error"], sym["problem"]) == ("frontend_unavailable", "helper_failed")

    server.fakes.routes = routes_result(complete=False, diagnostics=[_routes_helper_crash()])
    unmatched = j(mcp_server.route_unmatched(output_format="json"))
    assert (unmatched["error"], unmatched["problem"]) == ("frontend_unavailable", "helper_failed")
    assert unmatched["message"].count("Fix:") == 1
    matched = mcp_server.route_match(handler="x")
    assert "Frontend not analyzed: ts/helper.cjs" in matched

    del server.fakes.raise_in["resolve"]
    server.fakes.symbols = FakeSymbolResult(
        [],
        complete=False,
        problems=[
            problems.diagnostic(
                "frontend_unavailable",
                stage="symbol_resolve",
                step="run typescript helper (facts mode)",
                file=None,
                message="helper.cjs: typescript helper (facts mode) failed at step 'exit': boom",
                fix=analysis_runtime.HELPER_FAILED_FIX,
            )
        ],
        frontend_problem=analysis_runtime.FrontendProblem(
            "helper_failed", "helper.cjs: boom", None, analysis_runtime.HELPER_FAILED_FIX
        ),
    )
    both = j(mcp_server.symbol_resolve("Widget", output_format="json"))
    assert both["complete"] is False
    assert "helper.cjs: boom" in both["frontend_problem"]
    assert j(mcp_server.status(output_format="json"))["toolchain"]["problem"]["code"] == "helper_failed"


def test_a_real_helper_crash_carries_its_problem_code_on_every_symbol_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    """A TypeScript directory whose package.json says typescript but holds no
    compiler makes the real helper crash; every side names `helper_failed`."""
    fake_ts = tmp_path / "fake-typescript"
    write(fake_ts / "package.json", '{"name": "typescript", "version": "7.1.0-beta"}\n')
    monkeypatch.setenv("CODE_INTEL_TYPESCRIPT_DIR", str(fake_ts))
    hermetic_git(monkeypatch, tmp_path / "home")
    repo = make_repo(git_init(tmp_path / "repo"))
    rt = analysis_runtime.AnalysisRuntime(repo, ConfigFile.locate(repo), [])
    monkeypatch.setattr(mcp_server, "runtime", rt)

    frontend = j(mcp_server.symbol_resolve("Page", side="frontend", output_format="json"))
    assert (frontend["error"], frontend["problem"]) == ("frontend_unavailable", "helper_failed")
    both = j(mcp_server.symbol_resolve("Page", output_format="json"))
    unavailable = [d for d in both["diagnostics"] if d["kind"] == "frontend_unavailable"]
    assert [d.get("problem") for d in unavailable] == ["helper_failed"]
    assert both["complete"] is False


def test_status_reports_a_helper_crash_until_a_helper_run_succeeds(server: SimpleNamespace) -> None:
    server.fakes.raise_in["find_jsx_usage"] = _HELPER_CRASH
    mcp_server.jsx_usage("Widget")
    block = j(mcp_server.status(output_format="json"))["toolchain"]
    assert block["state"] == "helper_failed"
    assert block["problem"]["code"] == "helper_failed"
    text = mcp_server.status()
    assert "but the last TypeScript helper run in facts mode failed: " in text
    assert "helper.cjs: typescript helper (facts mode) failed at step 'exit'" in text
    assert ": ok" not in text.split("Frontend toolchain:")[1].splitlines()[0]

    del server.fakes.raise_in["find_jsx_usage"]
    mcp_server.jsx_usage("Widget")
    assert j(mcp_server.status(output_format="json"))["toolchain"]["state"] == "ok"


def test_symbol_resolve_both_states_a_missing_frontend_once(server: SimpleNamespace) -> None:
    server.fakes.symbols = FakeSymbolResult(
        [],
        complete=False,
        problems=[
            problems.diagnostic(
                "frontend_unavailable",
                stage="symbol_resolve",
                step="run typescript helper (facts mode)",
                file="node",
                message="frontend symbols were not searched: node not found on PATH. Fix: install node",
            )
        ],
        frontend_problem="node not found on PATH. Fix: install node",
    )
    text = mcp_server.symbol_resolve("Widget")
    assert text.count("node not found on PATH") == 1


# ── Argument edge cases ──────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["get_db(garbage", "get_db)x(", "(x)", "get_db(x) y"])
def test_di_reverse_rejects_a_malformed_factory_call(server: SimpleNamespace, value: str) -> None:
    payload = j(mcp_server.di_reverse(value, output_format="json"))
    assert (payload["error"], payload["argument"], payload["value"]) == ("invalid_argument", "dep_function", value)
    assert server.fakes.calls == {}, "rejected before any analysis"


def test_di_reverse_matches_names_untrimmed_and_notes_factories_only_for_calls(server: SimpleNamespace) -> None:
    server.fakes.di = _dep_routes()
    assert j(mcp_server.di_reverse(" get_db", output_format="json"))["groups"] == []
    assert "factory arguments are not filtered" not in mcp_server.di_reverse("get_db")
    assert "factory arguments are not filtered" in mcp_server.di_reverse("require_permission(X)")


def test_di_reverse_omitted_note_suggests_the_limit_that_shows_every_group(server: SimpleNamespace) -> None:
    """One limit is shared across groups, so the note names the total, not the group's count."""
    a, b = "pkg.squad_ws:_get_heartbeat_manager", "pkg.heartbeat_ws:_get_heartbeat_manager"
    server.fakes.di = di_result(
        [dep(a, line=64), dep(b, line=62)],
        [
            di_route("f", "/ws/f", method="WS", deps=[use(a, "_get_heartbeat_manager", "direct")]),
            di_route("v", "/ws/v", method="WS", deps=[use(b, "_get_heartbeat_manager", "direct")]),
        ],
    )
    text = mcp_server.di_reverse("_get_heartbeat_manager", limit=1)
    assert "showing 0 of 1 routes; pass limit=2 (or null) to see all" in text
    assert "pass limit=1 " not in text


def test_a_nul_in_root_is_an_invalid_root(server: SimpleNamespace) -> None:
    payload = j(mcp_server.jsx_usage("Widget", root="frontend\x00x", output_format="json"))
    assert (payload["error"], payload["root"]) == ("invalid_root", "frontend\x00x")
    assert "NUL" in payload["message"]


def test_internal_errors_name_the_frame_that_raised(server: SimpleNamespace, monkeypatch, caplog) -> None:
    monkeypatch.setattr(mcp_server.queries, "di_deps", lambda *a, **k: 1 / 0)
    with caplog.at_level("ERROR", logger="server"):
        payload = j(mcp_server.di_deps(output_format="json"))
    assert "failed unexpectedly at test_mcp_tools.py:" in payload["message"]
    assert "in <lambda>" in payload["message"]
    assert any(r.exc_info for r in caplog.records), "the traceback is logged"


# ── JSON keys ────────────────────────────────────────────────────────────


def test_every_list_reply_carries_omitted(server: SimpleNamespace) -> None:
    doc_lookup_repo(server.repo)
    server.fakes.di = _dep_routes()
    server.fakes.routes = routes_result([pair("/a", "a", "fa")], backend_only=[backend_side("GET", "/b", "b")])
    replies = [
        mcp_server.di_deps(handler="nope", output_format="json"),
        mcp_server.di_deps(handler="x", output_format="json"),
        mcp_server.di_reverse("zzz", output_format="json"),
        mcp_server.di_reverse("get_db", output_format="json"),
        mcp_server.route_match(path="/a", output_format="json"),
        mcp_server.route_match(path="/b", output_format="json"),
        mcp_server.route_match(path="/zzz", output_format="json"),
        mcp_server.route_unmatched(output_format="json"),
        mcp_server.jsx_usage("Widget", output_format="json"),
        mcp_server.symbol_resolve("Widget", output_format="json"),
        mcp_server.doc_refs(file=DOC_LOOKUP_ROUTE_FILE, output_format="json"),
        mcp_server.doc_refs(route="/items/{id}/checkout", output_format="json"),
        mcp_server.doc_refs(symbol="Holder", output_format="json"),
        mcp_server.doc_refs(doc="docs/a.md", output_format="json"),
    ]
    assert [("omitted" in j(r)) for r in replies] == [True] * len(replies)
    orphans = j(replies[5])
    unmatched = j(replies[7])
    unmatched_keys = {k for k in unmatched if k.startswith(("backend_", "frontend_"))}
    assert unmatched_keys <= set(orphans), "orphan lists use the same keys in both route tools"


# ── No frontend/ in the checkout ─────────────────────────────────────────


@pytest.fixture
def no_frontend(server: SimpleNamespace) -> SimpleNamespace:
    import shutil

    shutil.rmtree(server.repo / "frontend")
    return server


@pytest.mark.parametrize("root", ["frontend", "frontend/app"])
def test_jsx_usage_without_frontend_reports_frontend_not_found(no_frontend: SimpleNamespace, root: str) -> None:
    """The default root is not a user mistake: it is the same missing frontend the route tools report."""
    payload = j(mcp_server.jsx_usage("Widget", root=root, output_format="json"))
    assert (payload["error"], payload["problem"], payload["missing"]) == (
        "frontend_unavailable",
        "frontend_not_found",
        "frontend",
    )
    expected = ts_bridge.frontend_not_found_problem(no_frontend.repo, "frontend")
    assert expected is not None and payload["fix"] == expected.fix
    assert no_frontend.fakes.calls.get("find_jsx_usage") is None


def test_jsx_usage_with_a_missing_root_elsewhere_is_still_invalid_root(no_frontend: SimpleNamespace) -> None:
    assert j(mcp_server.jsx_usage("Widget", root="docs", output_format="json"))["error"] == "invalid_root"


def test_frontend_symbols_without_frontend_report_frontend_not_found(no_frontend: SimpleNamespace) -> None:
    payload = j(mcp_server.symbol_resolve("Widget", side="frontend", output_format="json"))
    assert (payload["error"], payload["problem"]) == ("frontend_unavailable", "frontend_not_found")


# ── Refresh without a frontend ───────────────────────────────────────────


def test_refresh_without_a_frontend_reports_no_matched_counts(no_toolchain: SimpleNamespace) -> None:
    no_toolchain.fakes.routes = routes_result(backend_only=[backend_side("GET", "/articles", "list_articles")])
    text = mcp_server.refresh()
    assert "matched" not in text
    assert "Routes: 1 backend routes; frontend not analyzed: ts-cache/node_modules/typescript/package.json" in text
    payload = j(mcp_server.refresh(output_format="json"))
    assert payload["routes_summary"]["backend_routes"] == 1
    assert payload["routes_summary"]["matched_routes"] is None
    assert payload["frontend_problem"].startswith("ts-cache/node_modules/typescript/package.json not found")


def test_refresh_with_a_frontend_reports_matched_counts(server: SimpleNamespace) -> None:
    server.fakes.routes = routes_result([pair("/a", "a", "fa")], backend_only=[backend_side("GET", "/b", "b")])
    assert "1/1 frontend calls matched, 1/2 backend routes matched" in mcp_server.refresh()
    assert j(mcp_server.refresh(output_format="json"))["frontend_problem"] is None


# ── Stable JSON keys per tool ────────────────────────────────────────────


def test_route_match_query_replies_share_one_key_set(server: SimpleNamespace, no_toolchain_server_factory) -> None:
    server.fakes.routes = routes_result([pair("/a", "a", "fa")], backend_only=[backend_side("GET", "/b", "b")])
    hit = j(mcp_server.route_match(path="/a", output_format="json"))
    orphan = j(mcp_server.route_match(path="/b", output_format="json"))
    miss = j(mcp_server.route_match(path="/zzz", output_format="json"))
    no_frontend = j(no_toolchain_server_factory().route_match(path="/a", output_format="json"))
    assert set(hit) == set(orphan) == set(miss) == set(no_frontend)


@pytest.fixture
def no_toolchain_server_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def make():
        _server(tmp_path / "nt", monkeypatch, toolchain_available=False)
        return mcp_server

    return make


def test_di_reverse_replies_share_one_key_set(server: SimpleNamespace) -> None:
    server.fakes.di = _dep_routes()
    known = j(mcp_server.di_reverse("get_db", output_format="json"))
    unknown = j(mcp_server.di_reverse("zzz", output_format="json"))
    assert set(known) == set(unknown)
    assert (known["suggestions"], unknown["matched_on"]) == ([], None)


def test_invalid_argument_omits_allowed_for_free_text(server: SimpleNamespace) -> None:
    free = j(mcp_server.di_reverse("get_db(", output_format="json"))
    assert free["error"] == "invalid_argument" and "allowed" not in free
    fixed = j(mcp_server.route_match(output_format="json", limit=-1))
    assert fixed["error"] == "invalid_argument" and "allowed" not in fixed


# ── Status ───────────────────────────────────────────────────────────────


def test_status_does_not_print_a_runtime_for_an_analyzer_that_never_ran(server: SimpleNamespace) -> None:
    text = mcp_server.status()
    assert "last_runtime_ms=None" not in text
    assert "  di_map: not_run\n" in text


def test_status_counts_report_an_internal_error_per_analyzer(server: SimpleNamespace, monkeypatch) -> None:
    """An exception outside the analyzer steps (not an AnalysisFailed) still
    lands in counts_errors for that analyzer, beside the other's counts."""

    def broken() -> dict:
        raise KeyError("summary")

    monkeypatch.setattr(server.rt, "get_routes", broken)
    payload = j(mcp_server.status(load_counts=True, output_format="json"))
    assert payload["counts"]["di"] is not None
    assert payload["counts"]["routes"] is None
    assert payload["counts_errors"]["routes"]["error"] == "internal_error"
    assert payload["counts_errors"]["routes"]["message"] == "routes: KeyError: 'summary'"
    assert "    routes: failed: routes: KeyError: 'summary'" in mcp_server.status(load_counts=True)


# ── Unknown argument names ───────────────────────────────────────────────


def test_an_unknown_argument_name_is_an_invalid_argument(server: SimpleNamespace) -> None:
    """FastMCP would drop `hanlder` and answer for every route; the server refuses instead."""
    result = _call_over_the_wire("di_deps", {"hanlder": "x", "output_format": "json"})
    assert result.is_error is True
    payload = j(_text(result))
    assert (payload["error"], payload["argument"], payload["value"]) == ("invalid_argument", "hanlder", "x")
    assert "handler" in payload["allowed"] and "output_format" in payload["allowed"]
    assert payload["message"].startswith("Unknown argument 'hanlder' for di_deps. Allowed: ")
    assert server.fakes.calls == {}, "rejected before any analysis"

    text = _call_over_the_wire("status", {"verbose": True})
    assert text.is_error is True
    assert _text(text).startswith("Unknown argument 'verbose' for status. Allowed: ")

    both = _call_over_the_wire("status", {"verbose": True, "bogus_arg": 1, "output_format": "json"})
    payload = j(_text(both))
    assert payload["message"].startswith("Unknown arguments 'bogus_arg', 'verbose' for status. Allowed: ")
    assert (payload["argument"], payload["value"]) == ("bogus_arg", 1)


def test_declared_arguments_still_pass(server: SimpleNamespace) -> None:
    assert _call_over_the_wire("route_match", {"limit": 1}).is_error is False


# ── Orphan claims and coverage gaps ──────────────────────────────────────

_ORPHAN_API_TS = """\
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

_ORPHAN_DOWNLOAD_TS = """\
import { apiFetch } from "../api";
export async function downloadExport(options: { url: string }): Promise<void> {
  await apiFetch(options.url);
}
"""

_ORPHAN_MAIN_PY = """\
from fastapi import FastAPI

app = FastAPI(redirect_slashes=False)


@app.get("/items")
def list_items():
    return []


@app.get("/orders")
def list_orders():
    return []
"""


def _orphan_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain, frontend: dict[str, str], backend_extra=None
) -> None:
    """A real repo (a git work tree) analyzed by the real analyzers behind the tools."""
    hermetic_git(monkeypatch, tmp_path / "home")
    root = git_init(tmp_path / "repo")
    write_config(root, config_toml(E2E_LAYOUT))
    write(root / ".python-version", "3.14\n", 1000)
    write(root / "backend" / "pyproject.toml", "[project]\nname = 'x'\n", 1000)
    write(root / "backend" / "service_backend" / "__init__.py", "", 1000)
    write(root / "backend" / "service_backend" / "main.py", _ORPHAN_MAIN_PY, 1000)
    for name, text in (backend_extra or {}).items():
        write(root / "backend" / "service_backend" / name, text, 1000)
    write(root / "frontend" / "tsconfig.json", '{"compilerOptions": {"jsx": "preserve"}}\n', 1000)
    write(root / "frontend" / "lib" / "api.ts", _ORPHAN_API_TS, 1000)
    write(root / "frontend" / "lib" / "utils" / "download-export.ts", _ORPHAN_DOWNLOAD_TS, 1000)
    for name, text in frontend.items():
        write(root / "frontend" / "lib" / name, text, 1000)
    toolchain = dataclasses.replace(typescript_toolchain, repo_root=root)
    rt = analysis_runtime.AnalysisRuntime(
        root, ConfigFile.locate(root), [], clock=lambda: 10**19, find_toolchain=lambda _root: toolchain
    )
    monkeypatch.setattr(mcp_server, "runtime", rt)


_CALLS_ITEMS = 'import { apiJson } from "./api";\nexport const items = () => apiJson("/items");\n'
# A reference to the client that is not a call: its URL, and so its route, is unknown.
_UNREAD_CALL = 'import { apiJson } from "./api";\nexport const handlers = { load: apiJson };\n'
_CALLS_MISSING = 'import { apiJson } from "./api";\nexport const missing = () => apiJson("/missing");\n'


def test_backend_only_routes_are_not_confirmed_when_a_frontend_call_was_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    _orphan_server(tmp_path, monkeypatch, typescript_toolchain, {"items.ts": _CALLS_ITEMS, "fetcher.ts": _UNREAD_CALL})
    data = j(mcp_server.route_unmatched(side="backend", output_format="json"))
    assert [r["path"] for r in data["backend_only"]] == ["/orders"]
    assert data["backend_only_confirmed"] is False
    assert [(g["kind"], g["file"], g["line"]) for g in data["backend_only_gaps"]] == [
        ("unparsed_frontend_callsite", "frontend/lib/fetcher.ts", 2)
    ]
    assert (data["frontend_only_confirmed"], data["frontend_only_gaps"]) == (None, None)
    text = mcp_server.route_unmatched(side="backend")
    assert "Backend-only routes, not confirmed (1):" in text
    assert (
        "⚠ Not confirmed: a route here may have a caller (frontend/lib/fetcher.ts:2 (unparsed_frontend_callsite))."
        in text
    )

    fallback = j(mcp_server.route_match(path="/orders", output_format="json"))
    assert [r["path"] for r in fallback["backend_only"]] == ["/orders"]
    assert fallback["backend_only_confirmed"] is False
    assert fallback["backend_only_gaps"] == data["backend_only_gaps"]
    fallback_text = mcp_server.route_match(path="/orders")
    assert "Backend routes with no frontend consumer found, not confirmed (1):" in fallback_text
    assert "Backend routes with no frontend consumer (1)" not in fallback_text

    matched = j(mcp_server.route_match(path="/items", output_format="json"))
    assert [m["backend"]["path"] for m in matched["matches"]] == ["/items"], "confirmed matches are kept"

    summary = j(mcp_server.route_match(output_format="json"))
    assert summary["summary"]["backend_only"] == 1
    assert (summary["backend_only_confirmed"], summary["frontend_only_confirmed"]) == (False, True)
    summary_text = mcp_server.route_match()
    assert "  Backend-only:    1 (at most; see below)" in summary_text
    assert (
        "⚠ Frontend calls may be missing from the scan (frontend/lib/fetcher.ts:2 (unparsed_frontend_callsite))."
        in (summary_text)
    )


def test_frontend_only_calls_are_not_confirmed_when_a_backend_file_was_not_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    _orphan_server(
        tmp_path,
        monkeypatch,
        typescript_toolchain,
        {"items.ts": _CALLS_ITEMS, "missing.ts": _CALLS_MISSING},
        backend_extra={"broken.py": "def (\n"},
    )
    data = j(mcp_server.route_unmatched(side="both", output_format="json"))
    assert [c["path"] for c in data["frontend_only"]] == ["/missing"]
    assert data["frontend_only_confirmed"] is False
    assert [(g["kind"], g["file"]) for g in data["frontend_only_gaps"]] == [
        ("parse_failed", "backend/service_backend/broken.py")
    ]
    assert data["backend_only_confirmed"] is True
    assert data["backend_only_gaps"] == data["frontend_only_gaps"], "a missed route can add a backend-only route"
    text = mcp_server.route_unmatched(side="both")
    assert "Backend-only routes, may be incomplete (1):" in text, "a route in broken.py may have no caller"
    assert (
        "⚠ May be incomplete: a route with no caller may be missing here "
        "(backend/service_backend/broken.py:1 (parse_failed))."
    ) in text
    # A mount in broken.py could also catch calls that look matched.
    assert (data["backend_only_count_bound"], data["frontend_only_count_bound"]) == ("at_least", "unbounded")
    assert "Frontend-only calls, not confirmed and may be incomplete (1):" in text
    assert "⚠ Not confirmed: a call here may have a route (backend/service_backend/broken.py:1 (parse_failed))." in text
    fallback = j(mcp_server.route_match(path="/missing", output_format="json"))
    assert (fallback["frontend_only_confirmed"], fallback["frontend_only_gaps"]) == (False, data["frontend_only_gaps"])
    assert (
        "Frontend calls with no backend route found, not confirmed and may be incomplete (1):"
        in mcp_server.route_match(path="/missing")
    )


def test_a_typescript_syntax_error_leaves_backend_only_routes_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    """TypeScript's recovered tree can swallow the rest of a file: the unclosed
    comment hides the `/orders` call, so `/orders` is not a confirmed orphan."""
    broken = _CALLS_ITEMS + '/* unclosed\nexport const orders = () => apiJson("/orders");\n'
    _orphan_server(tmp_path, monkeypatch, typescript_toolchain, {"items.ts": broken})
    data = j(mcp_server.route_unmatched(side="both", output_format="json"))
    assert [d["kind"] for d in data["diagnostics"]] == ["typescript_syntax_error"]
    assert [r["path"] for r in data["backend_only"]] == ["/orders"]
    assert data["backend_only_confirmed"] is False
    assert [(g["kind"], g["file"]) for g in data["backend_only_gaps"]] == [
        ("typescript_syntax_error", "frontend/lib/items.ts")
    ]
    assert data["frontend_only_confirmed"] is True, "no frontend-only call to confirm"
    assert data["frontend_only_count_bound"] == "at_least", "the swallowed call may have no route"
    assert "Backend-only routes, not confirmed and may be incomplete (1):" in mcp_server.route_unmatched(side="both"), (
        "a misread literal can also hide a route's caller elsewhere"
    )
    matched = j(mcp_server.route_match(path="/items", output_format="json"))
    assert [m["backend"]["path"] for m in matched["matches"]] == ["/items"], "calls before the error still match"
    assert j(mcp_server.route_match(output_format="json"))["backend_only_confirmed"] is False


# 32 URL variants, of which the evaluator keeps the first 16 (`/v1....`): the
# dropped `/v22222` is the URL of the route `add_route` registers.
_CALLS_TRUNCATED = (
    'import { apiJson } from "./api";\n'
    "export const pick = (a: boolean, b: boolean, c: boolean, d: boolean, e: boolean) =>\n"
    '  apiJson(`/v${a ? "1" : "2"}${b ? "1" : "2"}${c ? "1" : "2"}${d ? "1" : "2"}${e ? "1" : "2"}`);\n'
)
_MAIN_WITH_ADD_ROUTE = (
    _ORPHAN_MAIN_PY
    + """

async def page(request):
    return None


app.add_route("/v22222", page)
"""
)


def test_route_figures_are_not_bounded_when_both_a_call_and_a_route_may_be_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    """The truncated URL hides a call and `add_route` a route: a missed caller
    could lower the backend-only count while a missed route raises it, and the
    missed route joins the coverage denominator, so no figure they touch is a
    one-sided bound."""
    _orphan_server(
        tmp_path,
        monkeypatch,
        typescript_toolchain,
        {"items.ts": _CALLS_ITEMS, "pick.ts": _CALLS_TRUNCATED},
        backend_extra={"main.py": _MAIN_WITH_ADD_ROUTE},
    )
    data = j(mcp_server.route_match(output_format="json"))
    both = {
        ("url_variants_truncated", "frontend/lib/pick.ts"),
        ("unsupported_route_registration", "backend/service_backend/main.py"),
    }
    assert {(g["kind"], g["file"]) for g in data["backend_only_gaps"]} == both
    assert {(g["kind"], g["file"]) for g in data["frontend_only_gaps"]} == both
    s = data["summary"]
    # Each kept variant of the truncated URL is a frontend-only row.
    assert (s["backend_routes"], s["matched_routes"], s["backend_only"], s["frontend_only"]) == (2, 1, 1, 16)
    assert s["coverage_pct"] == 50.0
    assert data["summary_bounds"] == {
        "backend_routes": "at_least",
        "frontend_calls": "exact",
        "matched_routes": "unbounded",
        "matched_calls": "unbounded",
        "match_rows": "unbounded",
        "backend_only": "unbounded",
        "frontend_only": "unbounded",
        "coverage_pct": "unbounded",
    }
    assert (data["backend_only_count_bound"], data["frontend_only_count_bound"]) == ("unbounded", "unbounded")
    text = mcp_server.route_match()
    assert "  Backend-only:    1 (not bounded; see below)" in text
    assert "  Frontend-only:   16 (not bounded; see below)" in text
    assert "  Coverage:        50.0% (not bounded; see below)" in text
    assert "at most" not in text
    assert "  Frontend calls:  2 (test-file calls skipped: 0)\n" in text
    assert (
        "⚠ Some URL variants of a frontend call were dropped (frontend/lib/pick.ts:3 (url_variants_truncated))." in text
    )
    assert "⚠ Backend routes may be missing from the analysis (backend/service_backend/main.py:" in text

    unmatched = j(mcp_server.route_unmatched(side="both", output_format="json"))
    assert ([r["path"] for r in unmatched["backend_only"]], len(unmatched["frontend_only"])) == (["/orders"], 16)
    assert (unmatched["backend_only_count_bound"], unmatched["frontend_only_count_bound"]) == (
        "unbounded",
        "unbounded",
    )
    unmatched_text = mcp_server.route_unmatched(side="both")
    assert "Backend-only routes, not confirmed and may be incomplete (1):" in unmatched_text
    assert "Frontend-only calls, not confirmed and may be incomplete (16):" in unmatched_text
    assert "⚠ Not confirmed: a route here may have a caller (frontend/lib/pick.ts:3 (url_variants_truncated))." in (
        unmatched_text
    )
    assert "⚠ May be incomplete: a route with no caller may be missing here (backend/service_backend/main.py:" in (
        unmatched_text
    )
    [incomplete_calls] = [line for line in unmatched_text.splitlines() if "a call with no route may be missing" in line]
    assert "frontend/lib/pick.ts:3 (url_variants_truncated)" in incomplete_calls
    assert "(unsupported_route_registration)" in incomplete_calls, "a missed mount may catch a call"

    no_match = j(mcp_server.route_match(path="/v22222", output_format="json"))
    assert (no_match["match_count"], no_match["match_count_bound"]) == (0, "at_least")
    assert "⚠ May be incomplete: a match may be missing here (" in mcp_server.route_match(path="/v22222")
    by_handler = j(mcp_server.route_match(handler="list_orders", output_format="json"))
    assert by_handler["backend_only_count_bound"] == "unbounded"
    assert (by_handler["frontend_only_confirmed"], by_handler["frontend_only_count_bound"]) == (None, None), (
        "a handler filter does not consult the frontend side"
    )


_CALLS_API_ITEMS = 'import { apiJson } from "./api";\nexport const items = () => apiJson("/api/items");\n'
_CALLS_ANY_ITEMS = 'import { apiJson } from "./api";\nexport const any = (kind: string) => apiJson(`/${kind}/items`);\n'
_MAIN_UNRESOLVED_PREFIX = """\
import os

from fastapi import APIRouter, FastAPI

app = FastAPI(redirect_slashes=False)
router = APIRouter(prefix=os.environ["P"])


@router.get("/items")
def list_items():
    return []


app.include_router(router)
"""


def test_a_route_at_an_unresolved_prefix_is_not_a_confirmed_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    """With P=/api the route is `/api/items` and has the frontend caller, so
    neither orphan list is confirmed. The placeholder matches no call, not even
    one with a hole where it sits: with P=/api/v1 `/${kind}/items` cannot reach
    the route. The route may also shadow another at its real path."""
    _orphan_server(
        tmp_path,
        monkeypatch,
        typescript_toolchain,
        {"items.ts": _CALLS_API_ITEMS, "any.ts": _CALLS_ANY_ITEMS},
        backend_extra={"main.py": _MAIN_UNRESOLVED_PREFIX},
    )
    assert j(mcp_server.route_match(path="/items", match="suffix", output_format="json"))["matches"] == []
    data = j(mcp_server.route_unmatched(side="both", output_format="json"))
    assert [r["path"] for r in data["backend_only"]] == ['<unresolved: os.environ["P"]>/items']
    assert sorted(c["file"] for c in data["frontend_only"]) == ["frontend/lib/any.ts", "frontend/lib/items.ts"]
    assert (data["backend_only_confirmed"], data["backend_only_count_bound"]) == (False, "unbounded")
    assert (data["frontend_only_confirmed"], data["frontend_only_count_bound"]) == (False, "at_most")
    text = mcp_server.route_unmatched(side="both")
    assert "Backend-only routes, not confirmed and may be incomplete (1):" in text
    assert (
        "⚠ Not confirmed: a route here may have a caller (backend/service_backend/main.py:6 (unresolved_route_prefix))."
        in (text)
    )
    summary = j(mcp_server.route_match(output_format="json"))["summary_bounds"]
    assert (summary["backend_routes"], summary["matched_routes"], summary["coverage_pct"]) == (
        "exact",
        "at_least",
        "at_least",
    )
    assert "⚠ Backend routes sit at a path with a part the analysis could not evaluate (" in (mcp_server.route_match())


_MAIN_CUSTOM_ROUTER = """\
from fastapi import APIRouter, FastAPI

app = FastAPI(redirect_slashes=False)


class V2Router(APIRouter):
    def add_api_route(self, path, endpoint, **kwargs):
        super().add_api_route("/v2" + path, endpoint, **kwargs)


router = V2Router()


@router.get("/items")
def list_items():
    return []


app.include_router(router)
"""


def test_a_router_class_that_changes_registration_bounds_no_route_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain
) -> None:
    """V2Router serves `/v2/items`, so the analyzed match of `/items` is wrong:
    a match, a route or an orphan can be added or taken away."""
    _orphan_server(
        tmp_path,
        monkeypatch,
        typescript_toolchain,
        {"items.ts": _CALLS_ITEMS},
        backend_extra={"main.py": _MAIN_CUSTOM_ROUTER},
    )
    data = j(mcp_server.route_match(output_format="json"))
    s = data["summary"]
    assert (s["backend_routes"], s["matched_routes"], s["backend_only"], s["frontend_only"]) == (1, 1, 0, 0)
    assert data["summary_bounds"] == {
        "backend_routes": "unbounded",
        "frontend_calls": "exact",
        "matched_routes": "unbounded",
        "matched_calls": "at_most",
        "match_rows": "unbounded",
        "backend_only": "at_least",
        "frontend_only": "at_least",
        "coverage_pct": "at_most",
    }
    text = mcp_server.route_match()
    assert "  Frontend-only:   0 (at least; see below)" in text
    assert "  Coverage:        100.0% (at most; see below)" in text
    assert "⚠ Backend routes may be added, moved or removed in ways the analysis does not model (" in text
    matched = j(mcp_server.route_match(path="/items", output_format="json"))
    assert (matched["match_count"], matched["match_count_bound"]) == (1, "unbounded")
    assert "⚠" not in mcp_server.route_match(path="/items"), "a found match makes no claim of absence"


# ── doc_refs ─────────────────────────────────────────────────────


def _doc_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **runtime_kwargs) -> SimpleNamespace:
    s = _server(tmp_path, monkeypatch, **runtime_kwargs)
    doc_lookup_repo(s.repo)
    s.fakes.di = di_result(routes=[doc_lookup_route()])
    s.fakes.py_symbols = list(DOC_LOOKUP_SYMBOLS)
    return s


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return _doc_server(tmp_path, monkeypatch)


_DOC_REFS_INVALID = [
    ({}, "file", None),
    ({"file": "a.py", "symbol": "X"}, "symbol", "X"),
    ({"route": "/x", "doc": "docs/a.md"}, "doc", "docs/a.md"),
    ({"route": "FETCH /x"}, "route", "FETCH /x"),
    ({"route": "appointments"}, "route", "appointments"),
    ({"route": "GET /x extra"}, "route", "GET /x extra"),
    ({"doc": "docs/x.txt"}, "doc", "docs/x.txt"),
    ({"doc": ""}, "doc", ""),
    ({"file": "../x"}, "file", "../x"),
    ({"file": "docs/../../x"}, "file", "docs/../../x"),
    ({"file": "/etc/passwd"}, "file", "/etc/passwd"),
    ({"file": "."}, "file", "."),
    ({"file": "a\x00b"}, "file", "a\x00b"),
    ({"symbol": " \t"}, "symbol", " \t"),
    ({"symbol": "a b"}, "symbol", "a b"),
    ({"symbol": "x()"}, "symbol", "x()"),
    ({"doc": "docs/a.md", "limit": -1}, "limit", -1),
]


@pytest.mark.parametrize(("arguments", "argument", "value"), _DOC_REFS_INVALID)
def test_doc_refs_rejects_invalid_arguments_before_analysis(
    docs: SimpleNamespace, arguments: dict, argument: str, value
) -> None:
    payload = j(mcp_server.doc_refs(**arguments, output_format="json"))
    assert (payload["error"], payload["argument"], payload["value"]) == ("invalid_argument", argument, value)
    wire = _call_over_the_wire("doc_refs", {**arguments, "output_format": "json"})
    assert wire.is_error is True
    over = j(_text(wire))
    assert (over["error"], over["argument"], over["value"]) == ("invalid_argument", argument, value)
    assert docs.fakes.calls == {}, "no analysis may run for an invalid argument"


def test_doc_refs_names_the_rule_an_argument_breaks(docs: SimpleNamespace) -> None:
    def message(**arguments) -> str:
        return j(mcp_server.doc_refs(**arguments, output_format="json"))["message"]

    assert message().endswith("give exactly one of file, route, symbol or doc.")
    assert "with METHOD one of GET, POST" in message(route="FETCH /x")
    assert message(doc="docs/x.txt").endswith("must be a Markdown file ending in .md.")
    assert message(file="/etc/passwd").endswith("is outside the repository; give a repository-relative path.")
    assert message(file="../x").endswith("escapes the repository; give a repository-relative path.")
    assert message(file="a\x00b").endswith("must not contain a NUL byte.")


def test_doc_refs_refuses_an_unknown_argument(docs: SimpleNamespace) -> None:
    result = _call_over_the_wire("doc_refs", {"doc": "docs/a.md", "broken_only": True, "output_format": "json"})
    assert result.is_error is True
    payload = j(_text(result))
    assert (payload["error"], payload["argument"]) == ("invalid_argument", "broken_only")
    assert set(payload["allowed"]) == {"file", "route", "symbol", "doc", "limit", "output_format"}
    assert docs.fakes.calls == {}


def test_doc_refs_answers_each_query_kind(docs: SimpleNamespace) -> None:
    route = mcp_server.doc_refs(route="POST /items/{id}/checkout")
    assert f"Live route: POST /items/{{item_id}}/checkout  checkout()  [{DOC_LOOKUP_ROUTE_FILE}:1]" in route
    assert "  docs/a.md:5  route  POST /items/{id}/checkout  ok" in route
    absolute = j(mcp_server.doc_refs(file=str(docs.repo / DOC_LOOKUP_ROUTE_FILE), output_format="json"))
    assert absolute["query"] == {"kind": "file", "value": DOC_LOOKUP_ROUTE_FILE, "method": None}
    assert absolute["mention_count"] == 8
    symbol = j(mcp_server.doc_refs(symbol="Holder", output_format="json"))
    assert (symbol["definition_count"], symbol["mention_count"], symbol["complete"]) == (2, 2, True)
    doc = j(mcp_server.doc_refs(doc="./docs/a.md", output_format="json"))
    assert (doc["query"]["value"], doc["inbound_count"], doc["reachable"]) == ("docs/a.md", 3, True)


class _StaticPages:
    def pages(self, tracked: frozenset[str]) -> PageSet:
        return PageSet((FrontendPage(("items", "new"), "/items/new", "frontend/src/App.tsx", 12),))


def test_doc_refs_resolves_a_route_mention_to_a_frontend_page(docs: SimpleNamespace) -> None:
    write(docs.repo / "docs" / "c.md", "# C\n\nOpen `/items/new` to add one.\n")
    write(docs.repo / "docs" / "README.md", "# Docs\n\n[A](a.md)\n[B](b.md)\n[C](c.md)\n")
    docs.fakes.page_provider = _StaticPages()
    payload = j(mcp_server.doc_refs(route="/items/new", output_format="json"))
    assert [(m["file"], m["status"]) for m in payload["mentions"]] == [("docs/c.md", "frontend")]
    assert payload["complete"] is True


def test_doc_refs_replies_share_one_key_set(docs: SimpleNamespace) -> None:
    replies = [
        j(mcp_server.doc_refs(**cast(dict[str, Any], query), output_format="json"))
        for query in (
            {"file": DOC_LOOKUP_ROUTE_FILE},
            {"route": "/items/{id}/checkout"},
            {"symbol": "Holder"},
            {"doc": "docs/a.md"},
            {"route": "DELETE /nothing"},
        )
    ]
    assert len({frozenset(r) for r in replies}) == 1
    assert replies[-1]["mention_count"] == 0 and replies[-1]["definitions"] == []


def test_doc_refs_route_gap_limitations_keep_the_reply_complete(docs: SimpleNamespace) -> None:
    docs.fakes.di = di_result(routes=[doc_lookup_route()], limitations=[limitation()])
    text = mcp_server.doc_refs(route="/items/{id}/checkout")
    assert text.startswith("ⓘ 1 analysis limitation(s) (opaque_router_call)")
    assert "INCOMPLETE" not in text
    payload = j(mcp_server.doc_refs(route="/items/{id}/checkout", output_format="json"))
    assert payload["complete"] is True
    assert [lim["kind"] for lim in payload["analysis_limitations"]] == ["opaque_router_call"]


def test_doc_refs_an_unresolved_app_makes_the_reply_incomplete(docs: SimpleNamespace) -> None:
    docs.fakes.di = di_result(routes=[], complete=False, diagnostics=[unresolved_app()])
    payload = j(mcp_server.doc_refs(route="/items/{id}/checkout", output_format="json"))
    assert payload["complete"] is False
    assert [(d["kind"], d["file"]) for d in payload["diagnostics"]] == [("unresolved_app", "service_backend.main")]


def test_doc_refs_a_doc_that_cannot_be_read_makes_the_reply_incomplete(
    docs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_intel import code_paths

    real = code_paths.read_file_bytes

    def read(path: Path) -> bytes:
        if path.relative_to(docs.repo).as_posix() == "docs/b.md":
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(code_paths, "read_file_bytes", read)
    payload = j(mcp_server.doc_refs(doc="docs/a.md", output_format="json"))
    assert payload["complete"] is False
    assert [(d["kind"], d["file"]) for d in payload["diagnostics"]] == [("read_failed", "docs/b.md")]
    assert mcp_server.doc_refs(doc="docs/a.md").startswith("⚠ INCOMPLETE")


def test_doc_refs_needs_frontend_definitions_only_for_symbols_and_frontend_files(docs: SimpleNamespace) -> None:
    docs.fakes.ts_error = ts_bridge.ToolchainProblem(
        "typescript_not_found", "typescript not found", "ts-cache/node_modules", "code-intel setup"
    )
    symbol = j(mcp_server.doc_refs(symbol="Holder", output_format="json"))
    assert symbol["complete"] is False
    assert [d["kind"] for d in symbol["diagnostics"]] == ["frontend_unavailable"]
    frontend_file = j(mcp_server.doc_refs(file="frontend/lib/api.ts", output_format="json"))
    assert frontend_file["complete"] is False
    for query in ({"route": "/items/{id}/checkout"}, {"file": DOC_LOOKUP_ROUTE_FILE}, {"doc": "docs/a.md"}):
        payload = j(mcp_server.doc_refs(**cast(dict[str, Any], query), output_format="json"))
        assert payload["complete"] is True, query
        assert "frontend_definitions_unavailable" in {lim["kind"] for lim in payload["analysis_limitations"]}


def test_doc_refs_refuses_on_an_old_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _doc_server(tmp_path, monkeypatch, version_info=(3, 12))
    payload = j(mcp_server.doc_refs(doc="docs/a.md", output_format="json"))
    assert payload["error"] == "runtime_incompatible"
    assert s.fakes.calls == {}


def test_doc_refs_a_missing_config_is_an_analysis_failure(docs: SimpleNamespace) -> None:
    (docs.repo / ".code-intel" / "config.toml").unlink()
    payload = j(mcp_server.doc_refs(doc="docs/a.md", output_format="json"))
    assert (payload["error"], payload["analyzer"], payload["step"]) == ("analysis_failed", "config", "read config")
    assert payload["missing"] == ".code-intel/config.toml"
    status = mcp_server.status()
    assert "Config error:" in status and ".code-intel/config.toml" in status


def test_doc_refs_internal_errors_name_the_tool_and_frame(docs: SimpleNamespace, monkeypatch, caplog) -> None:
    monkeypatch.setattr(mcp_server.doc_queries, "doc_refs_lookup", lambda *a, **k: 1 / 0)
    with caplog.at_level("ERROR", logger="server"):
        payload = j(mcp_server.doc_refs(doc="docs/a.md", output_format="json"))
    assert (payload["error"], payload["tool"]) == ("internal_error", "doc_refs")
    assert "failed unexpectedly at test_mcp_tools.py:" in payload["message"]
    assert "ZeroDivisionError" in payload["message"]


# ── Pin file and config edits after start ────────────────────────────────


def test_a_pin_bump_after_start_puts_the_restart_banner_on_every_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    pin = write(repo / ".code-intel" / "requirements.txt", "code-intel @ git+https://example.invalid/x@aaa\n")
    _server(tmp_path, monkeypatch, pin_files=[pin])
    assert not mcp_server.di_deps().startswith("⚠")
    write(pin, "code-intel @ git+https://example.invalid/x@bbb\n", 3000)
    assert mcp_server.di_deps().startswith(presenters.STALE_CODE_WARNING)
    assert mcp_server.jsx_usage("X", root="../..").startswith(presenters.STALE_CODE_WARNING), "errors too"
    assert j(mcp_server.status(output_format="json"))["analyzer_code_stale"] is True


def test_a_config_edit_changes_the_next_reply_without_a_restart(server: SimpleNamespace) -> None:
    server.fakes.usages = [FakeUsage("frontend/a.tsx", 4)]
    mcp_server.jsx_usage("Widget")
    assert server.fakes.jsx_calls[-1]["root"] == (server.repo / "frontend").resolve()
    write(server.repo / "web" / "a.tsx", "export {};\n")
    config = server.repo / ".code-intel" / "config.toml"
    write(config, config.read_text(encoding="utf-8").replace('root = "frontend"', 'root = "web"'), 5000)
    mcp_server.jsx_usage("Widget")
    assert server.fakes.jsx_calls[-1]["root"] == (server.repo / "web").resolve()
    assert server.fakes.jsx_calls[-1]["frontend_root"] == "web"
    assert "Config:         .code-intel/config.toml" in mcp_server.status()
