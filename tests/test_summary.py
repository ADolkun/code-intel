"""``code-intel summary``: every section renders over a real git checkout, a
section that crashes or cannot run becomes a row in its place, and the
exit code says whether any section failed."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fixture_repo import hermetic_git
from mcp_fixtures import LAYOUT, FakeAnalyzers, make_repo, make_runtime, write_config
from test_verify import checkout

from code_intel import cli, summary, ts_bridge
from code_intel.analysis_runtime import AnalysisFailed, AnalysisRuntime, FrontendFailed, FrontendProblem
from code_intel.config import ConfigFile
from code_intel.ts_bridge import HelperError, ToolchainProblem, ToolchainUnavailable


def test_calls_by_kind_counts_distinct_calls_not_url_variants() -> None:
    rows = [
        {"file": "a.ts", "line": 1, "column": 3, "kind": "instance", "is_test": False},
        {"file": "a.ts", "line": 1, "column": 3, "kind": "instance", "is_test": False},
        {"file": "a.ts", "line": 9, "column": 3, "kind": "global", "is_test": False},
        {"file": "a.test.ts", "line": 2, "column": 1, "kind": "instance", "is_test": True},
    ]
    assert dict(summary.calls_by_kind(rows)) == {"instance": 1, "global": 1}


@pytest.mark.parametrize("value", ["a|b", 3])
def test_render_escapes_table_pipes(value: object) -> None:
    text = summary.render("T", [("x", value)])
    assert text.splitlines()[:4] == ["### T", "", "| Metric | Value |", "| --- | --- |"]
    assert text.splitlines()[4] == f"| x | {str(value).replace('|', '/')} |"


def crashing(error: Exception):
    def section(runtime: AnalysisRuntime) -> summary.Rows:
        raise error

    return section


@pytest.fixture
def runtime(tmp_path: Path) -> AnalysisRuntime:
    return make_runtime(make_repo(tmp_path / "repo"), FakeAnalyzers())


def problem() -> ToolchainProblem:
    return ToolchainProblem("node_not_found", "node not found", "node", "install node")


@pytest.mark.parametrize(
    ("error", "row"),
    [
        (KeyError("symbols"), ("Failed", "symbol_resolve: KeyError: 'symbols'")),
        (summary.Skipped("the config has no [docs] table"), ("Skipped", "the config has no [docs] table")),
        (ToolchainUnavailable(problem()), ("Unavailable", "node not found. Fix: install node")),
        (
            FrontendFailed("route_map", FrontendProblem("node_not_found", "no node", None, "set it up")),
            ("Unavailable", "no node. Fix: set it up"),
        ),
        (
            AnalysisFailed("di_map", "walk", "di_map failed at step 'walk': boom\n  repo root: /r"),
            ("Failed", "di_map failed at step 'walk': boom"),
        ),
    ],
)
def test_a_section_that_cannot_render_becomes_one_row(
    runtime: AnalysisRuntime, error: Exception, row: tuple[str, str]
) -> None:
    assert summary.section_rows(runtime, "symbol_resolve", crashing(error)) == [row]


def test_a_crashed_helper_is_unavailable_with_its_fix(runtime: AnalysisRuntime) -> None:
    [(name, value)] = summary.section_rows(runtime, "ts_facts", crashing(HelperError("facts", "exit", "boom")))
    assert name == "Unavailable" and "boom" in str(value) and "run the helper by hand" in str(value)


def test_problem_kinds_counts_each_kind() -> None:
    rows = [{"kind": "b"}, {"kind": "a"}, {"kind": "a"}]
    assert summary.problem_kinds(rows) == "a 2, b 1"
    assert summary.problem_kinds([]) == "none"


def test_an_old_interpreter_skips_every_section_that_reads_the_backend(tmp_path: Path) -> None:
    runtime = make_runtime(make_repo(tmp_path / "repo"), FakeAnalyzers(), version_info=(3, 10))
    text, _ = summary.summarize(runtime)
    assert text.count("| Skipped | Python 3.10 is older than the backend's 3.14 |") == sum(
        1 for *_, needs_backend in summary.SECTIONS if needs_backend
    )


def test_docs_are_skipped_without_a_docs_table(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    write_config(repo, LAYOUT)
    rows = summary.section_rows(make_runtime(repo, FakeAnalyzers()), "doc_refs", summary.docs_section)
    assert rows == [("Skipped", "the config has no [docs] table")]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typescript_toolchain: ts_bridge.Toolchain) -> Path:
    hermetic_git(monkeypatch, tmp_path / "home")
    monkeypatch.setenv(ts_bridge.TS_DIR_ENV, str(typescript_toolchain.typescript_dir))
    return checkout(tmp_path / "repo")


def test_every_section_renders_over_a_real_checkout_without_a_failed_row(repo: Path) -> None:
    text, failed = summary.summarize(AnalysisRuntime(repo, ConfigFile.locate(repo), []))
    assert not failed, text
    for title, *_ in summary.SECTIONS:
        assert f"### {title}" in text
    assert "| Failed |" not in text and "| Unavailable |" not in text
    assert "| App | service_backend.main:app (FastAPI unknown, include model unknown) |" in text
    assert "| Routes | 2 (2 HTTP, 0 WebSocket) |" in text
    assert "| Frontend calls | 1 (instance 1) |" in text
    assert "| Docs | 2 |" in text
    assert "| Findings | 0 |" in text


def test_the_command_prints_the_markdown_and_exits_0(repo: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["summary", "--repo-root", str(repo)])
    assert code == 0
    assert out.getvalue().startswith("## Code-intel analysis\n")


def test_the_command_exits_1_when_a_section_failed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary, "SECTIONS", (("Backend", "di_map", crashing(KeyError("x")), True),))
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["summary", "--repo-root", str(repo)])
    assert code == 1
    assert "| Failed | di_map: KeyError: 'x' |" in out.getvalue()
