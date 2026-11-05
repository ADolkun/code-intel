"""File walking, file stamps and source-text helpers shared by every analyzer.

One walk rule (skip directories, suffix filter, sorted repo-relative POSIX
paths) and one test-file rule for Python and JS/TS, so the analyzers agree on
which files exist and which of them are tests.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import tokenize
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from code_intel.problems import Problem, diagnostic

SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".next", "dist", ".git", ".venv", "venv"})
# Build, coverage and test-report output that frontend tooling writes at the top
# of the frontend root. Skipped only there: the same names are legitimate deeper
# (a Next.js `app/build/` route segment, a backend `coverage` package).
FRONTEND_GENERATED_DIRS = frozenset(
    {"coverage", "out", "build", "playwright-report", "test-results", "storybook-static"}
)
TS_SUFFIXES: tuple[str, ...] = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# `.ts/.mts/.cts` cannot contain JSX, so JSX queries never read them.
JSX_SUFFIXES: tuple[str, ...] = (".tsx", ".jsx", ".js", ".mjs", ".cjs")
PY_SUFFIXES: tuple[str, ...] = (".py",)

# git's "racy clean" window: a file changed this close to the moment its stamp
# was cached can change again within the same timestamp tick without changing
# its stamp, so its content hash is checked too.
RACY_WINDOW_NS = 2_000_000_000

# What a `git` subprocess can raise before it returns: OSError (git missing,
# cwd gone) or SubprocessError (a timeout).
GIT_ERRORS: tuple[type[Exception], ...] = (OSError, subprocess.SubprocessError)
# Without CREATE_NO_WINDOW every `git` call flashes a console window on Windows;
# the constant exists only there, and its value is fixed by the Win32 API.
GIT_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

_TEST_DIR_SEGMENTS = frozenset({"__tests__", "__mocks__", "test", "tests", "e2e"})
_TEST_FILE_RES = (
    re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$"),
    re.compile(r"^test_.*\.py$"),
    re.compile(r"_test\.py$"),
)
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
# Decoding failures: a bad or unknown coding cookie raises SyntaxError from
# tokenize.detect_encoding, LookupError for an unknown codec, and
# UnicodeDecodeError for bytes the codec rejects.
_DECODE_ERRORS: tuple[type[Exception], ...] = (SyntaxError, LookupError, UnicodeDecodeError)


@dataclass(frozen=True)
class FileStamp:
    """What a cached file is compared by. ``ctime_ns`` is there because, unlike
    the modification time, the kernel sets it on every content or metadata
    change and nothing can set it back (``os.utime``, ``touch -r`` and
    ``cp -p`` restore mtime only), so a same-size rewrite with its mtime
    restored still changes the stamp, as with git's ``core.trustctime``. On
    Windows it is the creation time, which adds nothing but costs nothing."""

    mtime_ns: int
    size: int
    ino: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, st: os.stat_result) -> FileStamp:
        return cls(st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)

    @property
    def changed_ns(self) -> int:
        """When the file last changed as far as the stamp can tell: the newer of
        mtime and ctime, so a restored mtime cannot make a fresh write look old."""
        return max(self.mtime_ns, self.ctime_ns)


@dataclass(frozen=True)
class SourceFile:
    path: Path  # absolute
    rel: str  # repo-relative POSIX, or the absolute POSIX path outside the repo
    stamp: FileStamp


def rel_posix(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX path, or the absolute POSIX path when outside the repo."""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def is_under(path: str, base: str) -> bool:
    """Repo-relative POSIX ``path`` is ``base`` or inside the directory ``base``
    (a trailing slash on ``base`` is ignored), by whole segments: ``vendor/subway``
    is not under ``vendor/sub``."""
    base = base.rstrip("/")
    return path == base or path.startswith(base + "/")


def cache_key(path: Path | str) -> str:
    """Key for per-file caches: case-insensitive filesystems map to one entry."""
    return os.path.normcase(str(path))


def _real_key(path: Path | str) -> str:
    """``cache_key`` of the real path, so a relative or symlinked spelling of
    the same directory compares equal."""
    return cache_key(os.path.realpath(path))


def _stat(entry: os.DirEntry) -> os.stat_result:
    return entry.stat(follow_symlinks=False)


def iter_source_files(
    root: Path,
    suffixes: tuple[str, ...],
    repo_root: Path,
    problems: list[Problem],
    *,
    stage: str = "walk sources",
    generated_parent: Path | None = None,
    visible: AbstractSet[str] | None = None,
) -> list[SourceFile]:
    """Every file under ``root`` ending in one of ``suffixes``, sorted by ``rel``.

    Skips ``SKIP_DIRS``, every dot directory and symlinks. With ``visible``
    (``RepoFiles.files``: tracked plus untracked-not-ignored), a file whose
    ``rel`` is not in it is skipped, so ignored build output is never read. With
    ``generated_parent`` (frontend walks pass the frontend root), the
    ``FRONTEND_GENERATED_DIRS`` that are direct children of that directory are
    skipped too. A directory that
    cannot be listed adds a ``list_failed`` diagnostic (step ``list directory``)
    and a file that cannot be stat'd adds ``stat_failed`` (step ``stat file``),
    so a walk that covers fewer files never passes as complete. ``stage`` names
    the caller in those diagnostics.
    """
    found: list[SourceFile] = []
    generated_key = _real_key(generated_parent) if generated_parent is not None else None
    stack: list[str] = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                listed = list(entries)
        except OSError as exc:
            problems.append(
                diagnostic(
                    "list_failed",
                    stage=stage,
                    step="list directory",
                    file=rel_posix(Path(current), repo_root),
                    exc=exc,
                )
            )
            continue
        for entry in listed:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = not is_dir and entry.is_file(follow_symlinks=False)
            except OSError as exc:
                problems.append(
                    diagnostic(
                        "stat_failed",
                        stage=stage,
                        step="stat file",
                        file=rel_posix(Path(entry.path), repo_root),
                        exc=exc,
                    )
                )
                continue
            if is_dir:
                generated = (
                    generated_key is not None
                    and entry.name in FRONTEND_GENERATED_DIRS
                    and _real_key(current) == generated_key
                )
                if entry.name not in SKIP_DIRS and not entry.name.startswith(".") and not generated:
                    stack.append(entry.path)
                continue
            if not is_file or not entry.name.endswith(suffixes):
                continue
            path = Path(entry.path)
            rel = rel_posix(path, repo_root)
            if visible is not None and rel not in visible:
                continue
            try:
                stamp = FileStamp.from_stat(_stat(entry))
            except OSError as exc:
                problems.append(diagnostic("stat_failed", stage=stage, step="stat file", file=rel, exc=exc))
                continue
            found.append(SourceFile(path=path, rel=rel, stamp=stamp))
    found.sort(key=lambda f: f.rel)
    return found


def is_test_path(rel: str) -> bool:
    """One test-file rule for Python and JS/TS.

    True when a directory segment is ``__tests__``, ``__mocks__``, ``test``,
    ``tests`` or ``e2e``, or the file name is ``*.test.*``/``*.spec.*`` (JS/TS),
    ``test_*.py``, ``*_test.py`` or ``conftest.py``.
    """
    parts = rel.replace("\\", "/").split("/")
    if any(part in _TEST_DIR_SEGMENTS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name == "conftest.py" or any(rx.search(name) for rx in _TEST_FILE_RES)


def split_source_lines(text: str) -> list[str]:
    """Split Python source the way ``ast`` counts lines (``\\r\\n``, ``\\r``, ``\\n``).

    ``str.splitlines`` also breaks on form feeds, ``\\x85`` and U+2028, which
    would shift every later line number.
    """
    return _LINE_BREAK_RE.split(text)


def read_python_source(
    path: Path,
    rel: str | None = None,
    *,
    stage: str = "read python source",
) -> tuple[str | None, Problem | None]:
    """Read and decode a Python file the way the interpreter does (PEP 263, BOM).

    Returns ``(text, None)`` or ``(None, problem)`` where the problem is a
    ``read_failed`` (step ``read file``) or ``decode_failed`` (step
    ``decode file``) diagnostic naming ``rel`` (default: the POSIX path).
    """
    text, _, problem = _read_python(path, rel, stage=stage, digest=False)
    return text, problem


def read_python_source_with_digest(
    path: Path,
    rel: str | None = None,
    *,
    stage: str = "read python source",
) -> tuple[str | None, str | None, Problem | None]:
    """``read_python_source`` plus the SHA-256 of exactly the bytes it decoded,
    from the same read: a cache that stores the digest beside what it derived
    from the text can never pair it with other bytes. The digest is None only
    when the read failed."""
    return _read_python(path, rel, stage=stage, digest=True)


def read_bytes_with_digest(
    path: Path,
    rel: str | None = None,
    *,
    stage: str = "read file",
) -> tuple[bytes | None, str | None, Problem | None]:
    """``(bytes, SHA-256 of those bytes, None)`` from one read, or ``(None,
    None, problem)`` with a ``read_failed`` diagnostic (step ``read file``)."""
    try:
        data = read_file_bytes(path)
    except OSError as exc:
        name = rel if rel is not None else path.as_posix()
        return None, None, diagnostic("read_failed", stage=stage, step="read file", file=name, exc=exc)
    return data, _digest(data), None


def _read_python(
    path: Path, rel: str | None, *, stage: str, digest: bool
) -> tuple[str | None, str | None, Problem | None]:
    name = rel if rel is not None else path.as_posix()
    try:
        data = read_file_bytes(path)
    except OSError as exc:
        return None, None, diagnostic("read_failed", stage=stage, step="read file", file=name, exc=exc)
    hashed = _digest(data) if digest else None
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding), hashed, None
    except _DECODE_ERRORS as exc:
        return None, hashed, diagnostic("decode_failed", stage=stage, step="decode file", file=name, exc=exc)


def read_file_bytes(path: Path) -> bytes:
    """Every source read goes through here, so a test can change a file right
    after it was read and check that no cache keeps the old result."""
    return path.read_bytes()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=False).hexdigest()


def is_racy(stamp: FileStamp, now_ns: int, window_ns: int = RACY_WINDOW_NS) -> bool:
    """True when ``stamp`` is too recent (or in the future) to trust alone.

    Measured from ``changed_ns``: a write in the same tick as the stamp changes
    neither mtime nor ctime, so either one being recent makes the stamp racy."""
    return now_ns - stamp.changed_ns < window_ns


def content_digest(path: Path) -> str:
    """Hex SHA-256 of the file's bytes, for change detection only. To store a
    digest beside data derived from the file, take both from one read
    (``read_python_source_with_digest``, ``read_bytes_with_digest``), or take
    the digest before that read, so a later edit can only force a re-read."""
    return _digest(read_file_bytes(path))


def fingerprint(files: Iterable[SourceFile]) -> str:
    """Hex SHA-256 over ``(rel, mtime_ns, size, ino, ctime_ns)`` of every file, order-independent."""
    digest = hashlib.sha256(usedforsecurity=False)
    stamps = sorted((f.rel, f.stamp.mtime_ns, f.stamp.size, f.stamp.ino, f.stamp.ctime_ns) for f in files)
    for rel, mtime_ns, size, ino, ctime_ns in stamps:
        digest.update(f"{rel}\0{mtime_ns}\0{size}\0{ino}\0{ctime_ns}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()
