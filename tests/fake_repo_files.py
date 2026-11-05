"""A git-free ``RepoFiles`` for the docs-check tests."""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from code_intel.repo_files import RepoFiles


@dataclass(frozen=True)
class FakeRepoFiles(RepoFiles):
    """``RepoFiles`` without git: ``ignored()`` answers from a fixed set and
    records each batch it was asked in ``asked``."""

    ignored_paths: frozenset[str] = field(default_factory=frozenset)
    asked: list[set[str]] = field(default_factory=list, compare=False)

    def ignored(self, paths: Iterable[str]) -> frozenset[str]:
        wanted = set(paths)
        self.asked.append(wanted)
        return frozenset(p for p in wanted if p.rstrip("/") in self.ignored_paths)


def repo_files_on_disk(repo_root: Path, *, untracked: Iterable[str] = ()) -> FakeRepoFiles:
    """Every file under ``repo_root`` (``.git`` excluded) as tracked, except
    ``untracked``, which are git-visible but not tracked."""
    files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            files.add((Path(dirpath) / name).relative_to(repo_root).as_posix())
    dirs: set[str] = set()
    for path in files:
        parent = posixpath.dirname(path)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    return FakeRepoFiles(
        repo_root=repo_root,
        files=frozenset(files),
        tracked=frozenset(files - set(untracked)),
        dirs=frozenset(dirs),
    )
