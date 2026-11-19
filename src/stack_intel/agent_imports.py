"""``@path`` imports in the instruction files Claude Code and Copilot CLI expand.

Files: every git-visible ``CLAUDE.md`` and ``AGENTS.md``, ``.claude/CLAUDE.md``
and ``.github/copilot-instructions.md``. Tokens come from Markdown text only
(code spans, fences and table cells are not scanned): ``@`` at the start of a
text run or after whitespace, not escaped as ``\\@``, then a path whose last
segment ends in ``.md``, or that starts with ``./`` or ``../`` and names a
Markdown or text file or has no extension. A ``#fragment`` is dropped.
Quoted, absolute and ``~`` paths, other extensions (Claude Code skips
non-text files) and every other ``@word`` (``@types/node``, mentions) are
not imports this check can prove.

A target resolves when it is a git-visible file or directory relative to the
importing file (Claude Code) or to the repository root (Copilot's base is
unconfirmed, so either counts). A target outside the repository, in an
uninitialized submodule, or one git ignores is not checked: it may exist on
the reader's machine.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from stack_intel.agent_frontmatter import finding, read_text
from stack_intel.doc_report import Finding
from stack_intel.markdown_facts import doc_facts_from_text
from stack_intel.repo_files import RepoFiles

_FIXED_FILES = (".claude/CLAUDE.md", ".github/copilot-instructions.md")
_ANY_DEPTH = ("CLAUDE.md", "AGENTS.md")
_TOKEN = re.compile(r"(?:^|(?<=\s))@((?:\\ |\S)+)")
_NAME_END = re.compile(r"[\w/-]$")
_TEXT_SUFFIXES = frozenset({"", ".md", ".mdx", ".markdown", ".txt"})


def import_files(visible: Collection[str]) -> list[str]:
    return sorted(rel for rel in visible if rel in _FIXED_FILES or posixpath.basename(rel) in _ANY_DEPTH)


def import_path(token: str) -> str | None:
    """The path ``token`` (the text after ``@``) imports, or None when it is not
    an import this check can prove."""
    path = token.replace("\\ ", " ").partition("#")[0]
    if path[:1] in ('"', "'", "/", "~") or not path:
        return None
    if posixpath.basename(path).lower().endswith(".md"):
        return path
    suffix = posixpath.splitext(posixpath.basename(path))[1].lower()
    if path.startswith(("./", "../")) and _NAME_END.search(path) and suffix in _TEXT_SUFFIXES:
        return path
    return None


@dataclass(frozen=True)
class _Import:
    rel: str
    line: int
    token: str
    candidates: tuple[str, ...]


def _candidates(rel: str, path: str) -> tuple[str, ...]:
    out = []
    for base in (posixpath.dirname(rel), ""):
        target = posixpath.normpath(posixpath.join(base, path))
        if target != ".." and not target.startswith("../") and target not in out:
            out.append(target)
    return tuple(out)


def _imports(rel: str, text: str) -> list[_Import]:
    facts, _ = doc_facts_from_text(rel, text)
    if facts is None:
        return []
    found = []
    source_lines = text.splitlines()
    for run in facts.text_runs:
        if run.context == "table":
            continue
        raw = source_lines[run.line - 1] if run.line <= len(source_lines) else ""
        for match in _TOKEN.finditer(run.text):
            path = import_path(match.group(1))
            if path is None or "\\@" + match.group(1) in raw:
                continue
            candidates = _candidates(rel, path)
            if candidates:
                found.append(_Import(rel, run.line, "@" + match.group(1), candidates))
    return found


def check(repo_root: Path, files: RepoFiles, findings: list[Finding]) -> list[str]:
    """Report each import whose target does not exist; returns the files read."""
    read = import_files(files.files)
    unresolved: list[_Import] = []
    for rel in read:
        text = read_text(repo_root, rel, findings)
        if text is None:
            continue
        for found in _imports(rel, text):
            if any(files.exists(c) or files.uninitialized_submodule_of(c) for c in found.candidates):
                continue
            if any((repo_root / c).exists() for c in found.candidates):
                continue
            unresolved.append(found)
    ignored = files.ignored({c for found in unresolved for c in found.candidates})
    for found in unresolved:
        if not ignored.intersection(found.candidates):
            findings.append(
                finding(found.rel, found.line, found.token, f"{found.token} imports a file that does not exist")
            )
    return read
