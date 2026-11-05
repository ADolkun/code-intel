"""Docs-vs-code references (``doc_refs``, ``doc_rules``, ``doc_report``):
links, anchors, paths, routes, symbols, scope rules, config, ignore entries,
orphans and the per-file caches, over fixture repositories in tmp dirs."""

from __future__ import annotations

import os
import subprocess
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from doc_config import FAKE_ROOT, LAYOUT, config_text, doc_config
from fake_repo_files import FakeRepoFiles
from fixture_repo import hermetic_git

from code_intel import code_paths, doc_presenters, doc_queries, doc_refs
from code_intel.code_paths import FileStamp
from code_intel.config import CONFIG_REL, ConfigError, load_config, parse_config
from code_intel.doc_refs import DocIndex, Reference
from code_intel.doc_report import symbol_rows
from code_intel.doc_rules import (
    CATCH,
    CATCH_OPT,
    PARAM,
    STAGE,
    DocRefsConfig,
    NextAppPages,
    NoPages,
    PageProvider,
    PageSet,
    backend_route_segments,
    expand_braces,
    glob_match,
    is_camel_case,
    match_segments,
    mention_segments,
    parse_route_text,
    path_tokens,
    qualifiers_match,
    route_path_variants,
    route_root,
    split_path_token,
)
from code_intel.problems import limitation
from code_intel.repo_files import list_repo_files

PAGES = NextAppPages("frontend/app", "frontend/public")


# ── Fixture repository ───────────────────────────────────────────────────


def _parents(path: str) -> set[str]:
    out = set()
    parent = os.path.dirname(path)
    while parent:
        out.add(parent)
        parent = os.path.dirname(parent)
    return out


APP_PY = '''\
"""Module docstring mentions _stale_helper."""
# OldHelper() was removed
import os


class AppointmentService:
    def approve(self):
        return _helper()


class HeartbeatConfig:
    retry = 1


def _helper(config):
    settings = {"key": "cache_ttl_seconds"}
    value = _used_private()
    config.pg_max_retry_seconds
    return len(settings), value, os


used_only_in_code = 1
RETRY_PRIVATE_SECONDS = 5
'''

BASE_FILES: dict[str, str] = {
    "backend/app.py": APP_PY,
    "backend/util.py": "x = 1\n",
    "backend/scripts/compile.sh": "echo compile\n",
    "tools/t/tests/test_a.py": "def test_a():\n    pass\n",
    "backend/tests/test_x.py": "def test_y():\n    pass\n",
    "backend/service_backend/api/routes/articles.py": "x = 1\n",
    "frontend/lib/api.ts": "export const api = 1;\n",
    "frontend/components/ArticleCard.tsx": "export function ArticleCard() { return <UsedCard />; }\n",
    "frontend/app/articles/[id]/page.tsx": "export default function Page() { return null; }\n",
    "frontend/app/(admin)/settings/page.tsx": "export default function Page() { return null; }\n",
    "frontend/app/api/gateway/[...path]/route.ts": "export const GET = 1;\n",
    "frontend/public/logo.svg": "<svg/>\n",
    "docs/README.md": "# Docs\n",
    "docs/b.md": '# B title\n\n## Section\n\n<a id="custom"></a>\n',
    "docs/sub/x.md": "# X\n",
}

ROUTES: list[dict[str, Any]] = [
    {"method": "GET", "path": "/appointments/{appointment_id}", "handler": "get_appointment", "line": 10},
    {"method": "POST", "path": "/appointments/{appointment_id}/checkout", "handler": "checkout", "line": 20},
    {"method": "GET", "path": "/appointments/{appointment_id}/drive-report", "handler": "get_report", "line": 30},
    {"method": "POST", "path": "/appointments/{appointment_id}/drive-report", "handler": "post_report", "line": 40},
    {"method": "GET", "path": "/articles/{article_id}", "handler": "get_article", "line": 50},
    {"method": "GET", "path": "/api/auth/me", "handler": "me", "line": 60},
    {"method": "WS", "path": "/ws/heartbeat/{article_id}", "handler": "heartbeat", "line": 70, "kind": "websocket"},
    {"method": "GET", "path": "/health", "handler": "health", "line": 80},
]
ROUTES = [{"kind": "http", "file": "backend/app.py", **row} for row in ROUTES]
UNMOUNTED: list[dict[str, Any]] = [
    {
        "method": "GET",
        "path": "/legacy/thing",
        "kind": "http",
        "handler": "legacy",
        "file": "backend/util.py",
        "line": 1,
    }
]

SYMBOLS: list[dict[str, Any]] = [
    {"name": "AppointmentService", "kind": "class", "container": None, "file": "backend/app.py", "line": 6},
    {"name": "approve", "kind": "method", "container": "AppointmentService", "file": "backend/app.py", "line": 7},
    {"name": "HeartbeatConfig", "kind": "class", "container": None, "file": "backend/app.py", "line": 11},
    {"name": "retry", "kind": "variable", "container": "HeartbeatConfig", "file": "backend/app.py", "line": 12},
    {"name": "_helper", "kind": "function", "container": None, "file": "backend/app.py", "line": 15},
    {"name": "UntrackedThing", "kind": "class", "container": None, "file": "backend/scratch.py", "line": 1},
]
TS_SYMBOLS: list[dict[str, Any]] = [
    {
        "name": "ArticleCard",
        "kind": "component",
        "container": None,
        "file": "frontend/components/ArticleCard.tsx",
        "line": 1,
        "language": "typescript",
    }
]


def make_repo(
    root: Path,
    files: Mapping[str, str],
    *,
    untracked: Iterable[str] = (),
    ignored: Iterable[str] = (),
    uninitialized: Iterable[str] = (),
) -> FakeRepoFiles:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    listed = frozenset(files)
    subs = frozenset(uninitialized)
    dirs = set(subs)
    for rel in listed | subs:
        dirs |= _parents(rel)
    return FakeRepoFiles(
        repo_root=root,
        files=listed,
        tracked=listed - frozenset(untracked),
        dirs=frozenset(dirs),
        submodules=subs,
        uninitialized_submodules=subs,
        ignored_paths=frozenset(ignored),
    )


def build(
    tmp_path: Path,
    docs: Mapping[str, str],
    *,
    routes: list[dict[str, Any]] | None = None,
    symbols: list[Any] | None = None,
    frontend_available: bool = True,
    cfg: doc_refs.DocRefsConfig | None = None,
    route_limitations: list[dict[str, Any]] | None = None,
    pages: PageProvider = PAGES,
    **repo_kw: Any,
) -> doc_refs.DocReport:
    files = {**BASE_FILES, **{rel: textwrap.dedent(text).lstrip("\n") for rel, text in docs.items()}}
    repo = make_repo(tmp_path / "repo", files, **repo_kw)
    return DocIndex(repo.repo_root).report(
        repo,
        ROUTES if routes is None else routes,
        UNMOUNTED,
        route_limitations or [],
        [*SYMBOLS, *TS_SYMBOLS] if symbols is None else symbols,
        frontend_available,
        cfg or doc_config(),
        pages=pages,
    )


def refs(report: doc_refs.DocReport, doc: str = "docs/a.md", kind: str | None = None) -> list[Reference]:
    return [r for r in report.docs[doc].references if kind is None or r.kind == kind]


def lookup(report: doc_refs.DocReport, **query: Any) -> Any:
    """``doc_queries.doc_refs_lookup`` over ``report``, every row."""
    return doc_queries.doc_refs_lookup(report, limit=None, **query)


def ref_findings(report: doc_refs.DocReport) -> list[doc_refs.Finding]:
    """Findings other than orphans (the fixture's extra docs are unlinked)."""
    return [f for f in report.findings() if f.kind != "orphan"]


def one(report: doc_refs.DocReport, kind: str, doc: str = "docs/a.md") -> Reference:
    found = refs(report, doc, kind)
    assert len(found) == 1, found
    return found[0]


# ── Links and anchors ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("markdown", "status", "detail"),
    [
        ("[x](missing.md)", "missing", "docs/missing.md does not exist"),
        ("[x](gone/)", "missing", "docs/gone does not exist"),
        ("[x](sub/)", "ok", None),
        ("[x](sub/#anything)", "not_checked", "no README.md or index.md to check the anchor against"),
        ("[x](/)", "ok", None),  # the repository root is a directory
        ("[x](../)", "ok", None),
        ("[x](sub/x.md)", "ok", None),
        ("[x](../backend/app.py)", "ok", None),
        ("[x](/backend/app.py)", "ok", None),
        ("[x](../../outside.md)", "missing", "points outside the repository"),
        ("[x](https://example.com/a.md)", "skipped", "external link"),
        ("[x](mailto:a@example.com)", "skipped", "external link"),
        ("[x](//cdn.example.com/a.js)", "skipped", "external link"),
        ("[x](blob:abc)", "skipped", "external link"),
        ("[x](#a-title)", "ok", None),
        ("[x](#A-TITLE)", "anchor_missing", "no heading or id in docs/a.md"),
        ("[x](#nope)", "anchor_missing", "no heading or id in docs/a.md"),
        ("[x](#)", "anchor_missing", "empty anchor"),
        # A query alone is the page itself; a fragment after it is checked.
        ("[x](?plain=1)", "ok", None),
        ("[x](?plain=1#a-title)", "ok", None),
        ("[x](?plain=1#nope)", "anchor_missing", "no heading or id in docs/a.md"),
        ("[x]()", "skipped", "empty link target"),
        ("![diagram]()", "skipped", "empty link target"),
        ("[x](b.md#section)", "ok", None),
        ("[x](b.md#custom)", "ok", None),
        ("[x](b.md#nope)", "anchor_missing", "no heading or id in docs/b.md"),
        ("[x](b.md?plain=1#section)", "ok", None),
        ("[x](../backend/app.py#L999)", "ok", None),
        ('<a href="b.md#nope">x</a>', "anchor_missing", "no heading or id in docs/b.md"),
        ("![img](missing.png)", "missing", "docs/missing.png does not exist"),
    ],
)
def test_links_and_anchors(tmp_path: Path, markdown: str, status: str, detail: str | None) -> None:
    report = build(tmp_path, {"docs/a.md": f"# A title\n\n{markdown}\n"})
    link = one(report, "link")
    assert (link.status, link.detail) == (status, detail)
    assert link.line == 3


def test_a_root_link_fragment_is_checked_against_the_root_readme(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        {"README.md": "# Project\n", "docs/a.md": "[x](/#project)\n[y](/#nope)\n[z](/)\n"},
    )
    assert [(r.status, r.detail, r.checked) for r in refs(report, kind="link")] == [
        ("ok", None, True),
        ("anchor_missing", "no heading or id in README.md", True),
        ("ok", None, True),
    ]


def test_a_root_link_fragment_without_a_root_readme_is_not_checked(tmp_path: Path) -> None:
    link = one(build(tmp_path, {"docs/a.md": "[x](/#anything)\n"}), "link")
    assert (link.status, link.detail, link.checked) == (
        "not_checked",
        "no README.md or index.md to check the anchor against",
        False,
    )


def test_external_links_are_outbound_but_not_checked(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "[x](https://example.com)\n[y](b.md)\n"})
    outbound = report.docs["docs/a.md"].outbound
    assert [(r.text, r.status, r.checked) for r in outbound] == [
        ("https://example.com", "skipped", False),
        ("b.md", "ok", True),
    ]


def test_link_into_uninitialized_submodule_is_not_checked(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        {"docs/a.md": "[x](../backend/packages/core_security/README.md)\n"},
        uninitialized=["backend/packages/core_security"],
    )
    link = one(report, "link")
    assert (link.status, link.checked) == ("not_checked", False)
    assert [p.kind for p in report.limitations] == ["submodule_missing"]
    assert report.complete


# ── Paths ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("`backend/app.py`", [("backend/app.py", "ok", "backend/app.py")]),
        ("`backend/gone.py`", [("backend/gone.py", "missing", "backend/gone.py")]),
        ("`backend/app.py:12`", [("backend/app.py:12", "ok", "backend/app.py")]),
        ("`backend/app.py:12-30`", [("backend/app.py:12-30", "ok", "backend/app.py")]),
        ("`backend/app.py:12–30`", [("backend/app.py:12–30", "ok", "backend/app.py")]),
        ("`backend/app.py#L3-L5`", [("backend/app.py#L3-L5", "ok", "backend/app.py")]),
        (
            "`backend/tests/test_x.py::test_y`",
            [("backend/tests/test_x.py::test_y", "ok", "backend/tests/test_x.py")],
        ),
        ("`./backend/app.py`", [("./backend/app.py", "ok", "backend/app.py")]),
        ("`./nope/x.md`", [("./nope/x.md", "missing", "docs/nope/x.md")]),
        ("`bash ./scripts/compile.sh`", [("./scripts/compile.sh", "ok", "backend/scripts/compile.sh")]),
        ("`routes/*.py`", [("routes/*.py", "ok", "backend/service_backend/api/routes/articles.py")]),
        ("`routes/*.sql`", [("routes/*.sql", "missing", None)]),
        ("`cat backend/gone.py`", [("backend/gone.py", "missing", "backend/gone.py")]),
        ("`uv run pytest backend/tests/test_x.py -q`", [("backend/tests/test_x.py", "ok", "backend/tests/test_x.py")]),
        ("`ruff --config=backend/app.py`", [("backend/app.py", "ok", "backend/app.py")]),
        ("`service_backend/api/routes/articles.py`", [("service_backend/api/routes/articles.py", "ok", None)]),
        ("`api/routes/users.py`", [("api/routes/users.py", "missing", "backend/service_backend/api/routes/users.py")]),
        ("`tests/test_x.py`", [("tests/test_x.py", "ok", "backend/tests/test_x.py")]),
        ("`lib/api.ts`", [("lib/api.ts", "ok", "frontend/lib/api.ts")]),
        ("`../backend/app.py`", [("../backend/app.py", "ok", "backend/app.py")]),
        ("`./b.md`", [("./b.md", "ok", "docs/b.md")]),
        ("`./nope.md`", [("./nope.md", "missing", "docs/nope.md")]),
        ("`backend/{app,util}.py`", [("backend/{app,util}.py", "ok", "backend/app.py")]),
        ("`backend/{app,gone}.py`", [("backend/{app,gone}.py", "missing", "backend/gone.py")]),
        ("`backend/**/*.py`", [("backend/**/*.py", "ok", None)]),
        ("`backend/**/*.rs`", [("backend/**/*.rs", "missing", "backend/**/*.rs")]),
        ("`backend/app.AppointmentService`", [("backend/app.AppointmentService", "ok", "backend/app.py")]),
        ("`frontend/lib/api`", [("frontend/lib/api", "ok", "frontend/lib/api.ts")]),
        ("`frontend/app/articles/[id]/page.tsx`", [("frontend/app/articles/[id]/page.tsx", "ok", None)]),
        ("`backend/`", [("backend/", "ok", None)]),
        ("`docs/<doc>.md`", [("docs/<doc>.md", "skipped", None)]),
        ("`backend/{name}.py`", [("backend/{name}.py", "skipped", None)]),
        ("`backend/.../x.py`", [("backend/.../x.py", "skipped", None)]),
        ("`backend/${DIR}/x.py`", [("backend/${DIR}/x.py", "skipped", None)]),
        ("`backend/migrations/NNNN_x.py`", [("backend/migrations/NNNN_x.py", "skipped", None)]),
        ("`backend/my_service.py`", [("backend/my_service.py", "skipped", None)]),
        ("`frontend/app/my-feature/page.tsx`", [("frontend/app/my-feature/page.tsx", "skipped", None)]),
        ("`frontend/node_modules/x/index.js`", [("frontend/node_modules/x/index.js", "local_only", None)]),
        ("`backend/.venv/bin/python`", [("backend/.venv/bin/python", "local_only", None)]),
        ("`heartbeat-canonical-fence/active.json`", [("heartbeat-canonical-fence/active.json", "skipped", None)]),
        ("`routes/users.py`", [("routes/users.py", "missing", "routes/users.py")]),
        ("`routes/articles.py`", [("routes/articles.py", "ok", "backend/service_backend/api/routes/articles.py")]),
        ("`app.py`", []),
        ("`application/json`", []),
        ("`https://example.com/a.py`", []),
        ("`~/.config/tool/settings.json`", []),
        ("`/var/lib/postgresql/x.py`", []),
    ],
)
def test_path_spans(tmp_path: Path, markdown: str, expected: list[tuple[str, str, str | None]]) -> None:
    report = build(tmp_path, {"docs/a.md": f"{markdown}\n"})
    got = refs(report, kind="path")
    assert [(r.text, r.status) for r in got] == [(text, status) for text, status, _ in expected]
    # A None target in the table means the target is not the point of the row.
    for ref, (_, _, target) in zip(got, expected, strict=True):
        if target is not None:
            assert ref.target == target


def test_missing_path_messages_name_the_roots_tried(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`api/routes/users.py` and `backend/gone.py`\n`./nope.md`\n"})
    messages = [f.message for f in ref_findings(report)]
    assert messages == [
        "stale path: api/routes/users.py (not found under backend/service_backend/ or docs/)",
        "stale path: backend/gone.py (not found under the repository root)",
        "stale path: ./nope.md (docs/nope.md does not exist)",
    ]


def test_tool_readme_paths_resolve_against_its_directory(tmp_path: Path) -> None:
    report = build(tmp_path, {"tools/t/README.md": "`tests/test_a.py` and `tests/test_gone.py`\n"})
    assert [(r.text, r.status, r.target) for r in refs(report, "tools/t/README.md", "path")] == [
        ("tests/test_a.py", "ok", "tools/t/tests/test_a.py"),
        ("tests/test_gone.py", "missing", "backend/tests/test_gone.py"),
    ]


def test_fences_check_only_root_paths(tmp_path: Path) -> None:
    doc = """
    ```bash
    cat backend/gone.py
    pytest tests/gone.py ../AppointmentPanel service_backend/gone.py
    ```
    """
    report = build(tmp_path, {"docs/a.md": doc})
    assert [(r.text, r.status, r.line, r.context) for r in refs(report, kind="path")] == [
        ("backend/gone.py", "missing", 2, "fence")
    ]


def test_gitignored_paths_are_local_only(tmp_path: Path) -> None:
    report = build(
        tmp_path, {"docs/a.md": "`frontend/.env.local` and `frontend/gone.ts`\n"}, ignored=["frontend/.env.local"]
    )
    assert [(r.text, r.status) for r in refs(report, kind="path")] == [
        ("frontend/.env.local", "local_only"),
        ("frontend/gone.ts", "missing"),
    ]


def test_ignored_paths_are_asked_in_one_batch(tmp_path: Path) -> None:
    files = {**BASE_FILES, "docs/a.md": "`backend/a.py` `backend/b.py`\n\n[x](gone.md)\n"}
    repo = make_repo(tmp_path / "repo", files)
    DocIndex(repo.repo_root).report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    assert repo.asked == [
        {"backend/a.py", "backend/a.py/", "backend/b.py", "backend/b.py/", "docs/gone.md", "docs/gone.md/"}
    ]


def test_path_into_uninitialized_submodule_is_not_checked(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        {"docs/a.md": "`backend/packages/core_security/security.py`\n"},
        uninitialized=["backend/packages/core_security"],
    )
    path = one(report, "path")
    assert (path.status, path.detail, path.checked) == (
        "not_checked",
        "submodule backend/packages/core_security is not checked out",
        False,
    )
    assert [(p.kind, p.file) for p in report.limitations] == [("submodule_missing", "backend/packages/core_security")]
    assert ref_findings(report) == []


def test_real_git_repository_marks_ignored_paths_local_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermetic_git(monkeypatch, tmp_path / "home")
    root = tmp_path / "git"
    files = {".gitignore": ".env\nbuild/\n", "docs/README.md": "`docs/.env` `docs/build/out.py` `docs/gone.py`\n"}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    repo = list_repo_files(root)
    report = DocIndex(root).report(repo, [], [], [], [], True, doc_config())
    assert [(r.text, r.status) for r in refs(report, "docs/README.md", "path")] == [
        ("docs/.env", "local_only"),
        ("docs/build/out.py", "local_only"),
        ("docs/gone.py", "missing"),
    ]


def test_paths_ending_inside_an_uninitialized_submodule_are_not_checked(tmp_path: Path) -> None:
    # `core_security/…` and `libs/core_security/…` could end a path inside the
    # submodule; `libs/gone.py` could not, although `libs` is one of its parents.
    report = build(
        tmp_path,
        {"docs/a.md": "`core_security/x.py` `libs/core_security/x.py` `libs/gone.py`\n"},
        uninitialized=["vendor/libs/core_security"],
    )
    assert [(r.text, r.status, r.checked) for r in refs(report, kind="path")] == [
        ("core_security/x.py", "not_checked", False),
        ("libs/core_security/x.py", "not_checked", False),
        ("libs/gone.py", "missing", True),
    ]


def test_dot_slash_in_a_fence_is_not_doc_relative(tmp_path: Path) -> None:
    # In a fence `./x` is relative to the shell's directory: a root path is
    # still checked from the repository root, anything else is not a path here.
    report = build(tmp_path, {"docs/a.md": "```bash\ncat ./backend/gone.py ./b.md ./nope/x.md\n```\n"})
    assert [(r.text, r.status, r.target) for r in refs(report, kind="path")] == [
        ("./backend/gone.py", "missing", "backend/gone.py")
    ]


@pytest.mark.parametrize(
    ("text", "form", "tokens"),
    [
        ("backend/app.py", "span", ["backend/app.py"]),
        ("(see backend/app.py).", "command", ["see", "backend/app.py"]),
        ('open("backend/app.py")', "command", ["backend/app.py"]),
        ("a.py,b.py", "span", ["a.py", "b.py"]),
        ("backend/{a,b}.py", "span", ["backend/{a,b}.py"]),
        ("'backend/app.py',", "fence", ["backend/app.py"]),
        ("[frontend/app/[id]/page.tsx]", "span", ["frontend/app/[id]/page.tsx"]),
        ("(backend/app.py)", "span", ["backend/app.py"]),
    ],
)
def test_path_tokens(text: str, form: Any, tokens: list[str]) -> None:
    assert path_tokens(text, form) == tokens


def test_split_path_token_strips_suffixes() -> None:
    def path_of(token: str) -> str | None:
        parsed = split_path_token(token)
        return parsed.path if parsed is not None else None

    assert path_of("backend/app.py:12:4") == "backend/app.py"
    assert path_of("backend/app.py#symbol") == "backend/app.py"
    assert path_of("tests/test_x.py::TestA::test_b[x-1]") == "tests/test_x.py"
    assert split_path_token("--flag") is None
    assert split_path_token("plain") is None
    assert expand_braces("a/{b,c}/{d,e}.py") == ["a/b/d.py", "a/b/e.py", "a/c/d.py", "a/c/e.py"]


# ── Routes ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("markdown", "status", "target"),
    [
        ("`GET /appointments/{id}`", "ok", "GET /appointments/{appointment_id}"),
        ("`GET /appointments/:id`", "ok", "GET /appointments/{appointment_id}"),
        ("`GET /api/appointments/{id}`", "ok", "GET /appointments/{appointment_id}"),
        ("`GET /api/auth/me`", "ok", "GET /api/auth/me"),
        ("`GET /auth/me`", "ok", "GET /api/auth/me"),
        ("`PUT /appointments/{id}/drive-report`", "method_differs", "GET /appointments/{appointment_id}/drive-report"),
        ("`GET /articles/camo-types`", "weak_param", "GET /articles/{article_id}"),
        ("`GET /articles/42`", "ok", "GET /articles/{article_id}"),
        ("`/appointments`", "prefix", "GET /appointments/{appointment_id}"),
        ("`/appointments/*`", "prefix", "GET /appointments/{appointment_id}"),
        ("`GET /appointments`", "missing", None),
        ("`POST /checkout`", "suffix", "POST /appointments/{appointment_id}/checkout"),
        ("`/settings`", "frontend", "/settings"),
        ("`/articles/[id]`", "ok", "GET /articles/{article_id}"),
        ("`/api/gateway/flags/x`", "frontend", "/api/gateway/[...path]"),
        ("`/logo.svg`", "frontend", "/logo.svg"),
        ("`GET /openapi.json`", "framework", None),
        ("`GET /legacy/thing`", "unmounted", "GET /legacy/thing"),
        ("`WebSocket /ws/heartbeat/{id}`", "ok", "WS /ws/heartbeat/{article_id}"),
        ("`WS /ws/heartbeat/{id}`", "ok", "WS /ws/heartbeat/{article_id}"),
        ("`GET /ws/heartbeat/{id}`", "method_differs", "WS /ws/heartbeat/{article_id}"),
        ("`HEAD /health`", "ok", "GET /health"),
        ("`GET /health?verbose=1`", "ok", "GET /health"),
        ("`GET /health/`", "missing", None),
        ("`/var/lib/x`", "skipped", None),
        ("`/groups/{id}`", "skipped", None),
        ("`/api/v1`", "skipped", None),
        ("`GET /var/lib/x`", "missing", None),
        ("`GET /appointments/${id}`", "skipped", None),
        ("`GET /appointments/{{id}}`", "skipped", None),
        ("`GET /api/...`", "skipped", None),
        # `.../tail` names the end of a route: in a span, a table, a fence and prose.
        ("`POST .../checkout`", "suffix", "POST /appointments/{appointment_id}/checkout"),
        ("`POST …/{id}/checkout`", "suffix", "POST /appointments/{appointment_id}/checkout"),
        ("| `POST .../checkout` | x |\n|---|---|\n| a | b |", "suffix", "POST /appointments/{appointment_id}/checkout"),
        ('```mermaid\nA -->|Go| B["POST .../checkout"]\n```', "suffix", "POST /appointments/{appointment_id}/checkout"),
        ("Call POST .../checkout first.", "suffix", "POST /appointments/{appointment_id}/checkout"),
        ("`GET .../checkout`", "method_differs", "POST /appointments/{appointment_id}/checkout"),
        ("`POST .../jira/link`", "missing", None),
        ("`post .../checkout`", "skipped", None),
        ("`GET /appointments/.../checkout`", "skipped", None),
        ("`GET /appointments/*`", "prefix", "GET /appointments/{appointment_id}"),
        ("`POST /health/*`", "missing", None),
        ("`/api/gateway`", "frontend", "/api/gateway/[...path]"),
        ("`/api/gateway/*`", "frontend", "/api/gateway/[...path]"),
        ("See [POST /appointments/{id}/checkout] first.", "ok", "POST /appointments/{appointment_id}/checkout"),
        ("Call **GET** /health first.", "ok", "GET /health"),
        ("| `/appointments/{id}` | x |\n|---|---|\n| a | b |", "ok", "GET /appointments/{appointment_id}"),
    ],
)
def test_routes(tmp_path: Path, markdown: str, status: str, target: str | None) -> None:
    report = build(tmp_path, {"docs/a.md": f"{markdown}\n"})
    route = one(report, "route")
    assert (route.status, route.target) == (status, target)


def test_a_route_tail_needs_a_method_and_fails_naming_the_tail(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`.../checkout` `POST .../jira/link`\n"})
    route = one(report, "route")
    assert (route.text, route.route) == ("POST .../jira/link", ("POST", ".../jira/link"))
    assert [f.message for f in ref_findings(report)] == [
        "route not found: POST .../jira/link (no live route ends with /jira/link)"
    ]


def test_a_route_query_finds_a_tail_that_ends_its_path(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`POST .../checkout` `GET .../drive-report` `POST .../jira/link`\n"})

    def texts(route: str) -> list[str]:
        return [r["text"] for r in lookup(report, route=route).mentions.items]

    assert texts("POST /api/appointments/{id}/checkout") == ["POST .../checkout"]
    assert texts("/appointments/42/checkout") == ["POST .../checkout"]
    assert texts("/appointments/{id}/drive-report") == ["GET .../drive-report"]
    assert texts("POST /appointments/{id}/drive-report") == [], "the methods differ"
    assert texts("/checkout") == ["POST .../checkout"], "the ellipsis may stand for nothing"
    assert texts("/jira/link/x") == []


APPROVALS: list[dict[str, Any]] = [
    {"method": "POST", "path": path, "kind": "http", "handler": "approve", "file": "backend/app.py", "line": line}
    for path, line in (("/api/access-requests/{request_id}/approve", 91), ("/scheduled-grants/{grant_id}/approve", 92))
]


def test_a_route_tail_several_routes_end_with_fails_and_is_still_found(tmp_path: Path) -> None:
    # The appointment approval is gone: two other approvals still end with
    # the tail, so it fails instead of passing on either of them.
    report = build(tmp_path, {"docs/a.md": "`POST .../approve`\n"}, routes=[*ROUTES, *APPROVALS])
    route = one(report, "route")
    assert (route.status, route.target, route.defined_in) == (
        "ambiguous",
        None,
        ("backend/app.py:91", "backend/app.py:92"),
    )
    message = (
        "route not specific: POST .../approve (2 live routes end with /approve: "
        "POST /api/access-requests/{request_id}/approve, POST /scheduled-grants/{grant_id}/approve; "
        "write the full path)"
    )
    assert [f.message for f in ref_findings(report)] == [message]
    # It still mentions every route it ends, so a lookup of one finds it.
    for query in ("POST /appointments/{id}/approve", "/scheduled-grants/{id}/approve"):
        body = doc_presenters.doc_refs(lookup(report, route=query))
        assert body.text.splitlines()[-1] == (f"  docs/a.md:1  route  POST .../approve  ambiguous ({route.detail})"), (
            query
        )
        [row] = body.payload["mentions"]
        assert (row["status"], row["message"], row["ambiguous"]) == ("ambiguous", message, None)
    assert lookup(report, file="backend/app.py").mentions.items == [], (
        "an ambiguous tail names no single route, so it is not tied to a file"
    )
    single = build(tmp_path / "one", {"docs/a.md": "`POST .../approve`\n"}, routes=[*ROUTES, APPROVALS[1]])
    assert (one(single, "route").status, one(single, "route").target) == (
        "suffix",
        "POST /scheduled-grants/{grant_id}/approve",
    )


def test_route_rows_carry_handler_locations_and_parsed_mention(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`POST /api/appointments/{id}/checkout`\n"})
    route = one(report, "route")
    assert route.defined_in == ("backend/app.py:20",)
    assert route.route == ("POST", "/api/appointments/{id}/checkout")
    assert route.checked


@pytest.mark.parametrize(
    ("markdown", "status"),
    [
        ("`GET /exports/{name}.csv`", "ok"),
        ("`GET /exports/{id}.csv`", "ok"),
        ("`GET /exports/42.csv`", "ok"),
        ("`GET /exports/report.csv`", "weak_param"),
    ],
)
def test_parameter_inside_a_segment_matches_as_defined(tmp_path: Path, markdown: str, status: str) -> None:
    exports = {"method": "GET", "path": "/exports/{name}.csv", "kind": "http", "file": "backend/app.py", "line": 90}
    report = build(tmp_path, {"docs/a.md": f"{markdown}\n"}, routes=[*ROUTES, {"handler": "export", **exports}])
    route = one(report, "route")
    assert (route.status, route.target) == (status, "GET /exports/{name}.csv")
    assert (
        mention_segments("/exports/{name}.csv") == backend_route_segments("/exports/{name}.csv") == ("exports", PARAM)
    )


def test_exact_frontend_page_beats_a_word_in_a_backend_parameter(tmp_path: Path) -> None:
    page = "frontend/app/articles/compare/page.tsx"
    report = build(
        tmp_path,
        {"docs/a.md": "`/articles/compare` `/articles/camo-types`\n", page: "export default function P() {}\n"},
    )
    assert [(r.text, r.status, r.target, r.defined_in) for r in refs(report, kind="route")] == [
        ("/articles/camo-types", "weak_param", "GET /articles/{article_id}", ("backend/app.py:50",)),
        ("/articles/compare", "frontend", "/articles/compare", (f"{page}:1",)),
    ]


def test_pages_and_routes_of_untracked_files_do_not_count(tmp_path: Path) -> None:
    # CI's checkout has neither file, so locally the page may not make the
    # mention a frontend route nor the route make it live.
    page = "frontend/app/articles/compare/page.tsx"
    scratch_route = {
        "method": "GET",
        "path": "/scratch/{thing_id}",
        "kind": "http",
        "handler": "scratch",
        "file": "backend/scratch.py",
        "line": 1,
    }
    report = build(
        tmp_path,
        {
            "docs/a.md": "`/articles/compare` `GET /scratch/{thing_id}`\n",
            page: "export default function P() {}\n",
            "backend/scratch.py": "x = 1\n",
        },
        routes=[*ROUTES, scratch_route],
        untracked=[page, "backend/scratch.py"],
    )
    found = {r.text: r.status for r in refs(report, kind="route")}
    assert found["/articles/compare"] == "weak_param"
    assert found["GET /scratch/{thing_id}"] == "missing"
    assert all(row.file != "backend/scratch.py" for row in report.routes)


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (None, False),
        ("", False),
        ("backend/app.py", False),  # tracked file
        ("backend", False),  # listed directory
        ("service_backend.main", False),  # a module name, not a path
        ("../outside.py", False),
        ("/etc/passwd", False),
        ("a\x00b", False),
        ("backend/scratch.py", True),  # untracked, git-visible
        ("backend/ignored.py", True),  # on disk, not listed (ignored)
        ("backend/gone.py", True),  # path-shaped, in a listed directory
        ("nowhere/gone.py", False),
    ],
)
def test_untracked_path(tmp_path: Path, file: str | None, expected: bool) -> None:
    repo = make_repo(
        tmp_path, {"backend/app.py": "x = 1\n", "backend/scratch.py": "y = 1\n"}, untracked=["backend/scratch.py"]
    )
    (tmp_path / "backend" / "ignored.py").write_text("z = 1\n", encoding="utf-8")
    assert doc_refs.untracked_path(repo, file) is expected


def test_untracked_path_finds_an_ignored_file_at_the_root(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"backend/app.py": "x = 1\n"})
    (tmp_path / "ignored_root.py").write_text("z = 1\n", encoding="utf-8")
    assert doc_refs.untracked_path(repo, "ignored_root.py")


def test_route_path_variants() -> None:
    api = ("/api",)
    assert route_path_variants("/api/appointments/{id}", api) == ("/api/appointments/{id}", "/appointments/{id}")
    assert route_path_variants("/api/", api) == ("/api/", "/")
    assert route_path_variants("/api", api) == ("/api",)
    assert route_path_variants("/api?x=1", api) == ("/api?x=1",)
    assert route_path_variants("/apiary/x", api) == ("/apiary/x", "/api/apiary/x")
    assert route_path_variants("/appointments", api) == ("/appointments", "/api/appointments")
    assert route_path_variants("/appointments?x=1#a", api) == ("/appointments?x=1#a", "/api/appointments?x=1#a")
    assert route_path_variants("/appointments", ()) == ("/appointments",)
    assert route_path_variants("/api/v1/x", ("/api", "/api/v1")) == ("/api/v1/x", "/v1/x", "/x")
    assert route_path_variants("/x", ("/api", "/v2")) == ("/x", "/api/x", "/v2/x")


def test_route_root_skips_the_longest_configured_prefix() -> None:
    assert route_root(("api", "v1", "items"), ("/api", "/api/v1")) == "items"
    assert route_root(("api", "items"), ("/api", "/api/v1")) == "items"
    assert route_root(("items",), ("/api",)) == "items"
    assert route_root(("api",), ("/api",)) == "api"
    assert route_root(("api", "items"), ()) == "api"
    assert route_root((), ("/api",)) is None


def test_route_in_fence_line(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "```http\nPOST /appointments/7/checkout HTTP/1.1\n```\n"})
    route = one(report, "route")
    assert (route.status, route.line, route.context) == ("ok", 2, "fence")


@pytest.mark.parametrize(
    ("markdown", "found"),
    [
        ("`get /appointments/{id}`", [("get /appointments/{id}", "GET", "ok")]),
        ("`Post /appointments/7/checkout`", [("Post /appointments/7/checkout", "POST", "ok")]),
        ("`websocket /ws/heartbeat/{id}`", [("websocket /ws/heartbeat/{id}", "WS", "ok")]),
        (
            "```\nput /appointments/{id}/drive-report\n```",
            [("put /appointments/{id}/drive-report", "PUT", "method_differs")],
        ),
        # A method not in upper case is gated like a bare `/path`: shell and client
        # commands put a file or key path after it, not a route.
        ("```sh\nhead /var/log/app.log\n```", [("head /var/log/app.log", "HEAD", "skipped")]),
        ("`etcdctl get /registry/pods`", [("get /registry/pods", "GET", "skipped")]),
        ("`redis-cli get /cache/key`", [("get /cache/key", "GET", "skipped")]),
        ("```\nsftp> put /tmp/report.csv\n```", [("put /tmp/report.csv", "PUT", "skipped")]),
        ("`patch /etc/nginx/nginx.conf fix.diff`", [("patch /etc/nginx/nginx.conf", "PATCH", "skipped")]),
        ("`options /etc/x`", [("options /etc/x", "OPTIONS", "skipped")]),
        ("`trace /tmp/app`", [("trace /tmp/app", "TRACE", "skipped")]),
        ("`get / set`", [("get /", "GET", "skipped")]),
        ("`Head /var/log/x`", [("Head /var/log/x", "HEAD", "skipped")]),
        ("`Cloud Trace /v1/x`", [("Trace /v1/x", "TRACE", "skipped")]),
        ("`get /api/appointments/{id}`", [("get /api/appointments/{id}", "GET", "ok")]),
        ("`HEAD /health`", [("HEAD /health", "HEAD", "ok")]),
        ("You get /health in prose, and post /x too.", []),
    ],
)
def test_lowercase_methods_count_in_code_only(tmp_path: Path, markdown: str, found: list[tuple[str, str, str]]) -> None:
    report = build(tmp_path, {"docs/a.md": f"{markdown}\n"})
    assert [(r.text, r.route[0] if r.route else None, r.status) for r in refs(report, kind="route")] == found


def test_framework_paths_beat_a_word_in_a_top_level_parameter(tmp_path: Path) -> None:
    by_id = {"method": "GET", "path": "/{item_id}", "kind": "http", "handler": "by_id", "file": "backend/app.py"}
    report = build(
        tmp_path,
        {"docs/a.md": "`GET /docs` `GET /redoc` `GET /other`\n"},
        routes=[*ROUTES, {"line": 99, **by_id}],
    )
    assert [(r.text, r.status) for r in refs(report, kind="route")] == [
        ("GET /docs", "framework"),
        ("GET /other", "weak_param"),
        ("GET /redoc", "framework"),
    ]


def test_bare_framework_paths_pass_the_gate(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`/docs` `/redoc` `/openapi.json` `/docs/oauth2-redirect`\n"})
    assert {(r.text, r.status) for r in refs(report, kind="route")} == {
        ("/docs", "framework"),
        ("/redoc", "framework"),
        ("/openapi.json", "framework"),
        ("/docs/oauth2-redirect", "framework"),
    }


NO_HEALTH = [row for row in ROUTES if row["path"] != "/health"]


def test_a_root_a_checked_doc_names_with_a_method_keeps_bare_paths_checked(tmp_path: Path) -> None:
    # The declaring doc sorts after the bare one: the verdict must not depend on walk order.
    report = build(
        tmp_path,
        {"docs/a.md": "`/health` `/etc/nginx`\n", "docs/z.md": "`GET /health` `patch /etc/nginx/x.conf`\n"},
        routes=NO_HEALTH,
    )
    assert [(r.text, r.status) for r in refs(report, kind="route")] == [
        ("/etc/nginx", "skipped"),
        ("/health", "missing"),
    ]
    assert [(r.text, r.status) for r in refs(report, "docs/z.md", kind="route")] == [
        ("GET /health", "missing"),
        ("patch /etc/nginx/x.conf", "skipped"),
    ]
    found = lookup(report, route="/health").mentions.items
    assert [(r["file"], r["text"], r["status"]) for r in found] == [
        ("docs/a.md", "/health", "missing"),
        ("docs/z.md", "GET /health", "missing"),
    ]


@pytest.mark.parametrize(
    "declaring",
    [
        {},
        # Docs whose rule does not check routes declare nothing.
        {"docs/plans/p.md": "`GET /health`\n"},
        {".claude/skills/generic-a/r.md": "`GET /health`\n"},
        # Neither does a gated method, a tail or a placeholder.
        {"docs/z.md": "`get /health/x` `GET .../health` `GET /health/${id}`\n"},
    ],
)
def test_a_root_only_named_bare_stays_skipped_once_its_routes_are_gone(
    tmp_path: Path, declaring: dict[str, str]
) -> None:
    report = build(tmp_path, {"docs/a.md": "`/health`\n", **declaring}, routes=NO_HEALTH)
    route = one(report, "route")
    assert (route.text, route.status, route.checked) == ("/health", "skipped", False)


def test_route_messages(tmp_path: Path) -> None:
    doc = """
    `PUT /appointments/{id}/drive-report`
    `GET /articles/camo-types`
    `GET /legacy/thing`
    `GET /gone`
    """
    report = build(tmp_path, {"docs/a.md": doc}, route_limitations=[{"kind": "opaque_router_call"}])
    assert [f.message for f in ref_findings(report)] == [
        "route method differs: PUT /appointments/{id}/drive-report (served: GET, POST)",
        "route not found: GET /articles/camo-types (only /articles/{article_id} fits, by a word in a parameter)",
        "route not mounted: GET /legacy/thing (legacy at backend/util.py:1 is not mounted on the app)",
        "route not found: GET /gone (not in the analyzed backend routes; see the limitation below if the route "
        "is mounted by code the analysis cannot see)",
    ]


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("GET /x", ("GET", "/x")),
        ("get /x/{id}", ("GET", "/x/{id}")),
        ("WebSocket /ws", ("WS", "/ws")),
        ("/x?y=1", (None, "/x?y=1")),
        ("POST /appointments]", ("POST", "/appointments")),
        ("  /x.  ", (None, "/x")),
        ("FETCH /x", None),
        ("appointments", None),
        ("GET x", None),
        ("//cdn/x", None),
    ],
)
def test_parse_route_text(text: str, parsed: tuple[str | None, str] | None) -> None:
    assert parse_route_text(text) == parsed


def test_segment_matching() -> None:
    assert mention_segments("/a/{x:int}/[...rest]") == ("a", PARAM, CATCH)
    assert match_segments(mention_segments("/a/1"), ("a", PARAM)) == "exact"
    assert match_segments(mention_segments("/a/b"), ("a", PARAM)) == "weak"
    assert match_segments(mention_segments("/a/{x}"), ("a", "b")) is None
    assert match_segments(mention_segments("/a/"), ("a",)) is None
    # A catch-all in the mention absorbs one or more route segments ([[...x]]: zero or more).
    assert match_segments(("files", CATCH), ("files", PARAM, "raw")) == "exact"
    assert match_segments(("files", CATCH), ("files",)) is None
    assert match_segments(("files", CATCH_OPT), ("files",)) == "exact"


# ── Symbols ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("span", "status", "checked"),
    [
        ("AppointmentService", "ok", True),
        ("GoneService", "missing", True),
        ("_helper", "ok", True),
        ("_gone", "missing", True),
        ("_gone()", "missing", True),
        ("gone_call()", "missing", True),
        ("<ArticleCard />", "ok", True),
        ("<UsedCard/>", "ok", True),
        ("<GoneCard />", "missing", True),
        ("AppointmentService.approve", "ok", True),
        ("AppointmentService.approve()", "ok", True),
        ("AppointmentService.gone_attr", "missing", True),
        ("HeartbeatConfig.pg_max_retry_seconds", "missing", True),
        ("config.pg_max_retry_seconds", "ok", False),
        ("appointment.approve", "unresolved", False),
        ("someCamel", "unresolved", False),
        ("snake_thing", "unresolved", False),
        ("UPPER_THING", "unresolved", False),
        ("Single", "unresolved", False),
        ("used_only_in_code", "ok", False),
        ("_used_private()", "ok", True),
        ("cache_ttl_seconds()", "ok", True),
        ("OldHelper()", "missing", True),
        ("_stale_helper()", "missing", True),
        ("len()", "ok", True),
        ("__wrapped__", "unresolved", False),
        ("_PRIVATE_SECONDS", "ok", True),
        ("_GONE_SUFFIX", "missing", True),
        ("AppointmentService.X.value", "skipped", False),
        ("AppointmentService.MY_THING", "skipped", False),
    ],
)
def test_symbols(tmp_path: Path, span: str, status: str, checked: bool) -> None:
    report = build(tmp_path, {"docs/a.md": f"`{span}`\n"})
    symbol = one(report, "symbol")
    assert (symbol.status, symbol.checked) == (status, checked)


def test_names_only_in_untracked_files_are_missing(tmp_path: Path) -> None:
    # The file is really there and git-visible, just not tracked: neither its
    # symbol-index row nor its words may make a name resolve locally.
    scratch = "class UntrackedThing:\n    pass\n\n\ndef _scratch_only():\n    pass\n"
    report = build(
        tmp_path,
        {"docs/a.md": "`UntrackedThing` and `_scratch_only()`\n", "backend/scratch.py": scratch},
        untracked=["backend/scratch.py"],
    )
    assert (tmp_path / "repo/backend/scratch.py").is_file()
    assert [(r.text, r.status) for r in refs(report, kind="symbol")] == [
        ("UntrackedThing", "missing"),
        ("_scratch_only()", "missing"),
    ]


def test_the_config_file_is_not_code(tmp_path: Path) -> None:
    # The config names `GoneWidget` in its own ignore entry: that is no use of
    # the name, so the ignore entry is needed and used.
    entry = {"file": "docs/a.md", "kind": "symbol", "text": "GoneWidget", "reason": "illustrative"}
    report = build(
        tmp_path,
        {"docs/a.md": "`GoneWidget`\n", CONFIG_REL: config_text(ignore=[entry])},
        cfg=doc_config(ignore=[entry]),
    )
    assert [(r.text, r.status) for r in refs(report, kind="symbol")] == [("GoneWidget", "ignored")]
    assert ref_findings(report) == []


def test_symbol_definitions_are_owner_matched(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`AppointmentService.approve` and `approve` and `_helper`\n"})
    by_text = {r.text: r for r in refs(report, kind="symbol")}
    assert by_text["AppointmentService.approve"].defined_in == ("backend/app.py:7",)
    assert by_text["_helper"].defined_in == ("backend/app.py:15",)
    assert by_text["AppointmentService.approve"].detail == "defined in the symbol index"


@pytest.mark.parametrize("span", ["config.py", "<div>", "<id>", "<ENTRY>", "a b", "x(1)"])
def test_spans_that_are_not_symbols(tmp_path: Path, span: str) -> None:
    report = build(tmp_path, {"docs/a.md": f"`{span}`\n"})
    assert refs(report, kind="symbol") == []


def test_symbol_findings_message(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`_gone()`\n"})
    assert ref_findings(report) == [
        (
            "docs/a.md",
            1,
            "symbol",
            "missing",
            "_gone()",
            "symbol not found: _gone() (no definition in the symbol index and no use in tracked code)",
        )
    ]


def test_symbols_are_checked_with_uninitialized_submodules(tmp_path: Path) -> None:
    # Code that uses a submodule names what it uses, so a missing submodule
    # changes no symbol verdict.
    report = build(tmp_path, {"docs/a.md": "`GoneManager`\n"}, uninitialized=["backend/packages/core_security"])
    symbol = one(report, "symbol")
    assert (symbol.status, symbol.checked) == ("missing", True)


_MODELS = "backend/service_backend/models/appointment.py"
# What the packages' `__init__.py` files bind: `models` re-exports
# `Appointment`, `service_backend` and `pkg` re-export nothing.
_BINDINGS = {
    "backend/service_backend/models": frozenset({"Appointment"}),
    "backend/service_backend": frozenset(),
    "backend/pkg": frozenset({"helper"}),
}


@pytest.mark.parametrize(
    ("container", "file", "qualifiers", "expected"),
    [
        # One qualifier: the container or the end of a nested one.
        ("AppointmentService", "backend/s.py", ["AppointmentService"], True),
        ("Outer.AppointmentService", "backend/s.py", ["AppointmentService"], True),
        ("MyAppointmentService", "backend/s.py", ["AppointmentService"], False),
        # More: the whole container chain, then the module path.
        ("Outer.Inner", "backend/s.py", ["Outer", "Inner"], True),
        ("Outer.Inner", "backend/s.py", ["Wrong", "Inner"], False),
        ("AppointmentService", "backend/s.py", ["SomeClass", "AppointmentService"], False),
        ("AppointmentService", "backend/pkg/mod.py", ["pkg", "mod", "AppointmentService"], True),
        ("AppointmentService", "backend/pkg/mod.py", ["other", "mod", "AppointmentService"], False),
        ("Outer.Inner", "backend/pkg/mod.py", ["mod", "Outer", "Inner"], True),
        ("Outer.Inner", "backend/pkg/mod.py", ["mod", "Inner"], False),
        # A module-level row: the qualifiers are the module path alone, which
        # needs no binding.
        (None, "backend/pkg/mod.py", ["mod"], True),
        (None, "backend/pkg/mod.py", ["pkg", "mod"], True),
        (None, "backend/pkg/mod.py", ["AppointmentService"], False),
        (None, "backend/pkg/__init__.py", ["pkg"], True),
        (None, "backend/pkg/__init__.py", ["__init__"], False),
        (None, "frontend/lib/api/index.ts", ["api"], True),
        ("ApiClient", "frontend/lib/api/index.ts", ["api", "ApiClient"], True),
        (None, "backend/s.py", ["x", "y", "backend", "s"], False),
        # A package containing the module, only for a name its `__init__.py` binds.
        ("Appointment", _MODELS, ["models", "Appointment"], True),
        ("Appointment", _MODELS, ["service_backend", "models", "Appointment"], True),
        ("Appointment", _MODELS, ["service_backend", "Appointment"], False),
        (None, "backend/pkg/mod.py", ["pkg"], False),
        (None, "backend/pkg/mod.py", ["backend", "pkg"], False),
        (None, _MODELS, ["services"], False),
        (None, _MODELS, ["service_backend", "appointment"], False),
        (None, _MODELS, ["models", "service_backend"], False),
    ],
)
def test_qualifiers_match(container: str | None, file: str, qualifiers: list[str], expected: bool) -> None:
    scope = "module" if container is None else "class"
    name = "status" if container else "Appointment"
    assert qualifiers_match(container, file, qualifiers, name=name, scope=scope, bindings=_BINDINGS) is expected


def test_a_package_qualifies_only_the_names_its_init_binds() -> None:
    def fits(qualifiers: list[str], name: str, bindings: dict[str, frozenset[str]]) -> bool:
        return qualifiers_match(None, "backend/pkg/mod.py", qualifiers, name=name, scope="module", bindings=bindings)

    assert fits(["pkg"], "helper", _BINDINGS)
    assert not fits(["pkg"], "other", _BINDINGS)
    assert not fits(["pkg"], "helper", {}), "no __init__.py: a namespace package binds nothing"
    assert fits(["mod"], "other", {}), "the module itself needs no binding"


@pytest.mark.parametrize(
    ("container", "file", "scope", "qualifiers", "expected"),
    [
        # A local of a function body is not reachable through its module.
        (None, "backend/pkg/mod.py", "local", ["mod"], False),
        (None, "frontend/components/article/Panel.tsx", "local", ["Panel"], False),
        # A TypeScript directory is no package: only the module itself, or a
        # folder through its index file.
        (None, "frontend/components/article/Panel.tsx", "module", ["Panel"], True),
        (None, "frontend/components/article/Panel.tsx", "module", ["article", "Panel"], True),
        (None, "frontend/components/article/Panel.tsx", "module", ["article"], False),
        ("Holder", "frontend/components/article/Panel.tsx", "class", ["article", "Holder"], False),
        # A container-only match does not depend on the scope.
        ("Holder", "backend/pkg/mod.py", "class", ["Holder"], True),
    ],
)
def test_qualifiers_match_needs_a_module_level_row_and_python_packages(
    container: str | None, file: str, scope: str, qualifiers: list[str], expected: bool
) -> None:
    bindings = {"frontend/components/article": frozenset({"view", "Holder"})}
    assert qualifiers_match(container, file, qualifiers, name="view", scope=scope, bindings=bindings) is expected


# Each package's `mod.py` defines `<Name>Thing`; its `__init__.py` (or none)
# decides whether `<package>.<Name>Thing` reaches it.
_PACKAGE_INITS: dict[str, str | None] = {
    "empty": "",
    "allonly": "__all__: list[str] = []\n",
    "explicit": "from .mod import ExplicitThing\n",
    "aliased": "from .mod import AliasedThing as RenamedThing\n",
    "typed": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from .mod import TypedThing\n",
    "tried": "try:\n    from .mod import TriedThing\nexcept ImportError:\n    pass\n",
    "nspace": None,
}
_PACKAGE_EXPECTED = {
    "empty": "missing",
    "allonly": "missing",
    "explicit": "ok",
    "aliased": "missing",
    "typed": "ok",
    "tried": "ok",
    "nspace": "missing",
}


def _package_report(tmp_path: Path) -> doc_refs.DocReport:
    files: dict[str, str] = {}
    symbols: list[dict[str, Any]] = []
    mentions: list[str] = []
    for package, init in _PACKAGE_INITS.items():
        thing = f"{package.capitalize()}Thing"
        files[f"backend/{package}/mod.py"] = f"class {thing}:\n    pass\n"
        if init is not None:
            files[f"backend/{package}/__init__.py"] = init
        symbols.append(
            {"name": thing, "kind": "class", "container": None, "file": f"backend/{package}/mod.py", "line": 1}
        )
        mentions += [f"`{package}.{thing}`", f"`mod.{thing}`"]
    return build(tmp_path, {**files, "docs/a.md": "\n\n".join(mentions) + "\n"}, symbols=symbols)


def test_a_package_qualifies_a_name_only_when_its_init_binds_it(tmp_path: Path) -> None:
    by_text = {r.text: r.status for r in refs(_package_report(tmp_path), kind="symbol")}
    for package, expected in _PACKAGE_EXPECTED.items():
        thing = f"{package.capitalize()}Thing"
        assert (package, by_text[f"{package}.{thing}"]) == (package, expected)
        # The module itself names it whatever its package binds.
        assert (package, by_text[f"mod.{thing}"]) == (package, "ok")


def test_the_lookup_qualifies_through_packages_as_the_check_does(tmp_path: Path) -> None:
    report = _package_report(tmp_path)
    assert report.package_bindings["backend/aliased"] == frozenset({"RenamedThing"})
    assert "backend/nspace" not in report.package_bindings
    by_text = {r.text: r for r in refs(report, kind="symbol")}
    for package in _PACKAGE_INITS:
        query = f"{package}.{package.capitalize()}Thing"
        found = lookup(report, symbol=query)
        defined = [f"{d['file']}:{d['line']}" for d in found.definitions.items]
        assert defined == list(by_text[query].defined_in), query


def test_dotted_tier_needs_a_python_class_head_whatever_typescript_says(tmp_path: Path) -> None:
    # A TypeScript class head is indexed only where Node runs; the tier must
    # not change with it, or the verdict would differ between machines.
    ts_class = {
        "name": "ApiClient",
        "kind": "class",
        "container": None,
        "file": "frontend/lib/api.ts",
        "line": 1,
        "language": "typescript",
    }
    doc = "`ApiClient.removedMethod` `AppointmentService.gone_attr`\n"
    with_ts = build(tmp_path / "a", {"docs/a.md": doc}, symbols=[*SYMBOLS, ts_class])
    without_ts = build(tmp_path / "b", {"docs/a.md": doc}, symbols=SYMBOLS, frontend_available=False)
    expected = [("ApiClient.removedMethod", "unresolved", False), ("AppointmentService.gone_attr", "missing", True)]
    assert [(r.text, r.status, r.checked) for r in refs(with_ts, kind="symbol")] == expected
    assert [(r.text, r.status, r.checked) for r in refs(without_ts, kind="symbol")] == expected


def test_typescript_unavailable_is_a_limitation_and_changes_no_status(tmp_path: Path) -> None:
    doc = "`<ArticleCard />` `<GoneCard />` `AppointmentService`\n"
    with_ts = build(tmp_path / "a", {"docs/a.md": doc})
    without_ts = build(tmp_path / "b", {"docs/a.md": doc}, symbols=SYMBOLS, frontend_available=False)
    assert [(r.text, r.status) for r in refs(with_ts)] == [(r.text, r.status) for r in refs(without_ts)]
    assert without_ts.complete
    assert [p.kind for p in without_ts.limitations] == ["frontend_definitions_unavailable"]
    assert without_ts.diagnostics == []
    assert with_ts.limitations == []


def test_camel_case_humps() -> None:
    assert is_camel_case("AppointmentStatus")
    assert is_camel_case("HTTPException")
    assert not is_camel_case("Appointment")
    assert not is_camel_case("JSON")
    assert not is_camel_case("UPPER_SNAKE")


# ── Scope rules ──────────────────────────────────────────────────────────

MIXED_DOC = "[x](gone.md)\n\n`backend/gone.py` `GET /gone` `_gone()`\n"


@pytest.mark.parametrize(
    ("doc", "kinds"),
    [
        ("docs/a.md", ["link", "path", "route", "symbol"]),
        ("docs/plans/p.md", ["link"]),
        ("docs/vendor/x.md", ["link"]),
        (".claude/skills/generic-a/r.md", ["link", "path"]),
        (".claude/skills/generic-b/SKILL.md", ["link", "path", "route"]),
        ("AGENTS.md", ["link", "path", "route", "symbol"]),
    ],
)
def test_scope_rules_select_checks(tmp_path: Path, doc: str, kinds: list[str]) -> None:
    report = build(tmp_path, {doc: MIXED_DOC})
    assert sorted({r.kind for r in report.docs[doc].references}) == kinds
    assert all(r.broken for r in report.docs[doc].references)


def test_first_matching_rule_wins(tmp_path: Path) -> None:
    cfg = doc_config(
        rules=[
            {"paths": ["docs/**"], "checks": ["links"], "reason": "first"},
            {"paths": ["docs/a.md"], "checks": ["links", "paths"], "reason": "second"},
        ]
    )
    report = build(tmp_path, {"docs/a.md": MIXED_DOC}, cfg=cfg)
    entry = report.docs["docs/a.md"]
    assert (entry.checks, entry.rule_reason) == (("links",), "first")


def test_files_outside_scope_are_not_docs(tmp_path: Path) -> None:
    report = build(tmp_path, {"notes/x.md": MIXED_DOC})
    assert "notes/x.md" not in report.docs
    assert "docs/README.md" in report.docs


def test_glob_semantics() -> None:
    assert glob_match("docs/a.md", "docs/**/*.md")
    assert glob_match("docs/x/y/a.md", "docs/**/*.md")
    assert glob_match("tools/code-intel/README.md", "tools/*/README.md")
    assert not glob_match("tools/a/b/README.md", "tools/*/README.md")
    assert glob_match("frontend/app/[id]/page.tsx", "frontend/app/[id]/*.tsx")
    assert not glob_match("frontend/app/i/page.tsx", "frontend/app/[id]/*.tsx")


# ── Config ───────────────────────────────────────────────────────────────


def disk_config(root: Path, **overrides: Any) -> doc_refs.DocRefsConfig:
    """The settings of a config file written to ``root``, so its entries have lines."""
    write_config(root, config_text(**overrides))
    return DocRefsConfig.from_config(load_config(root), root)


def write_config(root: Path, text: str) -> None:
    (root / CONFIG_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / CONFIG_REL).write_text(text, encoding="utf-8")


def test_scope_and_checks_come_from_the_docs_table() -> None:
    cfg = doc_config()
    assert cfg.checks_for("docs/plans/x.md") == ("links",)
    assert cfg.checks_for("docs/vendor/a/b.md") == ("links",)
    assert cfg.checks_for(".claude/skills/generic-a/r.md") == ("links", "paths")
    assert cfg.checks_for("docs/appointment-system.md") == ("links", "paths", "routes", "symbols")
    assert cfg.in_scope("tools/t/README.md")
    assert not cfg.in_scope("notes/x.md")
    assert cfg.in_orphan_scope("docs/protos/GAPS.md")
    assert not cfg.in_orphan_scope("docs/plans/x.md")
    assert not cfg.in_orphan_scope("README.md")


def test_path_roots_follow_the_backend_and_frontend_layout() -> None:
    assert [(r.prefix, r.name) for r in doc_config().path_roots] == [
        ("backend/", "backend-rel"),
        ("frontend/", "frontend-rel"),
        ("backend/service_backend/", "pkg-rel"),
    ]


@pytest.mark.parametrize(
    ("layout", "roots"),
    [
        (
            '[backend]\nroot = "."\npackage = "pkg"\napp = "pkg.main:app"\n'
            '[frontend]\nroot = "web"\ntsconfig = "web/tsconfig.json"\n',
            [("web/", "frontend-rel"), ("pkg/", "pkg-rel")],
        ),
        (
            '[backend]\nroot = "srv"\npackage = "a.b"\napp = "a.b.main:app"\n'
            '[frontend]\nroot = "."\ntsconfig = "tsconfig.json"\n',
            [("srv/", "backend-rel"), ("srv/a/b/", "pkg-rel")],
        ),
    ],
)
def test_path_roots_leave_out_the_repository_root(layout: str, roots: list[tuple[str, str]]) -> None:
    config = parse_config(layout + '[docs]\nscope = ["docs/**/*.md"]\n')
    cfg = DocRefsConfig.from_config(config, Path("."))
    assert [(r.prefix, r.name) for r in cfg.path_roots] == roots


def test_the_docs_table_is_required() -> None:
    config = parse_config(LAYOUT, CONFIG_REL, FAKE_ROOT / CONFIG_REL)
    with pytest.raises(ConfigError) as caught:
        DocRefsConfig.from_config(config, FAKE_ROOT)
    assert str(caught.value) == f"{CONFIG_REL}: missing required table [docs]"


def test_the_config_file_is_named_relative_to_the_repository(tmp_path: Path) -> None:
    assert doc_config().path == CONFIG_REL
    assert disk_config(tmp_path).path == CONFIG_REL
    outside = tmp_path / "elsewhere.toml"
    outside.write_text(config_text(), encoding="utf-8")
    cfg = DocRefsConfig.from_config(load_config(tmp_path / "repo", outside), tmp_path / "repo")
    assert cfg.path == outside.resolve().as_posix()


def test_ignore_entries_suppress_failures_and_unused_ones_are_findings(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    cfg = disk_config(
        root,
        ignore=[
            {"file": "docs/a.md", "kind": "route", "text": "GET /gone", "reason": "illustrative"},
            {"file": "docs/a.md", "kind": "path", "text": "backend/app.py", "reason": "passes anyway"},
            {"file": "docs/a.md", "kind": "symbol", "text": "_never()", "reason": "no such mention"},
        ],
    )
    report = build(tmp_path, {"docs/a.md": "`GET /gone` `backend/app.py`\n"}, cfg=cfg)
    route = one(report, "route")
    assert (route.status, route.detail, route.checked) == ("ignored", "ignored (missing): illustrative", False)
    headers = [n for n, line in enumerate((root / CONFIG_REL).read_text().splitlines(), 1) if line == "[[docs.ignore]]"]
    assert [(f.file, f.line, f.kind, f.status, f.text) for f in ref_findings(report)] == [
        (CONFIG_REL, headers[1], "ignore", "unused", "backend/app.py"),
        (CONFIG_REL, headers[2], "ignore", "unused", "_never()"),
    ]
    assert ref_findings(report)[0].message == (
        "unused ignore entry: path 'backend/app.py' in docs/a.md (no failing reference matches it)"
    )


def test_an_unused_ignore_entry_is_at_line_1_when_the_config_text_is_unavailable(tmp_path: Path) -> None:
    cfg = doc_config(ignore=[{"file": "docs/a.md", "kind": "route", "text": "GET /gone", "reason": "r"}])
    report = build(tmp_path, {"docs/a.md": "nothing\n"}, cfg=cfg)
    assert [(f.file, f.line) for f in ref_findings(report)] == [(CONFIG_REL, 1)]


def test_ignore_entry_on_a_not_checked_reference_is_used(tmp_path: Path) -> None:
    # Locally the submodule is missing, in CI the link may fail: the entry is
    # needed there, so it is not reported unused here.
    text = "../backend/packages/core_security/GONE.md"
    cfg = doc_config(ignore=[{"file": "docs/a.md", "kind": "link", "text": text, "reason": "CI only"}])
    report = build(
        tmp_path,
        {"docs/a.md": f"[x]({text})\n"},
        cfg=cfg,
        uninitialized=["backend/packages/core_security"],
    )
    assert one(report, "link").status == "not_checked"
    assert ref_findings(report) == []


@pytest.mark.parametrize("written", ["gone%20file.md", "gone file.md"])
def test_an_ignore_entry_names_a_link_as_written_or_decoded(tmp_path: Path, written: str) -> None:
    cfg = doc_config(ignore=[{"file": "docs/a.md", "kind": "link", "text": written, "reason": "old"}])
    report = build(tmp_path, {"docs/a.md": "[x](gone%20file.md)\n"}, cfg=cfg)
    link = one(report, "link")
    assert (link.text, link.status, link.detail) == ("gone file.md", "ignored", "ignored (missing): old")
    assert ref_findings(report) == []


# ── Route prefixes ───────────────────────────────────────────────────────

PREFIXED_ROUTES: list[dict[str, Any]] = [
    {"kind": "http", "file": "backend/app.py", "handler": h, "line": n, "method": m, "path": path}
    for n, (m, path, h) in enumerate(
        [
            ("GET", "/api/accounts", "list_accounts"),
            ("GET", "/api/accounts/{account_id}", "get_account"),
            ("POST", "/api/accounts", "create_account"),
            ("GET", "/legacy/status", "status"),
        ],
        start=10,
    )
]


@pytest.mark.parametrize(
    ("mention", "status", "target"),
    [
        ("`GET /accounts`", "ok", "GET /api/accounts"),
        ("`GET /accounts/42`", "ok", "GET /api/accounts/{account_id}"),
        ("`/accounts`", "ok", "GET /api/accounts"),
        ("`GET /api/accounts`", "ok", "GET /api/accounts"),
        ("`DELETE /accounts`", "method_differs", "GET /api/accounts"),
        ("`GET /accounts/*`", "prefix", "GET /api/accounts"),
        ("`GET .../accounts`", "suffix", "GET /api/accounts"),
        ("`GET /api/legacy/status`", "ok", "GET /legacy/status"),
        ("`GET /legacy/status`", "ok", "GET /legacy/status"),
        ("`GET /nothing`", "missing", None),
        ("`GET /api/nothing`", "missing", None),
    ],
)
def test_a_mention_may_add_or_omit_a_configured_route_prefix(
    tmp_path: Path, mention: str, status: str, target: str | None
) -> None:
    report = build(tmp_path, {"docs/a.md": f"{mention}\n"}, routes=PREFIXED_ROUTES)
    route = one(report, "route")
    assert (route.status, route.target) == (status, target)


def test_without_a_configured_prefix_only_the_written_path_is_an_exact_match(tmp_path: Path) -> None:
    docs = {"docs/a.md": "`GET /accounts` `GET /api/accounts` `GET /api/legacy/status`\n"}
    prefixed = build(tmp_path / "prefixed", docs, routes=PREFIXED_ROUTES)
    bare = build(tmp_path / "bare", docs, routes=PREFIXED_ROUTES, cfg=doc_config(route_prefixes=[]))
    assert [(r.text, r.status) for r in refs(prefixed, kind="route")] == [
        ("GET /accounts", "ok"),
        ("GET /api/accounts", "ok"),
        ("GET /api/legacy/status", "ok"),
    ]
    # `/accounts` still ends a live route, which the suffix rule accepts.
    assert [(r.text, r.status) for r in refs(bare, kind="route")] == [
        ("GET /accounts", "suffix"),
        ("GET /api/accounts", "ok"),
        ("GET /api/legacy/status", "missing"),
    ]


def test_the_prefixes_are_the_configured_ones(tmp_path: Path) -> None:
    cfg = doc_config(route_prefixes=["/v1"])
    routes = [{**PREFIXED_ROUTES[0], "path": "/v1/accounts"}, {**PREFIXED_ROUTES[0], "path": "/health"}]
    docs = {"docs/a.md": "`GET /accounts` `GET /v1/health` `GET /api/accounts`\n"}
    report = build(tmp_path, docs, routes=routes, cfg=cfg)
    assert [(r.text, r.status, r.target) for r in refs(report, kind="route")] == [
        ("GET /accounts", "ok", "GET /v1/accounts"),
        ("GET /api/accounts", "missing", None),
        ("GET /v1/health", "ok", "GET /health"),
    ]


def test_a_bare_path_passes_the_gate_through_a_prefixed_route(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`/accounts` `/orders`\n"}, routes=PREFIXED_ROUTES)
    assert [(r.text, r.status) for r in refs(report, kind="route")] == [
        ("/accounts", "ok"),
        ("/orders", "skipped"),
    ]


def test_a_page_named_as_written_beats_a_route_that_needs_the_prefix(tmp_path: Path) -> None:
    files = {"docs/a.md": "`/accounts` `GET /accounts`\n", "frontend/app/accounts/page.tsx": "export default 1;\n"}
    report = build(tmp_path, files, routes=PREFIXED_ROUTES)
    assert [(r.text, r.status, r.target) for r in refs(report, kind="route")] == [
        ("/accounts", "frontend", "/accounts"),
        ("GET /accounts", "ok", "GET /api/accounts"),
    ]


# ── Frontend pages ───────────────────────────────────────────────────────


def test_next_app_pages_are_found_under_the_configured_directory() -> None:
    tracked = frozenset(
        {
            "web/app/(group)/settings/page.tsx",
            "web/app/items/[id]/route.ts",
            "web/app/_private/page.tsx",
            "web/app/items/layout.tsx",
            "web/public/logo.svg",
            "frontend/app/other/page.tsx",
        }
    )
    found = NextAppPages("web/app", "web/public").pages(tracked)
    assert found.available
    assert [(p.display, p.segments, p.file) for p in found.pages] == [
        ("/settings", ("settings",), "web/app/(group)/settings/page.tsx"),
        ("/items/[id]", ("items", PARAM), "web/app/items/[id]/route.ts"),
        ("/logo.svg", ("logo.svg",), "web/public/logo.svg"),
    ]


def test_a_provider_without_a_public_dir_serves_no_public_files() -> None:
    tracked = frozenset({"public/logo.svg", "app/page.tsx"})
    assert [p.file for p in NextAppPages("app").pages(tracked).pages] == ["app/page.tsx"]
    assert NoPages().pages(tracked).pages == ()
    assert [p.display for p in NoPages("public").pages(tracked).pages] == ["/logo.svg"]


def test_a_directory_is_matched_whole_and_however_it_is_spelled() -> None:
    tracked = frozenset({"app/x/page.tsx", "application/y/page.tsx", "public/a.txt", "public-old/b.txt"})
    found = NextAppPages("./app", "public/").pages(tracked)
    assert [p.file for p in found.pages] == ["app/x/page.tsx", "public/a.txt"]


class UnavailablePages:
    def pages(self, tracked: frozenset[str]) -> PageSet:
        problem = limitation(
            "frontend_pages_unavailable", stage=STAGE, file="frontend", line=None, message="the pages cannot be read"
        )
        return PageSet(available=False, problems=(problem,))


def test_unreadable_pages_make_a_method_less_mention_not_checked(tmp_path: Path) -> None:
    docs = {"docs/a.md": "`/articles/zzz/overview` `GET /articles/zzz/overview` `GET .../zzz/overview`\n"}
    report = build(tmp_path, docs, pages=UnavailablePages())
    assert [(r.text, r.status) for r in refs(report, kind="route")] == [
        ("/articles/zzz/overview", "not_checked"),
        ("GET .../zzz/overview", "missing"),
        ("GET /articles/zzz/overview", "missing"),
    ]
    assert [p.kind for p in report.limitations] == ["frontend_pages_unavailable"]
    assert report.complete


def test_available_pages_keep_a_missing_mention_missing(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`/articles/zzz/overview`\n"}, pages=NoPages())
    assert one(report, "route").status == "missing"


# ── Path bases ───────────────────────────────────────────────────────────

SERVER_LAYOUT = """\
[backend]
root = "server"
package = "svc"
app = "svc.main:app"

[frontend]
root = "web"
tsconfig = "web/tsconfig.json"

"""


def test_paths_resolve_against_the_configured_roots_and_package(tmp_path: Path) -> None:
    files = {
        "docs/a.md": "`server/svc/api.py` `svc/api.py` `api/routes.py` `web/ui.ts` `lib/util.ts` `backend/app.py`\n",
        "server/svc/api/routes.py": "x = 1\n",
        "server/svc/api.py": "x = 1\n",
        "web/lib/util.ts": "export {};\n",
        "web/ui.ts": "export {};\n",
    }
    cfg = doc_config(layout=SERVER_LAYOUT)
    report = build(tmp_path, files, cfg=cfg)
    assert [(r.text, r.status, r.target) for r in refs(report, kind="path")] == [
        ("api/routes.py", "ok", "server/svc/api/routes.py"),
        ("backend/app.py", "ok", "backend/app.py"),
        ("lib/util.ts", "ok", "web/lib/util.ts"),
        ("server/svc/api.py", "ok", "server/svc/api.py"),
        ("svc/api.py", "ok", "server/svc/api.py"),
        ("web/ui.ts", "ok", "web/ui.ts"),
    ]


# ── Optional reachability ────────────────────────────────────────────────


def test_a_config_without_reachability_has_no_orphan_check(tmp_path: Path) -> None:
    cfg = doc_config(reachability=None)
    assert cfg.reachability is None
    docs = {"docs/README.md": "[b](b.md)\n", "docs/lonely.md": "# Lonely\n", "docs/a.md": "`GET /gone`\n"}
    report = build(tmp_path, docs, cfg=cfg)
    assert report.orphans == ()
    assert report.reachable == frozenset()
    assert report.inbound["docs/b.md"] == (("docs/README.md", 1),)
    assert [(f.file, f.kind) for f in report.findings()] == [("docs/a.md", "route")]
    assert report.complete


# ── Orphans and inbound links ────────────────────────────────────────────


def test_orphans_and_reachability(tmp_path: Path) -> None:
    docs = {
        "docs/README.md": "[a](a.md) [guide](guide/) [protos](protos) [empty](emptydir/) [echo](../echo/README.md)\n",
        "docs/a.md": "[deep](deep.md#top)\n",
        "docs/deep.md": "# Top\n",
        "docs/guide/README.md": "# Guide\n",
        "docs/protos/INDEX.md": "# Index\n",
        "docs/protos/GAPS.md": "# Gaps\n",
        "docs/emptydir/notes.md": "# Notes\n",
        "docs/orphan.md": "# Orphan\n",
        "docs/plans/old.md": "[lonely](../lonely.md)\n",
        "docs/lonely.md": "# Lonely\n",
        "echo/README.md": "[far](../docs/far.md)\n",
        "docs/far.md": "# Far\n",
    }
    report = build(tmp_path, docs)
    assert report.orphans == (
        "docs/b.md",
        "docs/emptydir/notes.md",
        "docs/lonely.md",
        "docs/orphan.md",
        "docs/protos/GAPS.md",
        "docs/sub/x.md",
    )
    assert {
        "docs/README.md",
        "docs/a.md",
        "docs/deep.md",
        "docs/guide/README.md",
        "docs/protos/INDEX.md",
        "echo/README.md",
        "docs/far.md",
    } <= report.reachable
    assert "docs/plans/old.md" not in report.orphans
    assert report.inbound["docs/a.md"] == (("docs/README.md", 1),)
    assert report.inbound["docs/guide/README.md"] == (("docs/README.md", 1),)
    assert report.inbound["docs/lonely.md"] == (("docs/plans/old.md", 1),)
    orphan = [f for f in report.findings() if f.file == "docs/orphan.md"]
    assert orphan == [
        ("docs/orphan.md", 1, "orphan", "orphaned", "docs/orphan.md", "orphaned doc: not reachable from docs/README.md")
    ]


def test_missing_reachability_root_orphans_every_doc(tmp_path: Path) -> None:
    cfg = doc_config(reachability={"root": "docs/GONE.md", "include": ["docs/**/*.md"], "exclude": []})
    report = build(tmp_path, {"docs/README.md": "[b](b.md)\n"}, cfg=cfg)
    assert report.reachable == frozenset()
    assert report.orphans == ("docs/README.md", "docs/b.md", "docs/sub/x.md")


def test_directory_link_prefers_readme_over_index(tmp_path: Path) -> None:
    docs = {"docs/README.md": "[g](g/)\n", "docs/g/README.md": "# R\n", "docs/g/index.md": "# I\n"}
    report = build(tmp_path, docs)
    assert "docs/g/README.md" in report.reachable
    assert "docs/g/index.md" in report.orphans


def test_directory_link_fragment_is_checked_against_its_landing_page(tmp_path: Path) -> None:
    docs = {
        "docs/a.md": "[ok](g/#real) [bare](g#real) [bad](g/#nope) [index](i/#nope)\n",
        "docs/g/README.md": "# G\n\n## Real\n",
        "docs/g/index.md": "# Nope\n",
        "docs/i/index.md": "# I\n",
    }
    report = build(tmp_path, docs)
    assert [(r.text, r.status, r.detail) for r in refs(report, kind="link")] == [
        ("g#real", "ok", None),
        ("g/#nope", "anchor_missing", "no heading or id in docs/g/README.md"),
        ("g/#real", "ok", None),
        ("i/#nope", "anchor_missing", "no heading or id in docs/i/index.md"),
    ]


# ── Findings and line numbers ────────────────────────────────────────────


def test_findings_are_sorted_with_exact_lines(tmp_path: Path) -> None:
    doc = """
    # Title

    See `backend/gone.py`.

    [broken](b.md#nope)

    | Method | Path |
    |---|---|
    | GET | `/appointments/{id}/gone` |
    """
    report = build(tmp_path, {"docs/a.md": doc, "docs/README.md": "[a](a.md) [b](b.md) [x](sub/x.md)\n"})
    assert [(f.file, f.line, f.kind, f.status) for f in report.findings()] == [
        ("docs/a.md", 3, "path", "missing"),
        ("docs/a.md", 5, "link", "anchor_missing"),
        ("docs/a.md", 9, "route", "missing"),
    ]
    assert report.findings()[1].message == "broken anchor: b.md#nope (no heading or id in docs/b.md)"


def test_reference_counts_and_complete(tmp_path: Path) -> None:
    report = build(tmp_path, {"docs/a.md": "`backend/app.py` `backend/gone.py` [x](b.md)\n"})
    counts = report.reference_counts()
    assert counts["path"] == {"ok": 1, "missing": 1}
    assert counts["link"] == {"ok": 1}
    assert report.complete


def test_undecodable_doc_is_a_diagnostic(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", {**BASE_FILES, "docs/bad.md": ""})
    (repo.repo_root / "docs/bad.md").write_bytes(b"\xff\xfe bad")
    report = DocIndex(repo.repo_root).report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    assert not report.complete
    assert [(p.kind, p.file) for p in report.diagnostics] == [("decode_failed", "docs/bad.md")]
    assert report.docs["docs/bad.md"].references == ()


def test_a_code_file_that_does_not_parse_is_one_diagnostic_on_every_report(tmp_path: Path) -> None:
    # The words are prefetched on threads; their problems still reach each report once.
    files = {**BASE_FILES, "backend/broken.py": "def (\n", "docs/a.md": "`_gone()`\n"}
    repo = make_repo(tmp_path / "repo", files)
    index = DocIndex(repo.repo_root, clock=lambda: 2**62)
    for _ in range(2):
        report = index.report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
        assert [(p.kind, p.file) for p in report.diagnostics] == [("parse_failed", "backend/broken.py")]


# ── Caches ───────────────────────────────────────────────────────────────


def _counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    calls: list[str] = []
    real = getattr(doc_refs, name)

    def wrapper(path: Path, rel: str, **kw: Any):
        calls.append(rel)
        return real(path, rel, **kw)

    monkeypatch.setattr(doc_refs, name, wrapper)
    return calls


def test_doc_facts_and_words_are_cached_until_a_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc_calls = _counting(monkeypatch, "read_doc_facts")
    word_calls: list[str] = []
    real_words = doc_refs.code_words_from_bytes

    def words(rel: str, data: bytes, **kw: Any):
        word_calls.append(rel)
        return real_words(rel, data, **kw)

    monkeypatch.setattr(doc_refs, "code_words_from_bytes", words)
    files = {**BASE_FILES, "docs/a.md": "`_gone()`\n"}
    repo = make_repo(tmp_path / "repo", files)
    # A clock far past every stamp: no entry is racy.
    index = DocIndex(repo.repo_root, clock=lambda: 2**62)
    index.report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    first_docs, first_words = len(doc_calls), len(word_calls)
    assert "docs/a.md" in doc_calls and "backend/app.py" in word_calls
    index.report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    assert (len(doc_calls), len(word_calls)) == (first_docs, first_words)
    (repo.repo_root / "docs/a.md").write_text("`_used_private()` and more\n", encoding="utf-8")
    (repo.repo_root / "backend/util.py").write_text("y = 2  # longer\n", encoding="utf-8")
    report = index.report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    assert doc_calls[first_docs:] == ["docs/a.md"]
    assert word_calls[first_words:] == ["backend/util.py"]
    assert one(report, "symbol").status == "ok"


def test_package_bindings_are_cached_until_the_init_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = doc_refs.package_bindings_from_bytes

    def bindings(rel: str, data: bytes, **kw: Any):
        calls.append(rel)
        return real(rel, data, **kw)

    monkeypatch.setattr(doc_refs, "package_bindings_from_bytes", bindings)
    rows = [{"name": "PkgThing", "kind": "class", "container": None, "file": "backend/pkg/mod.py", "line": 1}]
    files = {
        **BASE_FILES,
        "backend/pkg/__init__.py": "",
        "backend/pkg/mod.py": "class PkgThing:\n    pass\n",
        "docs/a.md": "`pkg.PkgThing`\n",
    }
    repo = make_repo(tmp_path / "repo", files)
    index = DocIndex(repo.repo_root, clock=lambda: 2**62)
    assert one(index.report(repo, ROUTES, [], [], rows, True, doc_config()), "symbol").status == "missing"
    assert calls == ["backend/pkg/__init__.py"]
    index.report(repo, ROUTES, [], [], rows, True, doc_config())
    assert calls == ["backend/pkg/__init__.py"], "an unchanged __init__.py is not read again"
    (repo.repo_root / "backend/pkg/__init__.py").write_text("from .mod import PkgThing\n", encoding="utf-8")
    assert one(index.report(repo, ROUTES, [], [], rows, True, doc_config()), "symbol").status == "ok"
    assert calls == ["backend/pkg/__init__.py"] * 2


def test_read_failures_are_never_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {**BASE_FILES, "docs/a.md": "# A\n"}
    repo = make_repo(tmp_path / "repo", files)
    real = code_paths.read_file_bytes
    failing = {"docs/a.md"}

    def read(path: Path) -> bytes:
        if path.relative_to(repo.repo_root).as_posix() in failing:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(code_paths, "read_file_bytes", read)
    index = DocIndex(repo.repo_root, clock=lambda: 2**62)
    report = index.report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    assert [(p.kind, p.file) for p in report.diagnostics] == [("read_failed", "docs/a.md")]
    failing.clear()
    report = index.report(repo, ROUTES, [], [], SYMBOLS, True, doc_config())
    assert report.complete


def test_racy_entries_are_checked_by_content(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text("# X\n", encoding="utf-8")
    stamp = FileStamp.from_stat(os.stat(path))
    now = [stamp.changed_ns]
    index = DocIndex(tmp_path, clock=lambda: now[0])
    digest = code_paths.content_digest(path)
    entry = doc_refs._Entry(stamp, True, digest, "value", ())
    assert index._still_valid(entry, path, stamp)
    assert entry.racy
    stale = doc_refs._Entry(stamp, True, "0" * 64, "value", ())
    assert not index._still_valid(stale, path, stamp)
    now[0] = stamp.changed_ns + code_paths.RACY_WINDOW_NS + 1
    assert index._still_valid(entry, path, stamp)
    assert (entry.racy, entry.digest) == (False, None)


def test_symbol_rows_accept_index_pairs() -> None:
    @dataclass(frozen=True)
    class Sym:
        name: str
        kind: str
        line: int
        container: str | None
        scope: str = "module"

    rows = symbol_rows([("backend/a.py", Sym("A", "class", 3, None)), ("frontend/b.ts", Sym("b", "const", 1, None))])
    assert [(r.name, r.file, r.line, r.language) for r in rows] == [
        ("A", "backend/a.py", 3, "python"),
        ("b", "frontend/b.ts", 1, "typescript"),
    ]
