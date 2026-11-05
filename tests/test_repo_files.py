"""Git-visible files (``repo_files``), against real git repositories in tmp dirs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fixture_repo import hermetic_git

from code_intel import repo_files
from code_intel.repo_files import RepoFilesError, list_repo_files


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hermetic_git(monkeypatch, tmp_path / "home")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(
        root,
        {
            ".gitignore": "build/\n*.log\n.env\n",
            "docs/guide.md": "# Guide\n",
            "backend/app/main.py": "x = 1\n",
        },
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    write(root, {"docs/new.md": "new\n", "debug.log": "x\n", "build/out.js": "x\n", ".env": "SECRET=placeholder\n"})
    return root


def test_tracked_and_untracked_not_ignored_files_are_visible(repo: Path) -> None:
    files = list_repo_files(repo)
    assert {"docs/guide.md", "backend/app/main.py", ".gitignore", "docs/new.md"} <= files.files
    assert not {"debug.log", "build/out.js", ".env"} & files.files
    assert files.exists("docs/new.md")
    assert not files.exists("build/out.js")


def test_tracked_set_excludes_untracked_files(repo: Path) -> None:
    files = list_repo_files(repo)
    assert "docs/guide.md" in files.tracked
    assert "docs/new.md" not in files.tracked


def test_parent_directories_are_implied(repo: Path) -> None:
    files = list_repo_files(repo)
    assert {"docs", "backend", "backend/app"} <= files.dirs
    assert files.exists("backend/app")
    assert not files.exists("build")
    assert "" not in files.dirs


def test_paths_keep_their_exact_case(repo: Path) -> None:
    files = list_repo_files(repo)
    assert files.exists("docs/guide.md")
    assert not files.exists("docs/Guide.md")


def test_ignored_answers_for_missing_paths(repo: Path) -> None:
    files = list_repo_files(repo)
    asked = ["gone.log", "frontend/build/", "frontend/build", "docs/missing.md", ".env"]
    # `frontend/build/` matches the directory pattern; without the slash it does
    # not, because git cannot know that a missing path is a directory.
    assert files.ignored(asked) == {"gone.log", "frontend/build/", ".env"}


def test_ignored_sends_paths_on_stdin_with_a_fixed_argv(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = list_repo_files(repo)
    calls: list[tuple[list[str], bytes | None]] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("input")))
        assert "shell" not in kwargs
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_files.subprocess, "run", recording_run)
    hostile = "$(touch pwned) x.log"
    assert files.ignored([hostile, "docs/a.md"]) == {hostile}
    ((argv, stdin),) = calls
    assert argv == ["git", "-c", "core.ignorecase=false", "check-ignore", "-z", "--stdin"]
    assert stdin == b"$(touch pwned) x.log\0docs/a.md\0"
    assert not (repo / "pwned").exists()


def test_ignored_skips_paths_git_would_reject(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = list_repo_files(repo)
    calls: list[bytes | None] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        calls.append(kwargs.get("input"))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_files.subprocess, "run", recording_run)
    # Any of these in the batch would make git exit 128 for all of them.
    assert files.ignored(["a\0b.log", "", "/abs.log", "../out.log", "a/../../x.log"]) == frozenset()
    assert calls == []
    assert files.ignored(["a\0b.log", "c.log"]) == {"c.log"}
    assert calls == [b"c.log\0"]


def symlink_to_outside(repo: Path, name: str, target: Path) -> None:
    """``repo/name`` as a symlink to ``target``, outside the repository, like the
    untracked ``.env`` files a git worktree links to the main checkout."""
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).unlink(missing_ok=True)
    (repo / name).symlink_to(target)


def test_ignored_gives_the_same_answers_for_symlinked_and_regular_files(repo: Path, tmp_path: Path) -> None:
    files = list_repo_files(repo)
    asked = [".env/", ".env", ".env/x", "docs/guide.md", "docs/missing.md", "debug.log/"]
    as_regular_file = files.ignored(asked)
    assert as_regular_file == {".env/", ".env", ".env/x", "debug.log/"}

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "env").write_text("X=1\n", encoding="utf-8")
    symlink_to_outside(repo, ".env", outside / "env")
    assert files.ignored(asked) == as_regular_file


def test_ignored_never_asks_git_beyond_a_symlink(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "env").write_text("X=1\n", encoding="utf-8")
    symlink_to_outside(repo, ".env", outside / "env")
    symlink_to_outside(repo, "frontend/.env", outside / "env")
    symlink_to_outside(repo, "linkdir", outside / "dir")
    symlink_to_outside(repo, "keep.txt", outside / "env")
    files = list_repo_files(repo)
    asked_of_git: list[bytes | None] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        asked_of_git.append(kwargs.get("input"))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_files.subprocess, "run", recording_run)
    asked = [".env/", "frontend/.env/", "frontend/.env/x/y", "linkdir/", "linkdir/x.log", "keep.txt/", "keep.txt"]
    # One batch for all of them (git would have failed it on any `…/` below a
    # symlink), asking about the symlinks themselves. `linkdir` is not ignored,
    # so nothing under it is, `*.log` or not; a symlink is a file, so `linkdir/`
    # and `.env/` get the answer for the link.
    assert files.ignored(asked) == {".env/", "frontend/.env/", "frontend/.env/x/y"}
    assert asked_of_git == [b".env\0frontend/.env\0keep.txt\0linkdir\0"]


def test_dot_components_cannot_hide_a_symlink(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "env").write_text("X=1\n", encoding="utf-8")
    symlink_to_outside(repo, ".env", tmp_path / "env")
    files = list_repo_files(repo)
    asked = ["./.env/", ".env/.", "docs/../.env/", ".env/../debug.log", ".env/../docs/guide.md"]
    assert files.ignored(asked) == {"./.env/", ".env/.", "docs/../.env/", ".env/../debug.log"}


def test_a_symlink_that_is_not_ignored_is_not_reported_ignored(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "target").mkdir()
    symlink_to_outside(repo, "notes", tmp_path / "target")
    files = list_repo_files(repo)
    assert files.ignored(["notes/", "notes", "notes/a.md", "notes/b/c"]) == frozenset()
    assert files.ignored(["notes/", "debug.log"]) == {"debug.log"}


def test_ignore_rules_are_case_sensitive_whatever_core_ignorecase_says(repo: Path) -> None:
    # macOS checkouts set core.ignorecase=true; CI's Linux checkout does not.
    git(repo, "config", "core.ignorecase", "true")
    write(repo, {".gitignore": "qa/\n", "docs/QA/x.md": "x\n", "notes/qa/y.md": "y\n"})
    files = list_repo_files(repo)
    assert "docs/QA/x.md" in files.files
    assert "notes/qa/y.md" not in files.files
    assert files.ignored(["docs/QA/z.md", "other/qa/z.md"]) == {"other/qa/z.md"}


def test_tracked_files_deleted_from_the_work_tree_are_gone(repo: Path) -> None:
    (repo / "docs/guide.md").unlink()
    files = list_repo_files(repo)
    assert "docs/guide.md" not in files.files
    assert "docs/guide.md" not in files.tracked
    assert not files.exists("docs/guide.md")
    # The directory stays while another file keeps it.
    assert files.exists("docs")


def test_uninitialized_submodule_is_reported(repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/sub")
    files = list_repo_files(repo)
    assert files.uninitialized_submodules == {"vendor/sub"}
    assert files.submodules == {"vendor/sub"}
    assert "vendor/sub" not in files.files
    assert files.exists("vendor/sub")
    assert files.uninitialized_submodule_of("vendor/sub/docs/a.md") == "vendor/sub"
    assert files.uninitialized_submodule_of("vendor/subway.md") is None
    (problem,) = files.submodule_limitations()
    assert (problem.kind, problem.category, problem.file) == ("submodule_missing", None, "vendor/sub")
    assert "git submodule update --init --recursive" in problem.message
    # git refuses check-ignore paths inside a submodule, so they are not asked.
    assert files.ignored(["vendor/sub/x.log", "y.log"]) == {"y.log"}


def test_initialized_submodule_files_are_listed(tmp_path: Path, repo: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    git(sub, "init", "-q")
    write(sub, {"README.md": "# Sub\n"})
    git(sub, "add", ".")
    git(sub, "commit", "-qm", "sub")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "ext")
    files = list_repo_files(repo)
    assert "ext/README.md" in files.files
    assert "ext/README.md" in files.tracked
    assert files.submodules == {"ext"}
    assert files.uninitialized_submodules == frozenset()
    assert files.submodule_limitations() == []


def committed_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    git(root, "init", "-q")
    write(root, files)
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def add_submodule(repo: Path, source: Path, path: str) -> None:
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), path)
    git(repo, "commit", "-qm", f"add {path}")


def test_initialized_submodule_deletions_and_untracked_files_count(tmp_path: Path, repo: Path) -> None:
    sub = committed_repo(tmp_path / "sub", {".gitignore": "*.tmp\n", "docs/a.md": "a\n", "docs/b.md": "b\n"})
    add_submodule(repo, sub, "ext")
    (repo / "ext/docs/a.md").unlink()
    # `*.log` is ignored by the superproject only, `*.tmp` by the submodule only.
    write(repo, {"ext/new.md": "n\n", "ext/notes.tmp": "t\n", "ext/run.log": "l\n"})
    files = list_repo_files(repo)
    assert "ext/docs/a.md" not in files.files
    assert "ext/docs/a.md" not in files.tracked
    assert not files.exists("ext/docs/a.md")
    assert "ext/docs/b.md" in files.tracked
    assert "ext/new.md" in files.files
    assert "ext/new.md" not in files.tracked
    assert "ext/run.log" in files.files
    assert "ext/notes.tmp" not in files.files


def test_nested_initialized_submodule_deletions_and_untracked_files_count(tmp_path: Path, repo: Path) -> None:
    inner = committed_repo(tmp_path / "inner", {".gitignore": "*.tmp\n", "x.md": "x\n", "y.md": "y\n"})
    outer = committed_repo(tmp_path / "outer", {"o.md": "o\n"})
    add_submodule(outer, inner, "inner")
    add_submodule(repo, outer, "ext")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init", "--recursive")
    (repo / "ext/inner/x.md").unlink()
    write(repo, {"ext/inner/new.md": "n\n", "ext/inner/notes.tmp": "t\n"})
    files = list_repo_files(repo)
    assert "ext/inner/x.md" not in files.files
    assert "ext/inner/y.md" in files.tracked
    assert "ext/inner/new.md" in files.files
    assert "ext/inner/notes.tmp" not in files.files
    assert files.uninitialized_submodules == frozenset()


def test_a_git_that_refuses_staged_recursion_gets_the_same_files(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = committed_repo(tmp_path / "inner", {"x.md": "x\n"})
    outer = committed_repo(tmp_path / "outer", {"o.md": "o\n"})
    add_submodule(outer, inner, "inner")
    add_submodule(repo, outer, "full")
    add_submodule(repo, outer, "half")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init", "--recursive", "full")
    expected = list_repo_files(repo)
    assert "full/inner/x.md" in expected.files
    assert expected.uninitialized_submodules == {"half/inner"}

    real_run = subprocess.run

    def run(argv, **kwargs):
        if tuple(argv) == repo_files._GIT_COMMANDS["ls_tracked_recursive"]:
            return subprocess.CompletedProcess(
                argv, 128, b"", b"fatal: ls-files --recurse-submodules unsupported mode\n"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_files.subprocess, "run", run)
    assert list_repo_files(repo) == expected


def test_uninitialized_nested_submodule_is_not_listed_inside(tmp_path: Path, repo: Path) -> None:
    inner = committed_repo(tmp_path / "inner", {"x.md": "x\n"})
    outer = committed_repo(tmp_path / "outer", {"o.md": "o\n"})
    add_submodule(outer, inner, "inner")
    add_submodule(repo, outer, "ext")
    # The empty `ext/inner` directory is not a repository: git run there would
    # list `ext` itself, so the listing must not run inside it.
    write(repo, {"ext/stray.md": "s\n"})
    files = list_repo_files(repo)
    assert files.uninitialized_submodules == {"ext/inner"}
    assert "ext/stray.md" in files.files
    assert not any(path.startswith("ext/inner/") for path in files.files)
    assert files.uninitialized_submodule_of("ext/inner/x.md") == "ext/inner"


def test_submodule_listings_use_fixed_argv_with_the_submodule_as_cwd(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = committed_repo(tmp_path / "sub", {"a.md": "a\n"})
    add_submodule(repo, sub, "ext")
    calls: list[tuple[tuple[str, ...], str]] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs["cwd"]))
        assert "shell" not in kwargs
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_files.subprocess, "run", recording_run)
    list_repo_files(repo)
    commands = repo_files._GIT_COMMANDS
    assert all(argv in commands.values() for argv, _ in calls)
    in_sub = {argv for argv, cwd in calls if cwd == str(repo / "ext")}
    assert in_sub == {commands["ls_tracked_top"], commands["ls_deleted"], commands["ls_untracked"]}


def test_git_failure_inside_a_submodule_raises_with_stderr(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = committed_repo(tmp_path / "sub", {"a.md": "a\n"})
    add_submodule(repo, sub, "ext")
    real_run = subprocess.run

    def failing_in_sub(argv, **kwargs):
        if kwargs["cwd"] == str(repo / "ext") and tuple(argv) == repo_files._GIT_COMMANDS["ls_deleted"]:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: broken submodule\n")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repo_files.subprocess, "run", failing_in_sub)
    with pytest.raises(RepoFilesError) as info:
        list_repo_files(repo)
    assert info.value.returncode == 128
    assert "broken submodule" in info.value.stderr


def test_untracked_nested_repository_is_a_directory(repo: Path) -> None:
    nested = repo / "scratch"
    nested.mkdir()
    git(nested, "init", "-q")
    write(nested, {"a.md": "a\n"})
    files = list_repo_files(repo)
    assert "scratch" in files.dirs
    assert "scratch/" not in files.files


def test_not_a_git_repository_raises_with_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    # Stop git from finding a repository above tmp_path.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(RepoFilesError) as info:
        list_repo_files(plain)
    assert info.value.returncode not in (None, 0)
    assert "not a git repository" in info.value.stderr
    assert "safe.directory" in str(info.value)


def test_git_missing_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(repo_files.subprocess, "run", missing)
    with pytest.raises(RepoFilesError) as info:
        list_repo_files(repo)
    assert info.value.returncode is None
    assert "FileNotFoundError" in str(info.value)
