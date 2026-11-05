"""Which repository paths exist, as git sees them, for the docs check.

Git decides, not the filesystem: a path exists when it is tracked or untracked
but not ignored, so a local checkout full of generated or ignored files answers
the same as a clean CI checkout. Paths are exact-case strings, so a reference
that only resolves on a case-insensitive filesystem still fails on macOS.

Every answer that depends on letter case is case-sensitive, whatever the
checkout's ``core.ignorecase`` says: macOS sets it to true, which would make a
``.gitignore`` pattern ``qa/`` hide ``docs/QA/x.md`` locally but not in CI.

Every git call is one of the fixed commands in ``_GIT_COMMANDS``, chosen by
name, with no shell, the repository root or one of its initialized submodules
as ``cwd``, and a timeout; the paths ``ignored()`` asks about go to git on
stdin, never into argv. A git
failure raises ``RepoFilesError``; there is no filesystem fallback, so there is
no second, less deterministic mode.
"""

from __future__ import annotations

import posixpath
import subprocess
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from code_intel.code_paths import GIT_CREATIONFLAGS, GIT_ERRORS, is_under
from code_intel.doc_rules import STAGE
from code_intel.problems import Problem, limitation

GIT_TIMEOUT_S = 30
_GITLINK_MODE = "160000"
# git 2.34 (Ubuntu 22.04, the CI runners) refuses `ls-files -s --recurse-submodules` with this.
_RECURSE_STAGED_UNSUPPORTED = "unsupported mode"
_FIX = "run inside a git checkout; in a container, `git config --global --add safe.directory <repo>`"

# Ignore rules match case-sensitively here, as on CI's Linux checkout.
_CASE_SENSITIVE = ("-c", "core.ignorecase=false")
# Every command this module runs. Callers name one, so no argv a caller builds
# ever reaches `subprocess.run`.
_GIT_COMMANDS: dict[str, tuple[str, ...]] = {
    # Tracked files with their mode, recursing into initialized submodules,
    # where an uninitialized submodule stays a mode-160000 entry.
    "ls_tracked_recursive": ("git", "ls-files", "-z", "-s", "--recurse-submodules"),
    # The non-recursive listing names every gitlink of the repository it runs
    # in, initialized or not.
    "ls_tracked_top": ("git", "ls-files", "-z", "-s"),
    # Tracked files deleted from the work tree but not staged: git still lists
    # them as tracked, yet a doc naming one points at nothing. Neither this nor
    # the untracked listing can recurse into submodules, so both also run inside
    # each initialized one.
    "ls_deleted": ("git", "ls-files", "-z", "--deleted"),
    "ls_untracked": ("git", *_CASE_SENSITIVE, "ls-files", "-z", "--others", "--exclude-standard"),
    "check_ignore": ("git", *_CASE_SENSITIVE, "check-ignore", "-z", "--stdin"),
}


class RepoFilesError(RuntimeError):
    """git could not list the repository. ``stderr`` is git's own explanation
    (not a repository, dubious ownership, git missing); ``fix`` says what to do."""

    fix = _FIX

    def __init__(self, argv: tuple[str, ...], detail: str, returncode: int | None = None) -> None:
        self.argv = argv
        self.stderr = detail
        self.returncode = returncode
        status = f"exit {returncode}" if returncode is not None else "did not run"
        super().__init__(f"`{' '.join(argv)}` failed ({status}): {detail.strip() or 'no output'}. Fix: {_FIX}")


def _run_git(cwd: Path, command: str, stdin: bytes | None = None, ok: tuple[int, ...] = (0,)) -> bytes:
    """Run the ``_GIT_COMMANDS`` entry ``command`` in ``cwd`` (the repository
    root or an initialized submodule's)."""
    argv = _GIT_COMMANDS[command]
    try:
        out = subprocess.run(
            list(argv),
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            creationflags=GIT_CREATIONFLAGS,
        )
    except GIT_ERRORS as exc:
        raise RepoFilesError(argv, f"{type(exc).__name__}: {exc}") from exc
    if out.returncode not in ok:
        raise RepoFilesError(argv, out.stderr.decode("utf-8", "replace"), out.returncode)
    return out.stdout


def _split_z(data: bytes) -> list[str]:
    # git prints paths as raw bytes; surrogateescape keeps a non-UTF-8 name
    # round-trippable instead of failing the whole listing.
    return [item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item]


def _staged(data: bytes) -> list[tuple[str, str]]:
    """``(mode, path)`` from ``ls-files -s -z`` lines ``<mode> <sha> <stage>\\t<path>``."""
    entries: list[tuple[str, str]] = []
    for item in _split_z(data):
        meta, _, path = item.partition("\t")
        entries.append((meta.split(" ", 1)[0], path))
    return entries


def _parents(path: str) -> Iterable[str]:
    parent = posixpath.dirname(path)
    while parent:
        yield parent
        parent = posixpath.dirname(parent)


@dataclass(frozen=True)
class RepoFiles:
    """The git-visible paths of one checkout.

    ``files``: tracked plus untracked-not-ignored files, in the repository and
    in every initialized submodule (nested ones included, each read with its
    own ignore rules); a tracked file deleted from the work tree is in
    neither. ``tracked``: tracked files only, for the symbol and word
    tables, so an untracked scratch file never makes a name resolve locally
    that fails in CI. ``dirs``: every parent of ``files``, every
    submodule path and every untracked directory git lists as a unit (a nested
    repository). ``submodules``: top-level and uninitialized nested gitlink
    paths; ``uninitialized_submodules``: those with no file checked out.
    """

    repo_root: Path
    files: frozenset[str]
    tracked: frozenset[str]
    dirs: frozenset[str]
    submodules: frozenset[str] = field(default_factory=frozenset)
    uninitialized_submodules: frozenset[str] = field(default_factory=frozenset)

    def exists(self, path: str) -> bool:
        """``path`` (repo-relative POSIX, no trailing slash) is a git-visible file or directory."""
        return path in self.files or path in self.dirs

    def uninitialized_submodule_of(self, path: str) -> str | None:
        """The uninitialized submodule ``path`` points into (or is), if any."""
        for sub in self.uninitialized_submodules:
            if is_under(path, sub):
                return sub
        return None

    def ignored(self, paths: Iterable[str]) -> frozenset[str]:
        """The subset of ``paths`` that git ignores (``git check-ignore``).

        Paths are repo-relative POSIX; a trailing ``/`` asks about a directory
        pattern such as ``build/``. Some paths are answered "not ignored"
        without asking, because git would fail the whole batch on them: a path
        with a NUL (cannot be passed), an empty, absolute or ``..``-escaping
        path (outside the repository), and a path inside a submodule (git
        refuses paths that cross into one). A path that is a work-tree symlink
        with a trailing ``/``, or goes through one, is answered by asking about
        the symlink itself (see ``_symlink_in``), because git refuses to look
        beyond a symlink. Paths are normalized first, so a ``.`` or ``..``
        component can't hide a symlink (``./.env/``, ``.env/.``).
        """
        query = {p: self._query_for(p) for p in paths if self._askable(p)}
        asked = sorted(set(query.values()))
        if not asked:
            return frozenset()
        payload = b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in asked)
        # Exit 1 means "nothing is ignored"; only >1 is a failure.
        out = _run_git(self.repo_root, "check_ignore", stdin=payload, ok=(0, 1))
        answered = frozenset(_split_z(out))
        return frozenset(p for p, q in query.items() if q in answered)

    def _query_for(self, path: str) -> str:
        """What git is asked about ``path``: its normalized form (still naming a
        directory when ``path`` did), or the symlink that form looks into."""
        normalized = posixpath.normpath(path)
        if path.endswith(("/", "/.", "/..")):
            normalized += "/"
        return self._symlink_in(normalized) or normalized

    def _symlink_in(self, path: str) -> str | None:
        """The outermost work-tree symlink that ``path`` looks into, if any.

        Worktree checkouts symlink untracked files such as ``.env`` to the main
        checkout, and git fails the whole ``check-ignore`` batch on ``.env/`` or
        ``.env/x`` ("beyond a symbolic link"). Asking about the symlink itself
        works, and git ignores everything under an ignored path, so its answer
        stands for the path's. A final component without a trailing ``/`` is
        not looked into, so it is asked as it is.
        """
        parts = path.rstrip("/").split("/")
        looked_into = len(parts) if path.endswith("/") else len(parts) - 1
        for end in range(1, looked_into + 1):
            prefix = "/".join(parts[:end])
            if (self.repo_root / prefix).is_symlink():
                return prefix
        return None

    def _askable(self, path: str) -> bool:
        if not path or "\0" in path or path.startswith("/"):
            return False
        bare = path.rstrip("/")
        normalized = posixpath.normpath(bare) if bare else ""
        if not normalized or normalized == "." or normalized.split("/", 1)[0] == "..":
            return False
        return not any(is_under(normalized, sub) for sub in self.submodules)

    def submodule_limitations(self, stage: str = STAGE) -> list[Problem]:
        """One ``submodule_missing`` limitation per uninitialized submodule:
        references into it cannot be checked here, but the result stays usable
        (CI checks out submodules and does check them)."""
        return [
            limitation(
                "submodule_missing",
                stage=stage,
                file=sub,
                line=None,
                message=(
                    f"submodule {sub} is not checked out, so references into it are not checked; "
                    "run `git submodule update --init --recursive`"
                ),
            )
            for sub in sorted(self.uninitialized_submodules)
        ]


def _initialized_submodules(repo_root: Path, top: list[tuple[str, str]], recursive: list[tuple[str, str]]) -> list[str]:
    """Every submodule the recursive listing expanded, nested ones included,
    parents first. A nested gitlink is only named by the listing of the
    submodule that holds it, so each initialized one is listed in turn."""
    expanded = {parent for _, path in recursive for parent in _parents(path)}
    queue = deque(sorted(path for mode, path in top if mode == _GITLINK_MODE and path in expanded))
    found: list[str] = []
    while queue:
        sub = queue.popleft()
        found.append(sub)
        nested = _staged(_run_git(repo_root / sub, "ls_tracked_top"))
        queue.extend(
            sorted(f"{sub}/{path}" for mode, path in nested if mode == _GITLINK_MODE and f"{sub}/{path}" in expanded)
        )
    return found


def _staged_by_hand(directory: Path, prefix: str = "") -> list[tuple[str, str]]:
    """``ls-files -s --recurse-submodules`` for a git that refuses it. A submodule
    without its own ``.git`` is not initialized and stays a gitlink entry (git run
    inside it would list the superproject)."""
    entries: list[tuple[str, str]] = []
    for mode, path in _staged(_run_git(directory, "ls_tracked_top")):
        if mode == _GITLINK_MODE and (directory / path / ".git").exists():
            entries.extend(_staged_by_hand(directory / path, f"{prefix}{path}/"))
        else:
            entries.append((mode, f"{prefix}{path}"))
    return entries


def _staged_recursive(repo_root: Path) -> list[tuple[str, str]]:
    try:
        return _staged(_run_git(repo_root, "ls_tracked_recursive"))
    except RepoFilesError as exc:
        if _RECURSE_STAGED_UNSUPPORTED not in exc.stderr:
            raise
    return _staged_by_hand(repo_root)


def list_repo_files(repo_root: Path) -> RepoFiles:
    """List the checkout's git-visible paths. Raises ``RepoFilesError``."""
    recursive = _staged_recursive(repo_root)
    top = _staged(_run_git(repo_root, "ls_tracked_top"))
    untracked = _split_z(_run_git(repo_root, "ls_untracked"))
    deleted = set(_split_z(_run_git(repo_root, "ls_deleted")))
    for sub in _initialized_submodules(repo_root, top, recursive):
        untracked.extend(f"{sub}/{path}" for path in _split_z(_run_git(repo_root / sub, "ls_untracked")))
        deleted.update(f"{sub}/{path}" for path in _split_z(_run_git(repo_root / sub, "ls_deleted")))

    tracked = frozenset(path for mode, path in recursive if mode != _GITLINK_MODE) - deleted
    gitlinks = {path for mode, path in top if mode == _GITLINK_MODE}
    # A gitlink the recursive listing still shows was not recursed into.
    unexpanded = {path for mode, path in recursive if mode == _GITLINK_MODE}
    files = set(tracked)
    dirs: set[str] = set(gitlinks | unexpanded)
    for path in untracked:
        # An untracked directory with its own .git is listed once, as `dir/`.
        if path.endswith("/"):
            dirs.add(path.rstrip("/"))
        else:
            files.add(path)
    for path in files | dirs:
        dirs.update(_parents(path))
    tracked_dirs = {parent for path in tracked for parent in _parents(path)}
    uninitialized = (gitlinks | unexpanded) - tracked_dirs
    return RepoFiles(
        repo_root=repo_root,
        files=frozenset(files),
        tracked=tracked,
        dirs=frozenset(dirs),
        submodules=frozenset(gitlinks | unexpanded),
        uninitialized_submodules=frozenset(uninitialized),
    )
