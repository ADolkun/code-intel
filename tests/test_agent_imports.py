"""``@path`` imports in CLAUDE.md, AGENTS.md and Copilot's instructions file."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fake_repo_files import FakeRepoFiles, repo_files_on_disk
from fixture_repo import write

from stack_intel import agent_files
from stack_intel.agent_imports import import_path
from stack_intel.config import AgentsConfig


def lines(root: Path, files: FakeRepoFiles | None = None) -> list[str]:
    report = agent_files.check(root, AgentsConfig(), files or repo_files_on_disk(root))
    return [f"{f.file}:{f.line}: {f.message}" for f in report.findings]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path / "docs" / "setup.md", "# Setup\n")
    write(tmp_path / "docs" / "My Notes.md", "# Notes\n")
    write(tmp_path / "scripts" / "run.sh", "echo\n")
    write(tmp_path / "web" / "local.md", "# Web\n")
    return tmp_path


@pytest.mark.parametrize(
    ("token", "path"),
    [
        ("docs/setup.md", "docs/setup.md"),
        ("AGENTS.md", "AGENTS.md"),
        ("docs/README.MD", "docs/README.MD"),
        ("./scripts/run.sh", "./scripts/run.sh"),
        ("../shared/x", "../shared/x"),
        ("docs/My\\ Notes.md", "docs/My Notes.md"),
        ("./docs/", "./docs/"),
        ("types/node", None),
        ("README", None),
        ("docs/setup.md.", None),
        ("docs/setup.md)", None),
        ("./scripts/run.sh,", None),
        ('"docs/setup.md"', None),
        ("'docs/setup.md'", None),
        ("/etc/x.md", None),
        ("~/.claude/x.md", None),
        ("name", None),
    ],
)
def test_what_counts_as_an_import(token: str, path: str | None) -> None:
    assert import_path(token) == path


def test_a_missing_import_is_reported_at_its_line(repo: Path) -> None:
    write(
        repo / "AGENTS.md",
        "# Agents\n\nRead @docs/setup.md first.\n\nThen @docs/gone.md and (@docs/old.md for history)\n",
    )
    assert lines(repo) == [
        "AGENTS.md:5: @docs/gone.md imports a file that does not exist",
        "AGENTS.md:5: @docs/old.md imports a file that does not exist",
    ]


@pytest.mark.parametrize(
    "rel",
    [
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/CLAUDE.md",
        ".github/copilot-instructions.md",
        "web/CLAUDE.md",
        "web/AGENTS.md",
    ],
)
def test_every_import_file_is_read(repo: Path, rel: str) -> None:
    write(repo / rel, "@./missing.md\n")
    assert lines(repo) == [f"{rel}:1: @./missing.md imports a file that does not exist"]


@pytest.mark.parametrize(
    "text",
    [
        "@docs/setup.md\n",
        "@./docs/setup.md\n",
        "See @docs/My\\ Notes.md\n",
        "@local.md\n",
        "@./scripts/run.sh\n",
        "@./docs/\n",
        "`@docs/gone.md`\n",
        "```\n@docs/gone.md\n```\n",
        "    @docs/gone.md\n",
        '@"docs/gone.md"\n',
        "@~/.claude/gone.md\n",
        "@/abs/gone.md\n",
        "@../../outside.md\n",
        "Install @types/node and ask @someone.\n",
        "mail me at me@example.md\n",
        "Read @docs/gone.md, then go.\n",
        "<!-- @docs/gone.md -->\n",
    ],
    ids=[
        "from-the-root",
        "dot-slash",
        "escaped-space",
        "relative-to-the-file",
        "non-markdown",
        "directory",
        "code-span",
        "fence",
        "indented-code",
        "quoted",
        "home",
        "absolute",
        "outside-the-repository",
        "package-and-mention",
        "email",
        "trailing-punctuation",
        "html-comment",
    ],
)
def test_an_import_that_resolves_or_cannot_be_proven_is_not_reported(repo: Path, text: str) -> None:
    write(repo / "web" / "CLAUDE.md", text)
    assert lines(repo) == []


def test_a_target_git_ignores_is_not_reported(repo: Path) -> None:
    write(repo / "CLAUDE.md", "@CLAUDE.local.md\n@notes/private.md\n")
    files = replace(repo_files_on_disk(repo), ignored_paths=frozenset({"CLAUDE.local.md"}))
    assert lines(repo, files) == ["CLAUDE.md:2: @notes/private.md imports a file that does not exist"]


def test_an_ignored_target_present_on_disk_is_not_reported(repo: Path) -> None:
    write(repo / "CLAUDE.md", "@CLAUDE.local.md\n")
    write(repo / "CLAUDE.local.md", "# Mine\n")
    files = repo_files_on_disk(repo)
    hidden = replace(files, files=files.files - {"CLAUDE.local.md"}, tracked=files.tracked - {"CLAUDE.local.md"})
    assert lines(repo, hidden) == []


def test_a_target_in_an_uninitialized_submodule_is_not_reported(repo: Path) -> None:
    write(repo / "AGENTS.md", "@vendor/kit/AGENTS.md\n")
    files = replace(repo_files_on_disk(repo), uninitialized_submodules=frozenset({"vendor/kit"}))
    assert lines(repo, files) == []
