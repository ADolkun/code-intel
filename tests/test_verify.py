"""``code-intel verify``: each relation broken once on plain data, the report
and exit codes, and the whole command over a real git checkout."""

from __future__ import annotations

import ast
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fixture_repo import hermetic_git
from mcp_fixtures import LAYOUT, FakeAnalyzers, doc_lookup_report, make_repo, make_runtime, write_config

from code_intel import cli, doc_queries, ts_bridge, verify
from code_intel.analysis_runtime import AnalysisRuntime
from code_intel.config import ConfigFile
from code_intel.doc_refs import reference_message
from code_intel.oracles import compare_facts

# ── Relations over plain data ────────────────────────────────────────────


def test_lost_content_lists_content_lost_diagnostics_only() -> None:
    rows = [
        {"kind": "read_failed", "category": "content_lost", "file": "a.py", "message": "could not read"},
        {"kind": "unresolved_app", "category": "resolver", "file": None, "message": "no app"},
    ]
    assert verify.lost_content(rows) == ["a.py: [read_failed] could not read"]
    assert verify.lost_content([]) == []


def test_unregistered_limitations_names_the_unknown_kinds() -> None:
    assert verify.unregistered_limitations(["asgi_mount", "made_up", "made_up"]) == ["made_up"]


def route(handler: str = "list_items", line: int = 2, **overrides: Any) -> dict[str, Any]:
    return {
        "method": "GET",
        "path": "/items",
        "handler": handler,
        "handler_id": f"pkg.api:{handler}",
        "file": "backend/pkg/api.py",
        "line": line,
        **overrides,
    }


API_LINES = ["", "def list_items():", "    pass", "", "async def other():"]


def read_api_line(rel: str, line: int) -> str:
    return API_LINES[line - 1]


def resolves_at(line: int) -> Any:
    return lambda module, qualname: ("backend/pkg/api.py", SimpleNamespace(lineno=line))


def test_a_handler_on_its_def_line_that_resolves_there_passes() -> None:
    assert verify.handler_location_problems([route()], read_api_line, resolves_at(2)) == []


def test_a_route_whose_line_is_not_a_def_of_its_handler_is_reported() -> None:
    wrong = verify.handler_location_problems([route(line=3)], read_api_line, resolves_at(3))
    assert wrong == ["GET /items: backend/pkg/api.py:3 is 'pass'"]


def test_a_handler_id_that_resolves_elsewhere_is_reported() -> None:
    [row] = verify.handler_location_problems([route()], read_api_line, resolves_at(5))
    assert "handler_id pkg.api:list_items does not resolve to backend/pkg/api.py:2" in row
    [row] = verify.handler_location_problems([route()], read_api_line, lambda module, qualname: None)
    assert "does not resolve" in row


def test_a_lambda_endpoint_is_judged_by_the_line_its_id_names() -> None:
    lines = ["", 'app.add_api_route("/p", lambda: 1)', "x = 1"]
    ok = route(handler="<lambda>", handler_id="pkg.api:<lambda>@L2", line=2)
    assert verify.handler_location_problems([ok], lambda rel, line: lines[line - 1], resolves_at(0)) == []
    off = route(handler="<lambda>", handler_id="pkg.api:<lambda>@L2", line=3)
    [row] = verify.handler_location_problems([off], lambda rel, line: lines[line - 1], resolves_at(0))
    assert "lambda pkg.api:<lambda>@L2 at backend/pkg/api.py:3" in row


def test_find_definition_follows_locals_and_instances(tmp_path: Path) -> None:
    pkg = tmp_path / "backend" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "api.py").write_text(
        "class Routes:\n    def handle(self):\n        pass\n\n\ndef make():\n    def inner():\n        pass\n"
        "    return inner\n",
        encoding="utf-8",
    )
    backend = tmp_path / "backend"
    found = verify.find_definition(backend, "pkg.api", "Routes().handle", tmp_path)
    assert found is not None and found[0] == "backend/pkg/api.py"
    assert isinstance(found[1], ast.FunctionDef) and found[1].lineno == 2
    nested = verify.find_definition(backend, "pkg.api", "make.<locals>.inner", tmp_path)
    assert nested is not None and isinstance(nested[1], ast.FunctionDef) and nested[1].lineno == 7
    assert verify.find_definition(backend, "pkg.api", "Routes.absent", tmp_path) is None
    assert verify.find_definition(backend, "pkg.missing", "x", tmp_path) is None


def test_route_sets_that_differ_are_reported_both_ways() -> None:
    di = [route(), route(handler="gone", handler_id="pkg.api:gone", path="/gone")]
    scan = [route(), route(handler="new", handler_id="pkg.api:new", path="/new")]
    assert verify.route_set_differences(di, scan) == [
        "only in the dependency map: GET /gone pkg.api:gone",
        "only in the routes scan: GET /new pkg.api:new",
    ]
    assert verify.route_set_differences(di, di) == []


def test_a_matched_backend_row_that_is_not_live_is_reported() -> None:
    live = {"method": "GET", "path": "/items", "handler": "list_items", "file": "a.py", "line": 2}
    stale = {**live, "line": 9}
    data = {"backend_routes": [live], "matches": [{"backend": live}, {"backend": stale}]}
    assert verify.stray_matched_backends(data) == ["GET /items -> a.py:9"]
    assert verify.stray_matched_backends({**data, "matches": [{"backend": live}]}) == []


def test_a_module_level_definition_missing_from_the_index_is_reported() -> None:
    indexed = {("a.py", "f", 1)}
    expected = {("a.py", "f", 1), ("a.py", "g", 5)}
    assert verify.unindexed_definitions(indexed, expected) == ["a.py:5: g"]
    assert verify.unindexed_definitions(expected, expected) == []


SOURCE = {
    "ping.ts": [
        'import { apiFetch as rawFetch } from "@/lib/api";',
        'export const ping = () => rawFetch("/ping");',
        "export function pong() {",
        "  return api",
        '    .get("/pong");',
        "}",
        'const ws = new WebSocket(getWsBaseUrl() + "/ws/items/" + id);',
    ]
}


def read_ping(rel: str, line: int) -> str:
    return SOURCE[rel][line - 1]


def call_row(**overrides: Any) -> dict[str, Any]:
    return {
        "file": "ping.ts",
        "line": 2,
        "kind": "call",
        "client": "apiFetch",
        "builder": None,
        "column": 27,
        "callee": "rawFetch",
        "callee_line": 2,
        "callee_column": 27,
        **overrides,
    }


def test_call_sites_accept_aliases_wrapped_member_calls_and_concatenated_websocket_urls() -> None:
    assert verify.call_site_problem(call_row(), read_ping) is None
    wrapped = call_row(line=4, kind="instance", client="api", callee="get", callee_line=5, callee_column=6)
    assert verify.call_site_problem(wrapped, read_ping) is None
    socket = {"file": "ping.ts", "line": 7, "kind": "websocket", "client": None, "builder": None, "column": 26}
    assert verify.call_site_problem(socket, read_ping) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"callee_column": 35}, "callee 'rawFetch' is not at 2:35"),
        ({"callee": "apiFetch"}, "callee 'apiFetch' is not at 2:27"),
        ({"callee_line": None}, "call row without callee/callee_line/callee_column"),
        ({"client": None}, "call row without a client"),
        ({"callee_line": 1}, "callee_line 1 is before the call's line 2"),
        ({"callee_column": 400}, "callee_column 400 is outside line 2"),
        ({"kind": "builder", "builder": None}, "builder row without a builder"),
    ],
)
def test_a_call_row_that_does_not_point_at_a_call_of_its_kind_is_reported(
    overrides: dict[str, Any], expected: str
) -> None:
    problem = verify.call_site_problem(call_row(**overrides), read_ping)
    assert problem is not None and problem.startswith(expected)
    [row] = verify.call_site_problems([call_row(**overrides)], read_ping)
    assert row.startswith("ping.ts:2:27 (")


def test_a_websocket_row_into_the_middle_of_a_line_is_reported() -> None:
    socket = {"file": "ping.ts", "line": 7, "kind": "websocket", "client": None, "builder": None, "column": 40}
    problem = verify.call_site_problem(socket, read_ping)
    assert problem is not None and problem.startswith("column 40 does not start a URL expression")
    attributed = {**socket, "client": "api"}
    assert verify.call_site_problem(attributed, read_ping) == "websocket row attributed to a client or builder"


def reference(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "doc": "docs/a.md",
        "line": 1,
        "kind": "route",
        "text": "GET /items",
        "context": "prose",
        "target": "GET /items",
        "status": "ok",
        "detail": None,
        "checked": True,
        "defined_in": ("backend/pkg/api.py:2",),
        "broken": False,
    }
    return SimpleNamespace(**{**fields, **overrides})


def test_a_reference_must_start_on_its_reported_line() -> None:
    lines = ["Call GET /items now.", "Other text."]
    assert verify.misplaced_references({"docs/a.md": SimpleNamespace(references=[reference()])}, lambda d: lines) == []
    moved = reference(line=2)
    [row] = verify.misplaced_references({"docs/a.md": SimpleNamespace(references=[moved])}, lambda d: lines)
    assert row == "docs/a.md:2: route 'GET /items'"
    beyond = reference(line=9)
    assert len(verify.misplaced_references({"docs/a.md": SimpleNamespace(references=[beyond])}, lambda d: lines)) == 1


def test_a_wrapped_code_span_starts_on_the_line_that_opens_it() -> None:
    lines = ["Hidden means `GET", "/assets/{id}/tax-lots` reports nothing.", "Elsewhere."]
    wrapped = reference(text="GET /assets/{id}/tax-lots", line=1)
    assert verify.reference_on_its_line(wrapped, lines)
    assert not verify.reference_on_its_line(reference(text="GET /assets/{id}/tax-lots", line=3), lines)


def test_symbols_and_paths_must_sit_between_backticks_on_their_line() -> None:
    lines = ["Use `Widget` here.", "Widget in prose."]
    assert verify.reference_on_its_line(reference(kind="symbol", text="Widget", line=1), lines)
    assert not verify.reference_on_its_line(reference(kind="symbol", text="Widget", line=2), lines)


def test_a_link_may_hold_its_target_percent_encoded() -> None:
    lines = ["[x](a%20b.md)"]
    assert verify.reference_on_its_line(reference(kind="link", text="a b.md"), lines)


def test_a_failing_reference_with_a_generic_message_is_reported() -> None:
    generic = reference(broken=True, status="missing", detail=None)
    specific = reference(broken=True, status="missing", detail="gone", line=2)
    passing = reference(line=3)
    assert verify.generic_failures([generic, specific, passing], lambda ref: "route not found: x") == [
        "docs/a.md:1: route missing"
    ]
    assert verify.generic_failures([specific], lambda ref: "route missing") == ["docs/a.md:2: route missing"]
    assert verify.generic_failures([specific], reference_message_stub) == []


def reference_message_stub(ref: Any) -> str:
    return f"{ref.kind} not found: {ref.text}"


def test_an_ok_route_reference_must_be_a_live_route_at_its_location() -> None:
    di = [route()]
    ok = reference(defined_in=("backend/pkg/api.py:2",))
    assert verify.stray_route_references(di, [ok]) == []
    moved = reference(defined_in=("backend/pkg/api.py:9",))
    [row] = verify.stray_route_references(di, [moved])
    assert row.startswith("docs/a.md:1: GET /items -> GET /items")
    other = reference(target="POST /items")
    assert len(verify.stray_route_references(di, [other])) == 1
    assert verify.stray_route_references(di, [reference(status="missing", defined_in=())]) == []


def test_the_lookups_agree_with_a_report_and_disagree_when_they_find_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = doc_lookup_report(tmp_path / "repo")
    assert verify.lookup_disagreements(report) == []

    empty = SimpleNamespace(definitions=None, mentions=None, inbound=None)
    monkeypatch.setattr(doc_queries, "doc_refs_lookup", lambda *args, **kwargs: empty)
    rows = verify.lookup_disagreements(report)
    assert any(row.startswith("route ") and "no live route" in row for row in rows)
    assert any(row.startswith("symbol ") for row in rows)
    assert any(row.startswith("doc ") for row in rows)


def test_reference_message_is_what_generic_failures_compares_against() -> None:
    ref = reference(kind="route", status="missing", broken=True, detail="x", text="GET /nope")
    assert verify.generic_failures([ref], reference_message) == []


def test_oracle_differences_are_reported_as_missing_extra_and_field_rows() -> None:
    oracle = [
        {"file": "a.tsx", "tag": "A", "line": 1, "column": 1, "kind": "open", "parent": "X", "is_test": False},
        {"file": "a.tsx", "tag": "B", "line": 2, "column": 1, "kind": "open", "parent": "X", "is_test": False},
        {"file": "a.tsx", "tag": "C", "line": 3, "column": 1, "kind": "open", "parent": "X", "is_test": False},
    ]
    tool = [
        {**oracle[0], "parent": "Y"},
        oracle[1],
        {**oracle[2], "line": 4},
    ]
    result = compare_facts.compare(oracle, tool, ("file", "line", "column", "tag"), compare_facts.JSX_FIELDS)
    assert verify.comparison_rows(result) == [
        "only in the oracle: ('a.tsx', 3, 1, 'C')",
        "only in the tool: ('a.tsx', 4, 1, 'C')",
        "('a.tsx', 1, 1, 'A'): parent oracle 'X', tool 'Y'",
    ]
    same = compare_facts.compare(oracle, oracle, ("file", "line", "column", "tag"), compare_facts.JSX_FIELDS)
    assert verify.comparison_rows(same) == []


def test_an_oracle_that_fails_to_run_cannot_run_and_names_its_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "oracle.py"
    script.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(3)\n", encoding="utf-8")
    with pytest.raises(verify.CannotRun, match=r"oracle\.py exited with code 3: boom"):
        verify.run_oracle([sys.executable, str(script)])
    with pytest.raises(verify.CannotRun, match="could not run"):
        verify.run_oracle([str(tmp_path / "missing"), str(script)])


def test_a_python_definition_that_is_not_a_code_word_of_its_file_is_reported() -> None:
    rows = [
        SimpleNamespace(language="python", file="a.py", name="present"),
        SimpleNamespace(language="python", file="a.py", name="absent"),
        SimpleNamespace(language="typescript", file="a.ts", name="ignored"),
    ]
    assert verify.absent_code_words(rows, lambda rel: b"def present(): pass\n") == ["a.py: absent"]
    assert verify.absent_code_words(rows[:1], lambda rel: b"def present(): pass\n") == []
    [row] = verify.absent_code_words(rows[:1], lambda rel: b"\xff\xfe not python")
    assert row.startswith("a.py: [decode_failed]")


# ── Outcomes, rendering and exit codes ───────────────────────────────────


def outcome(name: str, status: Any, detail: str = "d", rows: tuple[str, ...] = ()) -> verify.Outcome:
    return verify.Outcome(name, status, detail, rows)


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        (["pass", "skipped"], 0),
        (["pass", "fail"], 1),
        (["pass", "error"], 2),
        (["error", "fail"], 1),
        ([], 0),
    ],
)
def test_exit_codes_put_a_failure_before_a_check_that_could_not_run(statuses: list[str], code: int) -> None:
    assert verify.exit_code([outcome(f"c{i}", status) for i, status in enumerate(statuses)]) == code


def test_a_failing_check_lists_its_first_rows_and_counts_the_rest() -> None:
    rows = tuple(f"row {i}" for i in range(verify.SHOWN + 3))
    text = verify.render([outcome("alpha", "pass", "3 routes"), outcome("beta", "fail", "9 refs", rows)])
    lines = text.splitlines()
    assert lines[0] == "PASS    alpha  3 routes"
    assert lines[1] == "FAIL    beta   9 refs"
    assert lines[2] == "          row 0"
    assert "          ... 3 more" in lines
    assert lines[-1] == "verify: 2 checks, 1 passed, 1 failed, 0 could not run, 0 skipped"


def subject_for(tmp_path: Path, *, docs: bool) -> verify.Subject:
    repo = make_repo(tmp_path / "repo")
    if not docs:
        write_config(repo, LAYOUT)
    return verify.Subject(make_runtime(repo, FakeAnalyzers()))


def test_a_check_that_raises_cannot_run_is_an_error_not_a_failure(tmp_path: Path) -> None:
    def cannot(subject: verify.Subject) -> verify.Verdict:
        raise verify.CannotRun("the frontend toolchain is unavailable. Fix: code-intel setup")

    result = verify.run_check(verify.Check("needs-node", cannot), subject_for(tmp_path, docs=True))
    assert (result.status, result.detail) == ("error", "the frontend toolchain is unavailable. Fix: code-intel setup")


def test_a_docs_check_is_skipped_without_a_docs_table(tmp_path: Path) -> None:
    check = verify.Check("docs-x", lambda subject: verify.Verdict("never"), needs_docs=True)
    assert verify.run_check(check, subject_for(tmp_path, docs=False)).status == "skipped"
    assert verify.run_check(check, subject_for(tmp_path / "again", docs=True)).status == "pass"


def test_every_check_has_a_unique_name() -> None:
    names = [check.name for check in verify.CHECKS]
    assert len(names) == len(set(names))
    assert {c.name for c in verify.CHECKS if c.needs_docs} == {n for n in names if n.startswith("docs-")}


# ── A real checkout, end to end ──────────────────────────────────────────

CONFIG = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"
python_version_file = ".python-version"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"

[[frontend.clients]]
kind = "instance"
module = "frontend/lib/http.ts"
export = "api"
base_path = "/api"

[docs]
scope = ["docs/**/*.md"]
route_prefixes = ["/api"]

[docs.reachability]
root = "docs/README.md"
include = ["docs/**/*.md"]
exclude = []
"""

MAIN = """\
from fastapi import APIRouter, FastAPI

app = FastAPI()
router = APIRouter(prefix="/api")


@router.get("/items")
async def list_items() -> list[int]:
    return []


@router.delete("/items/{item_id}")
def delete_item(item_id: int) -> None:
    return None


class Holder:
    pass


app.include_router(router)
"""

HTTP_TS = """\
declare const axios: { create(options: { baseURL: string }): any };
export const api = axios.create({ baseURL: "/api" });
"""

ITEMS_TSX = """\
import { api } from "../lib/http";

export const loadItems = () => api.get("/items");

export function Items() {
  return <ul><li>{String(loadItems)}</li></ul>;
}
"""

GUIDE = """\
# Guide

List items with `GET /api/items`, which `list_items` handles.

Remove one with `DELETE /items/{item_id}`.
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def checkout(root: Path) -> Path:
    files = {
        ".code-intel/config.toml": CONFIG,
        ".python-version": "3.14\n",
        "backend/service_backend/__init__.py": "",
        "backend/service_backend/main.py": MAIN,
        "frontend/tsconfig.json": '{"compilerOptions": {"jsx": "react-jsx", "module": "esnext"}}\n',
        "frontend/lib/http.ts": HTTP_TS,
        "frontend/components/Items.tsx": ITEMS_TSX,
        "docs/README.md": "# Docs\n\n- [Guide](guide.md)\n",
        "docs/guide.md": GUIDE,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain: ts_bridge.Toolchain) -> Path:
    hermetic_git(monkeypatch, tmp_path / "home")
    # Hermetic git moves HOME, and with it the user cache the toolchain is found in.
    monkeypatch.setenv(ts_bridge.TS_DIR_ENV, str(typescript_toolchain.typescript_dir))
    return checkout(tmp_path / "repo")


def outcomes_of(repo: Path) -> dict[str, verify.Outcome]:
    runtime = AnalysisRuntime(repo, ConfigFile.locate(repo), [])
    return {o.name: o for o in verify.verify(runtime)}


def test_a_clean_checkout_passes_every_check(repo: Path) -> None:
    results = outcomes_of(repo)
    assert {name: (o.status, o.rows) for name, o in results.items()} == {
        check.name: (("skipped", ()) if check.needs_capture else ("pass", ())) for check in verify.CHECKS
    }
    assert results["library-capture"].detail == "the config has no [backend.capture] table"
    assert results["handler-locations"].detail == "2 routes"
    assert results["call-sites"].detail == "1 frontend calls"
    assert results["python-index"].detail == "3 module-level definitions"


def test_a_stale_doc_reference_does_not_fail_verify_but_a_lost_file_does(repo: Path) -> None:
    (repo / "docs" / "guide.md").write_text(GUIDE + "\nGone: `backend/service_backend/gone.py`.\n", encoding="utf-8")
    git(repo, "add", "-A")
    assert {o.status for o in outcomes_of(repo).values()} == {"pass", "skipped"}
    (repo / "backend" / "service_backend" / "broken.py").write_text("def (:\n", encoding="utf-8")
    git(repo, "add", "-A")
    results = outcomes_of(repo)
    assert results["backend-content"].status == "fail"
    assert any("broken.py" in row for row in results["backend-content"].rows)


def test_a_missing_toolchain_is_a_could_not_run_with_the_fix(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ts_bridge.CACHE_ENV, str(tmp_path / "no-cache"))
    monkeypatch.delenv(ts_bridge.TS_DIR_ENV)
    results = outcomes_of(repo)
    assert results["jsx-oracle"].status == "error"
    assert "code-intel setup" in results["jsx-oracle"].detail
    assert results["handler-locations"].status == "pass"
    assert verify.exit_code(results.values()) == 2


def run_cli(repo: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["verify", "--repo-root", str(repo), *args])
    return code, out.getvalue()


def test_the_command_prints_one_line_per_check_and_exits_0(repo: Path) -> None:
    code, text = run_cli(repo)
    assert code == 0
    assert (
        text.splitlines()[-1]
        == f"verify: {len(verify.CHECKS)} checks, {len(verify.CHECKS) - 1} passed, 0 failed, 0 could not run, 1 skipped"
    )
    code, text = run_cli(repo, "handler-locations", "call-sites")
    assert code == 0 and text.count("PASS") == 2


def test_an_unknown_check_name_is_a_usage_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(repo, "nope")[0] == 2
    assert "unknown check 'nope'" in capsys.readouterr().err


def test_the_command_is_listed_by_the_cli() -> None:
    args = cli.build_parser().parse_args(["verify", "handler-locations"])
    assert (args.command, args.only) == ("verify", ["handler-locations"])
