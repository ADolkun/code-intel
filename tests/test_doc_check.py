"""``code-intel doc-check``: exit codes, the exact ``path:line: message`` lines
for a stale path, a broken anchor, a removed route, an orphaned doc and a
broken agent file, and the same failures caused by editing a real git
checkout end to end."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fixture_repo import hermetic_git
from mcp_fixtures import (
    CONFIG_FILE,
    E2E_LAYOUT,
    FakeAnalyzers,
    config_toml,
    di_result,
    di_route,
    make_repo,
    make_runtime,
    missing_typescript,
    read_failed,
    repo_files_on_disk,
    unresolved_app,
    write,
    write_doc_config,
)

from code_intel import doc_check, ts_bridge
from code_intel.doc_rules import STAGE
from code_intel.problems import limitation
from code_intel.repo_files import RepoFilesError

pytestmark = pytest.mark.usefixtures("ctime_follows_mtime")

MAIN_FILE = "backend/service_backend/main.py"

README = """\
# Docs

- [Guide](a.md)
- [Reference](b.md)
"""

A_MD = """\
# Guide

The app lives in `backend/service_backend/main.py`.

List items with `GET /items`.

See [the part](b.md#the-part).
"""

B_MD = """\
# Reference

## The part
"""


def fixture_repo(root: Path) -> Path:
    make_repo(root)
    write_doc_config(root)
    write(root / "docs" / "README.md", README)
    write(root / "docs" / "a.md", A_MD)
    write(root / "docs" / "b.md", B_MD)
    return root


def fixture_fakes() -> FakeAnalyzers:
    fakes = FakeAnalyzers()
    fakes.di = di_result(routes=[di_route("list_items", "/items", file=MAIN_FILE, line=4)])
    return fakes


def check(repo: Path, fakes: FakeAnalyzers, *, as_json: bool = False, **runtime_kw) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = doc_check.check(make_runtime(repo, fakes, **runtime_kw), out, err, as_json=as_json)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return fixture_repo(tmp_path / "repo")


# ── Exit codes and lines, over injected analyzers ────────────────────────


def test_a_clean_checkout_exits_0_with_nothing_on_stdout(repo: Path) -> None:
    code, out, err = check(repo, fixture_fakes())
    assert (code, out) == (0, "")
    assert err.splitlines()[-1] == "doc-check: 0 findings in 3 docs (5 references, 5 checked)"


def append(path: Path, text: str) -> None:
    write(path, path.read_text(encoding="utf-8") + text, 2000)


@pytest.mark.parametrize(
    ("plant", "expected"),
    [
        (
            lambda repo: append(repo / "docs" / "a.md", "\nOld code was in `backend/gone.py`.\n"),
            "docs/a.md:9: stale path: backend/gone.py (not found under the repository root)",
        ),
        (
            lambda repo: append(repo / "docs" / "a.md", "\nSee [x](b.md#nope).\n"),
            "docs/a.md:9: broken anchor: b.md#nope (no heading or id in docs/b.md)",
        ),
        (
            lambda repo: append(repo / "docs" / "a.md", "\nRemove one with `DELETE /items/{item_id}`.\n"),
            "docs/a.md:9: route not found: DELETE /items/{item_id} (not in the analyzed backend routes)",
        ),
        (
            lambda repo: write(repo / "docs" / "orphan.md", "# Orphan\n"),
            "docs/orphan.md:1: orphaned doc: not reachable from docs/README.md",
        ),
    ],
    ids=["stale-path", "broken-anchor", "removed-route", "orphaned-doc"],
)
def test_each_planted_drift_prints_its_exact_line_and_exits_1(repo: Path, plant, expected: str) -> None:
    plant(repo)
    code, out, err = check(repo, fixture_fakes())
    assert (code, out.splitlines()) == (1, [expected])
    assert "1 finding in" in err


def test_an_incomplete_report_exits_2_and_names_the_diagnostic(repo: Path) -> None:
    fakes = fixture_fakes()
    fakes.di = di_result(
        routes=[di_route("list_items", "/items", file=MAIN_FILE, line=4)],
        complete=False,
        diagnostics=[read_failed(MAIN_FILE)],
    )
    code, out, err = check(repo, fakes)
    assert (code, out) == (2, "")
    assert "⚠ INCOMPLETE" in err
    assert f"[read_failed] {MAIN_FILE}" in err
    assert err.splitlines()[-1].endswith("; INCOMPLETE")


def test_a_diagnostic_of_an_untracked_file_does_not_count(repo: Path) -> None:
    """CI's checkout has no untracked files, so the local verdict ignores them."""
    untracked = "backend/service_backend/scratch.py"
    write(repo / untracked, "x = (\n")
    fakes = fixture_fakes()
    fakes.repo_files = repo_files_on_disk(repo, untracked=[untracked])
    fakes.di = di_result(routes=fakes.di["routes"], complete=False, diagnostics=[read_failed(untracked)])
    code, out, err = check(repo, fakes)
    assert (code, out) == (0, ""), err


def test_a_diagnostic_not_tied_to_a_repository_path_still_exits_2(repo: Path) -> None:
    """`unresolved_app` names a module, not a file: no untracked file explains
    it away, and a lost app hides every route."""
    fakes = fixture_fakes()
    fakes.di = di_result(routes=[], complete=False, diagnostics=[unresolved_app()])
    code, out, err = check(repo, fakes)
    assert code == 2, err
    assert "[unresolved_app] service_backend.main" in err


def test_an_incomplete_report_with_findings_still_exits_2(repo: Path) -> None:
    write(repo / "docs" / "orphan.md", "# Orphan\n")
    fakes = fixture_fakes()
    fakes.di = di_result(complete=False, diagnostics=[read_failed(MAIN_FILE)])
    code, out, _ = check(repo, fakes)
    assert code == 2
    assert "docs/orphan.md:1: orphaned doc: not reachable from docs/README.md" in out.splitlines()


def test_a_missing_typescript_toolchain_is_only_a_note(repo: Path) -> None:
    fakes = fixture_fakes()
    fakes.ts_error = missing_typescript()
    code, out, err = check(repo, fakes)
    assert (code, out) == (0, "")
    assert "ⓘ 1 analysis limitation(s)" in err
    assert "[frontend_definitions_unavailable] frontend:" in err


def test_route_gap_limitations_are_notes_and_others_are_not(repo: Path) -> None:
    gap = {"kind": "opaque_router_call", "stage": "router_graph", "file": MAIN_FILE, "line": 9, "message": "escapes"}
    other = {"kind": "unresolved_dependency", "stage": "di", "file": MAIN_FILE, "line": 3, "message": "dep"}
    fakes = fixture_fakes()
    fakes.di["analysis_limitations"] = [gap, other]
    code, out, err = check(repo, fakes)
    assert (code, out) == (0, "")
    assert "[opaque_router_call] backend/service_backend/main.py:9: escapes" in err
    assert "unresolved_dependency" not in err


def test_json_output_carries_findings_counts_and_the_analysis(repo: Path) -> None:
    write(repo / "docs" / "orphan.md", "# Orphan\n")
    code, out, _ = check(repo, fixture_fakes(), as_json=True)
    body = json.loads(out)
    assert code == 1
    assert body["findings"] == [
        {
            "file": "docs/orphan.md",
            "line": 1,
            "kind": "orphan",
            "status": "orphaned",
            "text": "docs/orphan.md",
            "message": "orphaned doc: not reachable from docs/README.md",
        }
    ]
    assert (body["finding_count"], body["doc_count"]) == (1, 4)
    assert body["reference_counts"]["route"] == {"ok": 1}
    assert body["reference_counts"]["link"] == {"ok": 3}
    assert (body["complete"], body["diagnostics"], body["analysis_limitations"]) == (True, [], [])
    assert body["schema_version"] == 2


def test_a_git_failure_exits_2_with_gits_message_and_the_fix(repo: Path) -> None:
    fakes = fixture_fakes()
    fakes.raise_in["list_repo_files"] = RepoFilesError(
        ("git", "ls-files"), "fatal: detected dubious ownership in repository", 128
    )
    code, out, err = check(repo, fakes)
    assert (code, out) == (2, "")
    assert "doc_refs failed at step 'list repository files'" in err
    assert "dubious ownership" in err
    assert "safe.directory" in err


def test_an_old_interpreter_exits_2_before_any_analysis(repo: Path) -> None:
    fakes = fixture_fakes()
    code, out, err = check(repo, fakes, version_info=(3, 10))
    assert (code, out) == (2, "")
    assert "Python 3.10 is older than the backend's 3.14" in err
    assert fakes.calls == {}


def test_an_internal_error_exits_2_and_names_the_crash(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = make_runtime(repo, fixture_fakes())

    def crash() -> None:
        raise KeyError("boom")

    monkeypatch.setattr(runtime, "get_doc_report", crash)
    out, err = io.StringIO(), io.StringIO()
    assert doc_check.check(runtime, out, err) == 2
    assert out.getvalue() == ""
    assert "internal error: KeyError: 'boom'" in err.getvalue()
    assert "in crash" in err.getvalue()


# ── Agent files and pages join the same report ───────────────────────────

AGENTS_TABLE = """\
[agents]
rules_dir = ".claude/rules"
skills_dir = ".claude/skills"
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
"""

AGENTS_MD = """\
# Agents

## Rules

### Scoped instruction map

- `api`: `backend/**`
"""

API_RULE = '---\npaths:\n  - "backend/**"\n---\n\n# API\n'


def agent_repo(root: Path) -> Path:
    fixture_repo(root)
    write(root / CONFIG_FILE, (root / CONFIG_FILE).read_text(encoding="utf-8") + AGENTS_TABLE)
    write(root / "AGENTS.md", AGENTS_MD)
    write(root / ".claude" / "rules" / "api.md", API_RULE)
    return root


def test_agent_findings_print_with_the_docs_findings_in_file_order(tmp_path: Path) -> None:
    root = agent_repo(tmp_path / "repo")
    assert check(root, fixture_fakes())[:2] == (0, "")
    write(root / ".claude" / "rules" / "api.md", API_RULE.replace("backend/**", "backend/gone/**"))
    write(root / "docs" / "orphan.md", "# Orphan\n")
    code, out, _ = check(root, fixture_fakes())
    assert (code, out.splitlines()) == (
        1,
        [
            ".claude/rules/api.md:3: paths glob 'backend/gone/**' matches no git-visible file",
            "AGENTS.md:7: instruction map globs for 'api' differ from the paths in .claude/rules/api.md "
            "(only in the map: backend/**; only in the rule: backend/gone/**)",
            "docs/orphan.md:1: orphaned doc: not reachable from docs/README.md",
        ],
    )


def test_agent_findings_are_in_the_json_findings_and_counts(tmp_path: Path) -> None:
    root = agent_repo(tmp_path / "repo")
    write(root / ".claude" / "rules" / "api.md", "# API\n")
    code, out, _ = check(root, fixture_fakes(), as_json=True)
    body = json.loads(out)
    assert code == 1
    assert [(f["file"], f["kind"], f["status"]) for f in body["findings"]] == [
        (".claude/rules/api.md", "agent", "invalid")
    ]
    assert body["finding_count"] == 1


def test_a_page_provider_limitation_is_a_note_and_the_run_still_passes(repo: Path) -> None:
    class Unavailable:
        def pages(self, tracked: frozenset[str]):
            from code_intel.doc_rules import PageSet

            note = limitation("frontend_pages_unavailable", stage=STAGE, file=None, line=None, message="no node")
            return PageSet(available=False, problems=(note,))

    fakes = fixture_fakes()
    fakes.page_provider = Unavailable()
    append(repo / "docs" / "a.md", "\nOpen `/assets` in the app.\n")
    code, out, err = check(repo, fakes)
    assert (code, out) == (0, "")
    assert "[frontend_pages_unavailable]" in err


# ── End to end: a real git checkout, the code changes ────────────────────

E2E_MAIN = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/items")
def list_items() -> list[int]:
    return []


@app.delete("/items/{item_id}")
def delete_item(item_id: int) -> None:
    return None
"""

E2E_README = """\
# Docs

- [Guide](a.md)
- [Reference](b.md)
- [Changelog](c.md)
"""

E2E_A = """\
# Guide

Items are stored by `backend/service_backend/items.py`.

Remove one with `DELETE /items/{item_id}`.

See [the part](b.md#the-part).
"""

E2E_APP = """\
export const App = () => (
  <Routes>
    <Route path="/home" element={<Home />} />
    <Route path="/groups" element={<Groups />}>
      <Route path=":id" element={<Group />} />
    </Route>
  </Routes>
);
"""

E2E_PAGES = """\
[frontend.pages]
kind = "react-router"
modules = ["frontend/src/App.tsx"]

"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def e2e_repo(root: Path) -> Path:
    files = {
        ".python-version": "3.14\n",
        "backend/service_backend/__init__.py": "",
        "backend/service_backend/main.py": E2E_MAIN,
        "backend/service_backend/items.py": "ITEMS: list[int] = []\n",
        "frontend/src/App.tsx": E2E_APP,
        "docs/README.md": E2E_README,
        "docs/a.md": E2E_A,
        "docs/b.md": B_MD,
        "docs/c.md": "# Changelog\n",
        "AGENTS.md": AGENTS_MD,
        ".claude/rules/api.md": API_RULE,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = config_toml(E2E_LAYOUT)
    (root / ".code-intel").mkdir()
    (root / CONFIG_FILE).write_text(config + E2E_PAGES + AGENTS_TABLE, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    return root


def run_cli(repo: Path, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    """The CLI as CI runs it: a fresh interpreter, no TypeScript unless ``env_extra`` provides it."""
    env = {k: v for k, v in os.environ.items() if k != ts_bridge.TS_DIR_ENV}
    env[ts_bridge.CACHE_ENV] = str(repo.parent / "no-cache")
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-c", "from code_intel.cli import main; raise SystemExit(main())", "doc-check"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _delete_named_file(repo: Path) -> None:
    (repo / "backend/service_backend/items.py").unlink()


def _rename_linked_heading(repo: Path) -> None:
    (repo / "docs/b.md").write_text("# Reference\n\n## Another part\n", encoding="utf-8")


def _delete_named_route(repo: Path) -> None:
    main = repo / "backend/service_backend/main.py"
    main.write_text(E2E_MAIN.split('\n\n\n@app.delete("/items/{item_id}")')[0] + "\n", encoding="utf-8")


def _rename_named_route(repo: Path) -> None:
    main = repo / "backend/service_backend/main.py"
    main.write_text(E2E_MAIN.replace('"/items/{item_id}"', '"/things/{item_id}"'), encoding="utf-8")


def _unlink_doc(repo: Path) -> None:
    (repo / "docs/README.md").write_text(E2E_README.replace("- [Changelog](c.md)\n", ""), encoding="utf-8")


@pytest.fixture
def _hermetic_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git, and the CLI's git, see no user or system config (an excludes file
    there would change what is ignored); a missing git fails under
    ``CODE_INTEL_REQUIRE_TOOLCHAIN=1``."""
    hermetic_git(monkeypatch, tmp_path / "home")


@pytest.mark.usefixtures("_hermetic_git")
def test_end_to_end_a_clean_checkout_exits_0(tmp_path: Path) -> None:
    code, out, err = run_cli(e2e_repo(tmp_path / "repo"))
    assert (code, out) == (0, ""), err
    assert "0 findings in" in err
    # No TypeScript here: frontend definitions and pages were not read, which is a note only.
    assert "frontend_definitions_unavailable" in err
    assert "frontend_pages_unavailable" in err


@pytest.mark.usefixtures("_hermetic_git")
@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            _delete_named_file,
            "docs/a.md:3: stale path: backend/service_backend/items.py (not found under the repository root)",
        ),
        (_rename_linked_heading, "docs/a.md:7: broken anchor: b.md#the-part (no heading or id in docs/b.md)"),
        (
            _delete_named_route,
            "docs/a.md:5: route not found: DELETE /items/{item_id} (not in the analyzed backend routes)",
        ),
        (
            _rename_named_route,
            "docs/a.md:5: route not found: DELETE /items/{item_id} (not in the analyzed backend routes)",
        ),
        (_unlink_doc, "docs/c.md:1: orphaned doc: not reachable from docs/README.md"),
    ],
    ids=["deleted-file", "renamed-heading", "deleted-route", "renamed-route", "unlinked-doc"],
)
def test_end_to_end_a_code_change_fails_the_check_with_its_line(tmp_path: Path, mutate, expected: str) -> None:
    repo = e2e_repo(tmp_path / "repo")
    mutate(repo)
    # What CI checks out: the change is in the index, not only on disk.
    git(repo, "add", "-A")
    code, out, err = run_cli(repo)
    assert (code, out.splitlines()) == (1, [expected]), err


@pytest.mark.usefixtures("_hermetic_git")
def test_end_to_end_a_rule_glob_matching_nothing_fails(tmp_path: Path) -> None:
    repo = e2e_repo(tmp_path / "repo")
    (repo / ".claude/rules/api.md").write_text(API_RULE.replace("backend/**", "backend/old/**"), encoding="utf-8")
    (repo / "AGENTS.md").write_text(AGENTS_MD.replace("backend/**", "backend/old/**"), encoding="utf-8")
    git(repo, "add", "-A")
    code, out, err = run_cli(repo)
    assert (code, out.splitlines()) == (
        1,
        [".claude/rules/api.md:3: paths glob 'backend/old/**' matches no git-visible file"],
    ), err


@pytest.mark.usefixtures("_hermetic_git")
def test_end_to_end_react_router_pages_resolve_bare_paths(
    tmp_path: Path, typescript_toolchain: ts_bridge.Toolchain
) -> None:
    assert typescript_toolchain.typescript_dir is not None
    env = {ts_bridge.TS_DIR_ENV: str(typescript_toolchain.typescript_dir)}
    repo = e2e_repo(tmp_path / "repo")
    (repo / "docs/a.md").write_text(
        E2E_A + "\nOpen `/home`, or `/groups/{id}` for one group, from the app.\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    code, out, err = run_cli(repo, env)
    assert (code, out) == (0, ""), err
    assert "frontend_pages_unavailable" not in err
    (repo / "docs/a.md").write_text(E2E_A + "\nOpen `/home` or `/groups/{id}/edit` in the app.\n", encoding="utf-8")
    git(repo, "add", "-A")
    code, out, err = run_cli(repo, env)
    assert code == 1, err
    assert out.splitlines() == ["docs/a.md:9: route not found: /groups/{id}/edit (not in the analyzed backend routes)"]


@pytest.mark.usefixtures("_hermetic_git")
def test_end_to_end_main_refuses_a_missing_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    code, out, err = run_cli(repo)
    assert (code, out) == (2, "")
    assert "code-intel:" in err


def test_the_command_is_listed_by_the_cli() -> None:
    from code_intel.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["doc-check", "--json"])
    assert (args.command, args.json) == ("doc-check", True)
