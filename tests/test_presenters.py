"""Tests for `presenters`: the envelope, error bodies and per-tool text/JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from mcp_fixtures import (
    DOC_LOOKUP_OTHER_FILE,
    DOC_LOOKUP_ROUTE_FILE,
    FakeCandidate,
    FakeSymbolResult,
    FakeUsage,
    backend_side,
    dep,
    di_result,
    di_route,
    doc_lookup_report,
    frontend_side,
    limitation,
    pair,
    read_failed,
    routes_result,
    use,
)

from code_intel import doc_presenters, doc_queries, presenters, problems, queries
from code_intel.analysis_runtime import StatusSnapshot
from code_intel.protocol_match import ProtocolMatch

README = Path(__file__).resolve().parent.parent / "README.md"


# ── Envelope ─────────────────────────────────────────────────────────────


def test_envelope_stacks_banners_and_stamps_the_payload() -> None:
    body = presenters.Body("BODY", {"x": 1})
    diag = read_failed()
    lim = limitation()
    analysis = {"complete": False, "diagnostics": [diag], "analysis_limitations": [lim]}
    text = presenters.envelope(body, "text", stale=True, analysis=analysis)
    parts = text.split("\n\n")
    assert parts[0] == presenters.STALE_CODE_WARNING
    assert parts[1] == "⚠ INCOMPLETE — 1 file could not be read; counts may be undercounts."
    assert parts[2].startswith("ⓘ 1 analysis limitation(s) (opaque_router_call)")
    assert parts[3] == "BODY"

    payload = json.loads(presenters.envelope(body, "json", stale=True, analysis=analysis))
    assert payload == {
        "x": 1,
        "schema_version": 2,
        "complete": False,
        "diagnostics": [diag],
        "analysis_limitations": [lim],
        "analyzer_code_stale": True,
        "analyzer_code_unconfirmed": [],
    }
    assert body.payload == {"x": 1}, "the envelope must not mutate the body"


def test_envelope_success_bodies_always_carry_the_contract_keys() -> None:
    """Every success body has schema_version, complete, diagnostics and limitations."""
    body = presenters.Body("BODY", {"x": 1})
    assert presenters.envelope(body, "text", stale=False) == "BODY"
    assert json.loads(presenters.envelope(body, "json", stale=False)) == {
        "x": 1,
        "schema_version": 2,
        "complete": True,
        "diagnostics": [],
        "analysis_limitations": [],
        "analyzer_code_stale": False,
        "analyzer_code_unconfirmed": [],
    }
    unknown = json.loads(presenters.envelope(body, "json", stale=False, analysis={"complete": None}))
    assert unknown["complete"] is None


def test_envelope_accepts_problem_objects() -> None:
    diag = problems.diagnostic(
        "parse_failed", stage="s", step="parse file", file="a.py", message="a.py: could not parse"
    )
    payload = json.loads(
        presenters.envelope(
            presenters.Body("B", {}), "json", stale=False, analysis={"complete": False, "diagnostics": [diag]}
        )
    )
    assert payload["diagnostics"] == [diag.as_dict()]


def test_envelope_rejects_an_unknown_format() -> None:
    with pytest.raises(ValueError, match="xml"):
        presenters.envelope(presenters.Body("B", {}), "xml", stale=False)


_UNCONFIRMED = [{"file": "src/code_intel/queries.py", "reason": "modified within 2 s of the server loading it"}]


@pytest.mark.parametrize("body", [presenters.Body("BODY", {}), presenters.error_body("invalid_root", "BODY", root="x")])
def test_envelope_reports_unconfirmed_code_as_unknown_not_stale(body: presenters.Body) -> None:
    """A file that cannot be compared is an explicit uncertainty naming the
    file and why, never the stale warning."""
    text = presenters.envelope(body, "text", stale=False, unconfirmed=_UNCONFIRMED)
    assert text == (
        "⚠ Cannot confirm that the analyzer code this server loaded matches the files on disk; restart the "
        "code-intel MCP server in your client to be sure. Unconfirmed: src/code_intel/queries.py (modified within "
        "2 s of the server loading it).\n\nBODY"
    )
    assert presenters.STALE_CODE_WARNING not in text
    payload = json.loads(presenters.envelope(body, "json", stale=False, unconfirmed=_UNCONFIRMED))
    assert payload["analyzer_code_stale"] is None
    assert payload["analyzer_code_unconfirmed"] == _UNCONFIRMED


def test_envelope_a_confirmed_change_outranks_unconfirmed_files() -> None:
    body = presenters.Body("BODY", {})
    text = presenters.envelope(body, "text", stale=True, unconfirmed=_UNCONFIRMED)
    assert text == f"{presenters.STALE_CODE_WARNING}\n\nBODY"
    payload = json.loads(presenters.envelope(body, "json", stale=True, unconfirmed=_UNCONFIRMED))
    assert (payload["analyzer_code_stale"], payload["analyzer_code_unconfirmed"]) == (True, _UNCONFIRMED)


def test_stale_warning_is_client_neutral() -> None:
    """The restart advice names no client command."""
    assert "restart the code-intel MCP server in your client" in presenters.STALE_CODE_WARNING
    assert "/mcp" not in presenters.STALE_CODE_WARNING
    assert "restart the code-intel MCP server in your client" in presenters.UNCONFIRMED_CODE_WARNING
    assert "/mcp" not in presenters.UNCONFIRMED_CODE_WARNING


def test_incomplete_banner_groups_diagnostics_by_step() -> None:
    """The banner says what was lost per step, not "read or parsed"."""
    diags = [
        read_failed("a.py"),
        read_failed("b.py"),
        problems.diagnostic(
            "list_failed", stage="s", step="list directory", file="d", exc=PermissionError("x")
        ).as_dict(),
    ]
    text = presenters.envelope(
        presenters.Body("B", {}), "text", stale=False, analysis={"complete": False, "diagnostics": diags}
    )
    assert text.startswith("⚠ INCOMPLETE — 2 files could not be read, 1 directory could not be listed;")
    assert "router bindings" not in text
    assert "read or parsed" not in text


# ── Error bodies ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "fields"),
    [
        ("invalid_argument", {"argument": "side", "value": "x", "allowed": ["backend"]}),
        ("invalid_root", {"root": "../x"}),
        ("runtime_incompatible", {"running": "3.12", "required": "3.14"}),
        ("analysis_failed", {"analyzer": "di_map", "step": "build router graph"}),
        ("analysis_failed", {"analyzer": "di_map", "step": "build router graph", "missing": "backend/service_backend"}),
        ("frontend_unavailable", {"missing": "node", "fix": "install node", "problem": "node_not_found"}),
        ("internal_error", {"tool": "di_deps"}),
    ],
)
def test_error_body_shape_per_code(code: str, fields: dict) -> None:
    body = presenters.error_body(cast(Any, code), "what failed", **fields)
    assert body.text == "what failed"
    assert body.payload == {"schema_version": 2, "error": code, "message": "what failed", **fields}
    text = presenters.envelope(body, "text", stale=True)
    assert text == f"{presenters.STALE_CODE_WARNING}\n\nwhat failed"
    payload = json.loads(presenters.envelope(body, "json", stale=True))
    assert payload["analyzer_code_stale"] is True
    assert "complete" not in payload, "nothing was analyzed, so an error makes no completeness claim"


def test_error_body_rejects_unknown_codes_and_fields() -> None:
    with pytest.raises(ValueError, match="bogus"):
        presenters.error_body(cast(Any, "bogus"), "m")
    with pytest.raises(ValueError, match="unexpected fields"):
        presenters.error_body("invalid_root", "m", root="x", extra=1)
    with pytest.raises(ValueError, match="missing fields"):
        presenters.error_body("analysis_failed", "m", analyzer="di_map")


def test_runtime_refusal_names_versions_and_what_still_works() -> None:
    body = presenters.runtime_refusal("3.12", "3.14")
    assert body.payload["error"] == "runtime_incompatible"
    assert (body.payload["running"], body.payload["required"]) == ("3.12", "3.14")
    assert "Python 3.12 is older than the backend's 3.14" in body.text
    assert "JSX usage, frontend symbols and status keep working" in body.text


# ── DI ───────────────────────────────────────────────────────────────────

GET_DB = "service_backend.core.database:get_db"
PERM = "service_backend.core.auth:require_any_permission"


def _di():
    return di_result(
        [dep(GET_DB, line=83), dep(PERM, kind="factory_result", sub=[GET_DB], aliases=["_require_list_access"])],
        [
            di_route(
                "list_locations",
                "/locations",
                line=166,
                deps=[
                    use(GET_DB, "get_db", "app"),
                    use(PERM, "require_any_permission(…)", "route decorator", alias="_require_list_access"),
                    use(GET_DB, "get_db", "direct", param="db"),
                ],
            )
        ],
    )


def test_di_routes_text_lists_every_layer_and_guard_aliases() -> None:
    result = queries.di_deps(_di(), handler="list_locations", path=None, method=None, match="exact", limit=None)
    text = presenters.di_deps(result).text
    assert text.splitlines()[0] == f"GET /locations  →  list_locations()  [{di_route('x', '/').get('file')}:166]"
    assert "  App deps:     get_db" in text
    assert "  Router deps:  (none)" in text
    assert "  Route deps:   require_any_permission(…) [as _require_list_access]" in text
    assert "  Direct deps:  get_db" in text
    assert "  All deps:     get_db, require_any_permission" in text


def test_dependency_listing_text_shows_kind_aliases_and_sub_deps() -> None:
    result = queries.di_deps(_di(), handler=None, path=None, method=None, match="exact", limit=None)
    text = presenters.di_deps(result).text
    assert (
        "  require_any_permission (factory_result)  [backend/service_backend/deps.py:1]  [as _require_list_access]  -> get_db"
        in text
    )
    assert "  get_db  [backend/service_backend/deps.py:83]  -> (leaf)" in text


def test_di_reverse_text_shows_alias_and_name_match_note() -> None:
    result = queries.di_reverse(_di(), "_require_list_access", limit=None)
    body = presenters.di_reverse(result)
    assert body.text.startswith("Matched on alias '_require_list_access'")
    assert "(route decorator: require_any_permission(…) [as _require_list_access])" in body.text
    assert body.payload["groups"][0]["dependency"]["id"] == PERM
    assert body.payload["route_count"] == 1


def test_di_reverse_unknown_suggests_or_points_to_the_listing() -> None:
    unknown = presenters.di_reverse(queries.di_reverse(_di(), "zzz", limit=None))
    assert "call di_deps() with no arguments" in unknown.text
    assert unknown.payload["suggestions"] == []
    close = presenters.di_reverse(queries.di_reverse(_di(), "get_dbb", limit=None))
    assert "Did you mean get_db?" in close.text


# ── Routes ───────────────────────────────────────────────────────────────


def test_route_match_text_uses_def_and_call_lines() -> None:
    """Frontend rows read `[file def D · call C]`, collapsing when they coincide."""
    data = routes_result(
        [
            pair("/user-files", "upload_user_file", "uploadUserFile", line=2348, def_line=2328),
            pair("/one", "one", "oneLiner", line=4, def_line=4),
            pair(
                "/x/changes/export",
                "export_changes",
                "useAuditFilterTab",
                confidence="possible",
                reason="hole_on_literal",
            ),
        ]
    )
    text = presenters.route_match(
        queries.route_match(data, path=None, handler=None, frontend_func="uploadUserFile", match="exact", limit=None)
    ).text
    assert "uploadUserFile()  [frontend/lib/api.ts def 2328 · call 2348] via apiJson" in text
    one = presenters.route_match(
        queries.route_match(data, path=None, handler=None, frontend_func="oneLiner", match="exact", limit=None)
    ).text
    assert "oneLiner()  [frontend/lib/api.ts:4]" in one
    possible = presenters.route_match(
        queries.route_match(data, path="/x/changes/export", handler=None, frontend_func=None, match="exact", limit=None)
    ).text
    assert possible.startswith("GET /x/changes/export  (possible: hole_on_literal)")


def test_route_match_json_nests_backend_and_frontend() -> None:
    """Match rows nest `backend` and `frontend`; no `backend_line`/`frontend_def_line`."""
    data = routes_result([pair("/a", "a", "fa", line=9, def_line=7)])
    payload = presenters.route_match(
        queries.route_match(data, path="/a", handler=None, frontend_func=None, match="exact", limit=None)
    ).payload
    row = payload["matches"][0]
    assert set(row) == {"backend", "frontend", "confidence", "reason"}
    assert (row["frontend"]["line"], row["frontend"]["def_line"]) == (9, 7)
    assert "backend_line" not in json.dumps(payload)


def test_route_summary_text_splits_routes_calls_and_rows() -> None:
    data = routes_result([pair("/a", "a", "fa"), pair("/a", "a", "fb", line=11)])
    text = presenters.route_match(
        queries.route_match(data, path=None, handler=None, frontend_func=None, match="exact", limit=None)
    ).text
    assert "  Matched routes:  1" in text
    assert "  Matched calls:   2" in text
    assert "  Match rows:      2" in text
    assert "  Coverage:        100.0%" in text


# ── JSX and symbols ──────────────────────────────────────────────────────


def test_jsx_text_shows_column_tag_kind_and_filters() -> None:
    """Rows carry column and tag kind; the header states the filters."""
    usages = [FakeUsage("frontend/a.tsx", 3, column=7, tag_kind="open", parent_function="Page")]
    result = queries.jsx_usages(
        "Widget", usages, limit=None, root="frontend", include_self_close=False, include_tests=True
    )
    text = presenters.jsx_usage(result).text
    assert text.startswith(
        "Found 1 JSX usages of <Widget> across 1 file(s) (self-closing tags excluded, test files included):"
    )
    assert "  frontend/a.tsx:3:7  open in Page" in text
    payload = presenters.jsx_usage(result).payload
    assert payload["usages"][0]["tag_kind"] == "open"
    assert payload["include_self_close"] is False


def test_jsx_text_lists_file_problems_by_name() -> None:
    result = queries.jsx_usages(
        "Widget",
        [],
        limit=None,
        root="frontend",
        include_self_close=True,
        include_tests=False,
        found_problems=[read_failed("frontend/a.tsx")],
    )
    text = presenters.jsx_usage(result).text
    assert "No JSX usages of <Widget> found under frontend/ (test files excluded)." in text
    assert "[read_failed] frontend/a.tsx: frontend/a.tsx: could not read file (PermissionError" in text


def test_symbol_rows_use_line_and_end_line_and_scope() -> None:
    found = FakeSymbolResult(
        [
            FakeCandidate(
                "DataTable",
                "frontend/components/ui/data-table.tsx",
                45,
                120,
                "component",
                "export function DataTable()",
                "typescript",
                score=1.0,
            )
        ]
    )
    result = queries.symbol_resolution("DataTable", found, side="both", kind=None, file_hint="ui", limit=None)
    body = presenters.symbol_resolution(result)
    row = body.payload["candidates"][0]
    assert (row["line"], row["end_line"], row["scope"], row["file_hint_match"]) == (45, 120, "module", True)
    assert "  1. component  frontend/components/ui/data-table.tsx:45-120" in body.text
    assert "     score 1.00 (file_hint match)" in body.text


def _protocol_symbols() -> FakeSymbolResult:
    gateway = ProtocolMatch("Gateway", "service_backend.gateway:Gateway", "backend/service_backend/gateway.py", 7)
    return FakeSymbolResult(
        [
            FakeCandidate(
                "fetch",
                "backend/service_backend/gateway.py",
                7,
                7,
                "method",
                "def fetch(self) -> int: ...",
                "python",
                scope="class",
                container="Gateway",
                protocol_method=True,
            ),
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
            ),
            FakeCandidate(
                "fetch", "backend/other.py", 1, 2, "method", "def fetch(self):", "python", scope="class", container="O"
            ),
        ]
    )


def test_symbol_text_labels_protocol_methods_and_lists_the_protocols_a_class_matches() -> None:
    result = queries.symbol_resolution(
        "fetch", _protocol_symbols(), side="backend", kind=None, file_hint=None, limit=None
    )
    lines = presenters.symbol_resolution(result).text.splitlines()
    assert "  1. method     backend/service_backend/gateway.py:7 in Gateway (Protocol)" in lines
    assert "  2. method     backend/service_backend/impl.py:3-4 in Impl" in lines
    assert (
        "     calls typed as a Protocol it matches (by member names) resolve to: "
        "Gateway.fetch (backend/service_backend/gateway.py:7)"
    ) in lines
    assert sum("calls typed as a Protocol" in line for line in lines) == 1, "only the matching class gets the line"


def test_symbol_json_adds_protocol_fields_to_every_candidate() -> None:
    result = queries.symbol_resolution(
        "fetch", _protocol_symbols(), side="backend", kind=None, file_hint=None, limit=None
    )
    rows = presenters.symbol_resolution(result).payload["candidates"]
    assert [(r["protocol_method"], r["protocols"]) for r in rows] == [
        (True, []),
        (
            False,
            [
                {
                    "name": "Gateway",
                    "id": "service_backend.gateway:Gateway",
                    "file": "backend/service_backend/gateway.py",
                    "line": 7,
                }
            ],
        ),
        (False, []),
    ]
    typescript = FakeSymbolResult([FakeCandidate("X", "frontend/x.ts", 1, 1, "function", "function X()", "typescript")])
    [row] = presenters.symbol_resolution(
        queries.symbol_resolution("X", typescript, side="both", kind=None, file_hint=None, limit=None)
    ).payload["candidates"]
    assert (row["protocol_method"], row["protocols"]) == (False, [])


def test_symbol_text_reports_a_missing_frontend() -> None:
    found = FakeSymbolResult([], complete=False, frontend_problem="node not found on PATH")
    result = queries.symbol_resolution("X", found, side="both", kind=None, file_hint=None, limit=None)
    assert "⚠ Frontend symbols missing: node not found on PATH" in presenters.symbol_resolution(result).text


# ── Status ───────────────────────────────────────────────────────────────


def _snapshot(**overrides) -> StatusSnapshot:
    base = dict(
        repo_root="/repo",
        tool_dir="/repo/src/code_intel",
        git_sha="abc",
        complete=None,
        code_stale=False,
        python="3.14.3",
        executable="/py",
        runtime_compatible=True,
        python_running="3.14",
        python_required="3.14",
        diagnostics=[],
        limitations=[],
        analyzers={
            "di_map": {
                "state": "not_run",
                "last_step": None,
                "last_success_at": None,
                "last_runtime_ms": None,
                "last_error": None,
            },
            "route_map": {
                "state": "not_run",
                "last_step": None,
                "last_success_at": None,
                "last_runtime_ms": None,
                "last_error": None,
            },
        },
        cache={
            "backend_model": {"loaded": False},
            "di": {"loaded": False, "fresh": False},
            "routes": {"loaded": False, "fresh": False},
        },
        toolchain={
            "state": "ok",
            "node": {"path": "/usr/bin/node", "version": "v24.21.0"},
            "typescript": {
                "dir": "ts-cache/node_modules/typescript",
                "version": "7.1.0-beta",
                "source": "helper",
                "lockfile_version": "7.1.0-beta",
            },
            "problem": None,
            "helper_failures": {},
            "warnings": [],
            "facts_cache": None,
        },
        counts=None,
    )
    base.update(overrides)
    return StatusSnapshot(**base)


def test_status_text_names_the_interpreter_only_when_incompatible() -> None:
    """A compatible interpreter is never blamed, even with diagnostics present."""
    ok = presenters.status(_snapshot(diagnostics=[read_failed()], complete=False)).text
    assert "older than" not in ok
    assert ok.startswith("⚠ INCOMPLETE — 1 file could not be read")
    old = presenters.status(_snapshot(runtime_compatible=False, python_running="3.12")).text
    assert "Python 3.12 is older than the backend's 3.14; DI, routes and backend symbols are refused" in old


def test_status_text_toolchain_lines() -> None:
    ok = presenters.status(_snapshot()).text
    assert "  Frontend toolchain: node v24.21.0, typescript 7.1.0-beta (ts-cache/node_modules/typescript): ok" in ok
    problem = {
        "code": "typescript_not_found",
        "message": "ts-cache/node_modules/typescript/package.json not found",
        "missing": "ts-cache/node_modules/typescript/package.json",
        "fix": 'run "code-intel setup" (or set CODE_INTEL_TYPESCRIPT_DIR)',
    }
    down = presenters.status(
        _snapshot(toolchain={**_snapshot().toolchain, "state": "unavailable", "problem": problem})
    ).text
    assert (
        "  Frontend toolchain: unavailable: ts-cache/node_modules/typescript/package.json not found. "
        'Fix: run "code-intel setup" (or set CODE_INTEL_TYPESCRIPT_DIR). DI tools are unaffected; '
        "JSX usage, frontend symbols and route matching need it."
    ) in down


def test_status_text_lists_each_outstanding_helper_failure_with_what_it_breaks() -> None:
    def crash(mode: str) -> dict:
        return {
            "code": "helper_failed",
            "message": f"helper.cjs: typescript helper ({mode} mode) failed at step 'exit': boom",
            "missing": None,
            "fix": "run the helper by hand",
        }

    failures = {"facts": crash("facts"), "routes": crash("routes")}
    toolchain = {**_snapshot().toolchain, "state": "helper_failed", "problem": failures["facts"]}
    text = presenters.status(_snapshot(toolchain={**toolchain, "helper_failures": failures})).text
    line = next(line for line in text.splitlines() if "Frontend toolchain:" in line)
    assert line == (
        "  Frontend toolchain: node v24.21.0, typescript 7.1.0-beta (ts-cache/node_modules/typescript), but "
        "the last TypeScript helper run in facts mode failed: helper.cjs: typescript helper (facts mode) "
        "failed at step 'exit': boom. Fix: run the helper by hand. JSX usage and frontend symbols fail until "
        "a facts-mode helper run succeeds. Also, the last TypeScript helper run in routes mode failed: "
        "helper.cjs: typescript helper (routes mode) failed at step 'exit': boom. Fix: run the helper by "
        "hand. Route matching fails until a routes-mode helper run succeeds."
    )
    only_routes = {**toolchain, "problem": failures["routes"], "helper_failures": {"routes": failures["routes"]}}
    routes_line = presenters.status(_snapshot(toolchain=only_routes)).text
    assert "JSX usage" not in routes_line
    assert "Route matching fails until a routes-mode helper run succeeds." in routes_line


def test_status_text_uses_failed_and_pluralizes() -> None:
    """The text says `failed` like the JSON, and pluralizes for two analyzers."""
    failed = {
        "state": "failed",
        "last_step": "build router graph",
        "last_success_at": None,
        "last_runtime_ms": None,
        "last_error": "boom",
    }
    text = presenters.status(_snapshot(complete=False, analyzers={"di_map": failed, "route_map": dict(failed)})).text
    assert text.startswith("⚠ INCOMPLETE — di_map failed (boom); route_map failed (boom); their results are missing")
    assert "  di_map: failed" in text
    assert " error " not in text
    one = presenters.status(
        _snapshot(complete=False, analyzers={"di_map": failed, "route_map": _snapshot().analyzers["route_map"]})
    ).text
    assert "its results are missing" in one


def test_status_counts_text_and_errors() -> None:
    counts = {
        "di": {"routes": 3, "http": 2, "websocket": 1, "dependency_functions": 4, "files_scanned": 5},
        "routes": None,
    }
    errors = {"routes": {"error": "frontend_unavailable", "message": "node not found on PATH"}}
    text = presenters.status(_snapshot(counts=counts, counts_errors=errors)).text
    assert "    DI: 3 routes (2 HTTP, 1 WebSocket), 4 dependency targets, 5 files" in text
    assert "    routes: failed: node not found on PATH" in text
    refused = presenters.status(_snapshot(counts_error={"error": "runtime_incompatible", "message": "too old"})).text
    assert "  Counts: not loaded: too old" in refused


# ── README ───────────────────────────────────────────────────────────────


def test_readme_documents_every_registered_problem_kind() -> None:
    """Every diagnostic and limitation kind an analyzer may emit is explained in the README."""
    text = README.read_text(encoding="utf-8")
    missing = [k for k in (*problems.DIAGNOSTIC_KINDS, *problems.LIMITATION_KINDS) if f"`{k}`" not in text]
    assert missing == []


def test_readme_documents_every_error_code() -> None:
    from code_intel.result_types import ERROR_FIELDS

    text = README.read_text(encoding="utf-8")
    assert [code for code in ERROR_FIELDS if f"`{code}`" not in text] == []


# ── Fix text and frontend problems ───────────────────────────────────────


def test_frontend_unavailable_does_not_repeat_a_fix_the_message_carries() -> None:
    body = presenters.frontend_unavailable(
        "frontend: root not found. Fix: check out frontend/", "frontend", "check out frontend/", "frontend_not_found"
    )
    assert body.text == "Frontend analysis unavailable: frontend: root not found. Fix: check out frontend/."
    assert body.payload["problem"] == "frontend_not_found"


def test_route_frontend_missing_body_lists_routes_without_an_orphan_heading() -> None:
    result = queries.RouteFrontendMissing(
        query=queries.RouteQuery(path="/v", handler=None, frontend_func=None, match="exact"),
        backend=queries.Page([{"method": "GET", "path": "/v", "handler": "v", "file": "b.py", "line": 3}], 1, 0),
        frontend_problem="node not found (missing: node). Fix: install node",
    )
    body = presenters.route_match(result)
    assert body.text.splitlines()[0] == "Frontend not analyzed: node not found (missing: node). Fix: install node"
    assert "Backend routes that fit the query (1):" in body.text
    assert "no frontend consumer" not in body.text
    assert body.payload["backend_only"] is None
    assert body.payload["backend_route_count_bound"] == "exact"


def test_route_frontend_missing_bounds_the_routes_it_lists() -> None:
    gap = {"kind": "asgi_mount", "file": "b.py", "line": 7, "message": "m", "stage": "build router graph"}
    gaps = queries.orphan_gaps({"diagnostics": [], "analysis_limitations": [gap]})
    query = queries.RouteQuery(path="/v", handler=None, frontend_func=None, match="exact")
    body = presenters.route_match(
        queries.RouteFrontendMissing(query, queries.Page([], 0, 0), "node not found", gaps=gaps)
    )
    assert body.payload["backend_route_count_bound"] == "at_least"
    assert body.text.endswith(
        "No backend route fits the query.\n⚠ May be incomplete: a route may be missing here (b.py:7 (asgi_mount))."
    )


def test_omitted_note_can_name_a_shared_limit() -> None:
    assert presenters.omitted_note(0, 1, "routes", limit_for_all=5) == (
        "... showing 0 of 1 routes; pass limit=5 (or null) to see all"
    )


def test_no_match_text_names_only_the_arguments_given() -> None:
    empty = queries.Page(items=[], total=0, omitted=0)
    di = presenters.di_deps(
        queries.DiRouteSelection(queries.DiRouteQuery("list_articles", None, "PUT", "exact"), empty, [])
    )
    assert di.text == "No route found matching handler=list_articles, method=PUT (match=exact)."
    route = presenters.route_match(queries.RouteNoMatch(queries.RouteQuery("/ghost", None, None, "suffix"), []))
    assert route.text == "No match found for path=/ghost (match=suffix)."


def test_route_summary_marks_counts_a_frontend_gap_cannot_confirm() -> None:
    """Truncated URL variants keep the result complete, so coverage is a number,
    but a missed caller could only raise it."""
    gaps = [
        {"kind": "url_variants_truncated", "file": f"frontend/lib/f{i}.ts", "line": 1, "message": "m"} for i in range(7)
    ]
    data = routes_result(
        [pair("/a", "a", "fa")],
        backend_only=[backend_side("GET", "/b", "b")],
        limitations=[{**g, "stage": "frontend routes"} for g in gaps],
    )
    data["summary"]["coverage_pct"] = 50.0
    body = presenters.route_match(
        queries.route_match(data, path=None, handler=None, frontend_func=None, match="exact", limit=None)
    )
    assert "  Backend routes:  2\n" in body.text
    assert "  Frontend calls:  1 (test-file calls skipped: 0)\n" in body.text, "truncation drops URLs, not calls"
    assert "  Matched routes:  1 (at least; see below)" in body.text
    assert "  Backend-only:    1 (at most; see below)" in body.text
    assert "  Frontend-only:   0 (at least; see below)\n" in body.text, "a missed call may have no route"
    assert "  Coverage:        50.0% (at least; see below)" in body.text
    assert body.text.endswith(
        "\n  ⚠ Some URL variants of a frontend call were dropped "
        "(frontend/lib/f0.ts:1 (url_variants_truncated), frontend/lib/f1.ts:1 (url_variants_truncated), "
        "frontend/lib/f2.ts:1 (url_variants_truncated), frontend/lib/f3.ts:1 (url_variants_truncated), "
        "frontend/lib/f4.ts:1 (url_variants_truncated) and 2 more (see output_format='json'))."
    )
    rows = [{k: g[k] for k in ("kind", "file", "line", "message")} for g in gaps]
    assert (body.payload["backend_only_confirmed"], body.payload["backend_only_gaps"]) == (False, rows)
    assert (body.payload["frontend_only_confirmed"], body.payload["frontend_only_gaps"]) == (True, rows)
    assert body.payload["summary_bounds"]["coverage_pct"] == "at_least"
    assert (body.payload["backend_only_count_bound"], body.payload["frontend_only_count_bound"]) == (
        "at_most",
        "at_least",
    )


def test_route_summary_bounds_nothing_a_missing_backend_route_can_move() -> None:
    """A route the analysis missed is in no figure: it adds to the routes and
    may be backend-only, and joins the coverage denominator matched or not."""
    gap = {"kind": "unsupported_route_registration", "file": "backend/m.py", "line": 9, "message": "m"}
    data = routes_result(
        [pair("/a", "a", "fa")],
        backend_only=[backend_side("GET", "/b", "b")],
        frontend_only=[frontend_side("GET", "/c", "fc", line=20)],
        limitations=[{**gap, "stage": "build router graph"}],
    )
    data["summary"]["coverage_pct"] = 50.0
    body = presenters.route_match(
        queries.route_match(data, path=None, handler=None, frontend_func=None, match="exact", limit=None)
    )
    assert "  Backend routes:  2 (at least; see below)" in body.text
    assert "  Frontend calls:  2 (test-file calls skipped: 0)\n" in body.text
    assert "  Backend-only:    1 (at least; see below)" in body.text
    assert "  Frontend-only:   1 (not bounded; see below)" in body.text, "a missed mount may catch a matched call"
    assert "  Coverage:        50.0% (not bounded; see below)" in body.text
    assert body.text.endswith(
        "\n  ⚠ Backend routes may be missing from the analysis (backend/m.py:9 (unsupported_route_registration))."
        "\n  ⚠ A mount the analysis misses or misplaces may catch requests for routes after it "
        "(backend/m.py:9 (unsupported_route_registration))."
    )
    assert "Frontend calls may be missing" not in body.text
    assert body.payload["summary_bounds"] == {
        "backend_routes": "at_least",
        "frontend_calls": "exact",
        "matched_routes": "unbounded",
        "matched_calls": "unbounded",
        "match_rows": "unbounded",
        "backend_only": "at_least",
        "frontend_only": "unbounded",
        "coverage_pct": "unbounded",
    }
    unmatched = presenters.route_unmatched(queries.route_unmatched(data, side="both", limit=None))
    assert "Backend-only routes, may be incomplete (1):" in unmatched.text
    assert (
        "  ⚠ May be incomplete: a route with no caller may be missing here "
        "(backend/m.py:9 (unsupported_route_registration))."
    ) in unmatched.text
    assert "Frontend-only calls, not confirmed and may be incomplete (1):" in unmatched.text
    assert (
        "  ⚠ Not confirmed: a call here may have a route (backend/m.py:9 (unsupported_route_registration))."
    ) in unmatched.text


# ── Docs lookup ──────────────────────────────────────────────────────────


@pytest.fixture
def doc_report(tmp_path: Path):
    return doc_lookup_report(tmp_path / "repo")


def _doc_refs(report, limit: int | None = None, **query: str) -> presenters.Body:
    return doc_presenters.doc_refs(doc_queries.doc_refs_lookup(report, limit=limit, **query))


def test_doc_refs_route_text_names_the_live_route_then_each_mention(doc_report) -> None:
    body = _doc_refs(doc_report, route="POST /items/{x}/checkout")
    assert body.text.splitlines() == [
        f"Live route: POST /items/{{item_id}}/checkout  checkout()  [{DOC_LOOKUP_ROUTE_FILE}:1]",
        "",
        "2 mention(s) of POST /items/{x}/checkout in 1 doc(s):",
        "  docs/a.md:5  route  /items/{item_id}/checkout  ok",
        "  docs/a.md:5  route  POST /items/{id}/checkout  ok",
    ]
    assert body.payload["query"] == {"kind": "route", "value": "/items/{x}/checkout", "method": "POST"}
    assert (body.payload["definition_count"], body.payload["mention_count"], body.payload["omitted"]) == (1, 2, 0)


def test_doc_refs_says_when_no_live_route_or_doc_matches(doc_report) -> None:
    body = _doc_refs(doc_report, route="DELETE /nothing")
    assert body.text.splitlines() == [
        "No live route matches DELETE /nothing.",
        "",
        "No doc mentions DELETE /nothing.",
    ]
    assert (body.payload["definitions"], body.payload["mentions"]) == ([], [])


def test_doc_refs_file_text_shows_status_details_and_ambiguity(doc_report) -> None:
    text = _doc_refs(doc_report, file=DOC_LOOKUP_ROUTE_FILE).text
    assert text.startswith(f"8 mention(s) of {DOC_LOOKUP_ROUTE_FILE} in 1 doc(s):\n")
    assert "  docs/a.md:5  route  GET /items/{id}/checkout  method_differs (served: POST)" in text
    assert "  docs/a.md:7  symbol  Holder  ok  [also defined outside it]" in text
    assert text.splitlines()[-1] == "  docs/a.md:7  symbol  checkout()  ok", "ok rows carry no detail"


def test_doc_refs_symbol_text_lists_definitions_first(doc_report) -> None:
    lines = _doc_refs(doc_report, symbol="Holder.status").text.splitlines()
    assert lines[:3] == [
        "Definitions of Holder.status (1):",
        f"  variable   {DOC_LOOKUP_ROUTE_FILE}:4 in Holder",
        "",
    ]
    assert lines[3] == "1 mention(s) of Holder.status in 1 doc(s):"
    assert _doc_refs(doc_report, symbol="Nope").text.splitlines()[0] == "No definition of Nope in the symbol index."


def test_doc_refs_symbol_text_notes_an_ambiguous_name(doc_report) -> None:
    lines = _doc_refs(doc_report, symbol="Holder").text.splitlines()
    note = (
        f"Ambiguous: Holder is defined in 2 places ({DOC_LOOKUP_ROUTE_FILE}, {DOC_LOOKUP_OTHER_FILE}), "
        "so these mentions may name different symbols."
    )
    assert lines[lines.index(note) + 2].startswith("2 mention(s) of Holder"), "the note precedes the mentions"
    assert not any(line.startswith("Ambiguous:") for line in _doc_refs(doc_report, symbol="status").text.splitlines())
    assert _doc_refs(doc_report, symbol="Holder").payload["ambiguous"] is True


def test_the_ambiguity_note_names_five_owners_then_counts_the_rest() -> None:
    owners = [f"backend/m{i}.py" for i in range(7)]
    result = doc_queries.DocRefs(
        kind="symbol",
        value="router",
        method=None,
        complete=True,
        diagnostics=[],
        limitations=[],
        definitions=queries.page([], None),
        mentions=queries.page([], None),
        doc_count=0,
        ambiguous=True,
        owners=owners,
    )
    note = doc_presenters.doc_refs(result).text.splitlines()[2]
    assert note == (
        "Ambiguous: router is defined in 7 places (backend/m0.py, backend/m1.py, backend/m2.py, backend/m3.py, "
        "backend/m4.py and 2 more), so these mentions may name different symbols."
    )


def test_doc_refs_doc_text_has_links_references_and_inbound_blocks(doc_report) -> None:
    lines = _doc_refs(doc_report, limit=1, doc="docs/a.md").text.splitlines()
    assert lines[:4] == [
        "docs/a.md: checks links, paths, routes, symbols, scripts",
        "Reachability: reachable from docs/README.md.",
        "",
        "Links (3):",
    ]
    assert "  docs/a.md:9  link  nope.md  missing (docs/nope.md does not exist)" not in lines, "limited to 1"
    assert "  ... showing 1 of 3 links; pass limit=3 (or null) to see all" in lines
    assert "References (9):" in lines and "Linked from (3):" in lines
    assert lines[-2:] == ["  docs/README.md:3", "  ... showing 1 of 3 links; pass limit=3 (or null) to see all"]
    plan = _doc_refs(doc_report, doc="docs/plans/p.md").text
    assert "Reachability: outside the orphan check." in plan
    missing = _doc_refs(doc_report, doc="docs/nope.md").text
    assert missing.startswith("docs/nope.md is not a doc the check reads")


def test_doc_refs_payload_keys_are_the_same_for_every_query_kind(doc_report) -> None:
    replies = [
        _doc_refs(doc_report, file=DOC_LOOKUP_ROUTE_FILE),
        _doc_refs(doc_report, route="/items/{id}/checkout"),
        _doc_refs(doc_report, symbol="Holder"),
        _doc_refs(doc_report, doc="docs/a.md"),
        _doc_refs(doc_report, symbol="Nope"),
    ]
    keys = {frozenset(r.payload) for r in replies}
    assert len(keys) == 1
    file, route, symbol, doc, nope = (r.payload for r in replies)
    assert (file["definitions"], file["links"], file["reachable"], file["checks"]) == (None, None, None, None)
    assert (file["ambiguous"], route["ambiguous"], doc["ambiguous"]) == (None, None, None)
    assert (symbol["ambiguous"], nope["ambiguous"]) == (True, False)
    assert (file["owners"], route["owners"], doc["owners"]) == (None, None, None)
    assert (symbol["owners"], nope["owners"]) == ([DOC_LOOKUP_ROUTE_FILE, DOC_LOOKUP_OTHER_FILE], [])
    assert (doc["mentions"], doc["mention_count"], doc["doc_count"]) == (None, None, None)
    row = file["mentions"][0]
    assert set(row) == {
        "file",
        "line",
        "kind",
        "text",
        "target",
        "status",
        "detail",
        "message",
        "checked",
        "defined_in",
        "ambiguous",
    }
    assert doc["omitted"] == 0 and route["omitted"] == 0 and symbol["omitted"] == 0
    limited = _doc_refs(doc_report, limit=1, doc="docs/a.md").payload
    assert (limited["link_omitted"], limited["reference_omitted"], limited["inbound_omitted"]) == (2, 8, 2)
    assert limited["omitted"] == 12


def test_a_doc_reply_points_its_limitations_at_the_json_only(tmp_path: Path) -> None:
    """The docs check's own limitations are not in status, so a doc
    reply does not send the reader there; other replies still do."""
    report = doc_lookup_report(tmp_path / "repo", route_limitations=[limitation()])
    result = doc_queries.doc_refs_lookup(report, route="/items/{id}/checkout", limit=None)
    text = presenters.envelope(doc_presenters.doc_refs(result), "text", stale=False, analysis=result.analysis)
    assert text.splitlines()[0] == "ⓘ 1 analysis limitation(s) (opaque_router_call); details: output_format='json'"
    other = presenters.envelope(
        presenters.Body("x", {}), "text", stale=False, analysis={"analysis_limitations": [limitation()]}
    )
    assert other.splitlines()[0].endswith("details: status or output_format='json'")


def test_the_doc_check_body_text_is_its_finding_lines_only(tmp_path: Path) -> None:
    report = doc_lookup_report(tmp_path / "repo", route_limitations=[limitation()])
    result = doc_queries.doc_check(report)
    body = doc_presenters.doc_check(result)
    findings, notes = doc_presenters.doc_check_lines(result)
    assert findings and notes, "the fixture has findings and a limitation"
    assert body.text.splitlines() == findings
    assert body.payload["finding_count"] == len(result.findings)


# ── Library routes and dependencies ──────────────────────────────────────

USERS_ME = {"dist": "fastapi-users", "version": "15.0.5", "file": "fastapi_users/router/users.py", "line": 46}
MOUNTED_AT = "backend/app/main.py:170 · fastapi-users 15.0.5 fastapi_users/router/users.py:46"


def _library_match() -> Any:
    from mcp_fixtures import match

    return match(
        backend_side("GET", "/users/me", "me", file="backend/app/main.py", line=170, library=USERS_ME),
        frontend_side("GET", "/users/me", "me", line=274, def_line=273),
    )


def test_a_library_route_shows_the_include_that_mounts_it_and_where_the_library_defines_it() -> None:
    data = routes_result([_library_match(), pair("/a", "a", "fa")])
    text = presenters.route_match(
        queries.route_match(data, path="/users/me", handler=None, frontend_func=None, match="exact", limit=None)
    ).text
    assert f"Backend:  me()  [{MOUNTED_AT}]" in text
    assert "[backend/service_backend" not in text


def test_the_route_summary_counts_library_routes_apart_and_only_when_there_are_some() -> None:
    data = routes_result([_library_match(), pair("/a", "a", "fa")])
    text = presenters.route_match(
        queries.route_match(data, path=None, handler=None, frontend_func=None, match="exact", limit=None)
    ).text
    assert "  Library routes:  1 (1 matched)" in text
    assert "  Backend routes:  1" in text and "  Coverage:        100.0%" in text
    plain = presenters.route_match(
        queries.route_match(
            routes_result([pair("/a", "a", "fa")]),
            path=None,
            handler=None,
            frontend_func=None,
            match="exact",
            limit=None,
        )
    ).text
    assert "Library routes" not in plain


def test_a_library_route_is_listed_with_the_routes_that_fit_when_the_frontend_is_missing() -> None:
    from mcp_fixtures import frontend_unavailable_diagnostic

    data = routes_result([_library_match()], diagnostics=[frontend_unavailable_diagnostic()], complete=False)
    body = presenters.route_match(
        queries.route_match(data, path="/users/me", handler=None, frontend_func=None, match="exact", limit=None)
    )
    assert f"  GET /users/me  →  me()  [{MOUNTED_AT}]" in body.text


def test_di_routes_text_shows_where_the_library_defines_a_handler() -> None:
    data = di_result(routes=[di_route("me", "/users/me", file="backend/app/main.py", line=170, library=USERS_ME)])
    text = presenters.di_deps(
        queries.di_deps(data, handler="me", path=None, method=None, match="exact", limit=None)
    ).text
    assert text.splitlines()[0] == f"GET /users/me  →  me()  [{MOUNTED_AT}]"


def test_dependency_targets_show_the_binding_a_capture_filled_and_the_dist_of_a_library_callable() -> None:
    bearer = "fastapi.security.oauth2:OAuth2PasswordBearer()"
    user = "app.core.auth:current_active_user"
    data = di_result(
        [
            dep(user, kind="external", sub=[bearer], bound_to="fastapi_users.current_user(active=True)"),
            dep(
                bearer,
                kind="library",
                file=None,
                line=None,
                library={"dist": "fastapi", "version": "0.162.0", "file": "fastapi/security/oauth2.py", "line": 536},
            ),
        ],
        [di_route("me", "/me", deps=[use(user, "current_active_user", "direct", param="u")], all_deps=[user, bearer])],
    )
    listing = presenters.di_deps(
        queries.di_deps(data, handler=None, path=None, method=None, match="exact", limit=None)
    ).text
    assert "  current_active_user (external)  [backend/service_backend/deps.py:1]" in listing
    assert "-> OAuth2PasswordBearer  = fastapi_users.current_user(active=True)" in listing
    assert "  OAuth2PasswordBearer (library)  [fastapi 0.162.0 fastapi/security/oauth2.py:536]" in listing
    usage = presenters.di_reverse(queries.di_reverse(data, bearer, limit=None)).text
    assert usage.startswith("OAuth2PasswordBearer  [fastapi 0.162.0 fastapi/security/oauth2.py:536]")
    bound = presenters.di_reverse(queries.di_reverse(data, user, limit=None)).text
    assert "  Bound to: fastapi_users.current_user(active=True)" in bound


def test_status_text_names_the_library_capture_its_applied_counts_and_why_it_is_stale() -> None:
    block = {
        "file": ".code-intel/library-capture.json",
        "present": True,
        "app": "app.main:app",
        "distributions": {"fastapi": "0.162.0", "fastapi-users": "15.0.5"},
        "entries": {"includes": 3, "routes": 8, "dependencies": 3},
        "applied": {"file": ".code-intel/library-capture.json", "includes": 3, "routes": 8, "dependencies": 2},
        "problems": [],
        "stale": ["fastapi-users was captured at 15.0.4"],
    }
    text = presenters.status(_snapshot(library_capture=block)).text
    assert (
        "  Library capture: .code-intel/library-capture.json: 3 includes (8 routes), 2 dependencies applied; "
        "fastapi 0.162.0, fastapi-users 15.0.5; stale: 1 (run code-intel capture)"
    ) in text
    unbuilt = presenters.status(_snapshot(library_capture={**block, "applied": None, "stale": []})).text
    assert ": 3 includes (8 routes), 3 dependencies; fastapi 0.162.0" in unbuilt and "stale" not in unbuilt
    missing = presenters.status(_snapshot(library_capture={**block, "present": False})).text
    assert "does not exist; run code-intel capture" in missing
    payload = presenters.status(_snapshot(library_capture=block)).payload
    assert payload["library_capture"] == block
    assert "Library capture" not in presenters.status(_snapshot()).text
