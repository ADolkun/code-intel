"""Skills: ``<root>/<dir>/SKILL.md``, one directory below a skill root.

A ``SKILL.md`` deeper down (a template under ``references/``) is not a skill.

- ``.claude/skills`` (Claude Code; Copilot CLI): frontmatter, when present,
  parses with no repeated key; the directory is not ``synced`` and the
  effective name (``name``, else the directory) is not ``anthropic-skills`` or
  ``anthropic-skills:*`` and has no ``<``, control, zero-width, bidi or
  private-use character, because Claude Code does not load those.
- Agent Skills roots (``.agents/skills``, ``.github/skills``, the configured
  ``skill_dirs``, and ``.claude/skills`` under ``strict``): frontmatter is
  present and parses; ``name`` is 1-64 lowercase letters, digits and single
  inner hyphens and equals the directory; ``description`` is a non-blank
  string of at most 1024 characters.
- ``strict``: every directory directly under a root holds a ``SKILL.md``.
"""

from __future__ import annotations

import posixpath
import unicodedata
from collections.abc import Collection
from pathlib import Path

from stack_intel.agent_frontmatter import Frontmatter, finding, is_blank, parse_frontmatter, read_text
from stack_intel.doc_report import Finding

CLAUDE_SKILLS = ".claude/skills"
GITHUB_SKILLS = ".github/skills"
SPEC_ROOTS: tuple[str, ...] = (".agents/skills", GITHUB_SKILLS)
CONVENTIONAL_ROOTS: tuple[str, ...] = (CLAUDE_SKILLS, *SPEC_ROOTS)
SKILL_FILE = "SKILL.md"

_MAX_NAME = 64
_MAX_DESCRIPTION = 1024
_INVISIBLE = frozenset(chr(c) for c in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0x061C)) | frozenset(
    chr(c) for c in [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)


def skill_files(root: str, visible: Collection[str]) -> list[str]:
    prefix = root + "/"
    return sorted(
        rel
        for rel in visible
        if rel.startswith(prefix) and rel[len(prefix) :].count("/") == 1 and rel.endswith("/" + SKILL_FILE)
    )


def _hidden_character(name: str) -> bool:
    return any(c == "<" or c in _INVISIBLE or unicodedata.category(c) in ("Cc", "Co") for c in name)


def _check_claude(rel: str, directory: str, meta: Frontmatter | None, findings: list[Finding]) -> None:
    if directory.lower() == "synced":
        findings.append(
            finding(rel, 1, directory, f"skill directory {directory!r} is reserved; Claude Code does not load it")
        )
    name = meta.data.get("name") if meta is not None else None
    effective = name if isinstance(name, str) and name.strip() else directory
    line = meta.line("name") if meta is not None and effective is name else 1
    if effective == "anthropic-skills" or effective.startswith("anthropic-skills:"):
        findings.append(
            finding(rel, line, effective, f"skill name {effective!r} is reserved; Claude Code does not load it")
        )
    if _hidden_character(effective):
        findings.append(
            finding(
                rel,
                line,
                effective,
                f"skill name {effective!r} has a '<', control, zero-width, bidi or private-use character; "
                "Claude Code does not load it",
            )
        )


def _name_problem(name: str, unloaded: str) -> str | None:
    if len(name) > _MAX_NAME:
        return f"must be at most {_MAX_NAME} characters (Agent Skills)"
    if any(c != "-" and (not c.isalnum() or c.isupper()) for c in name):
        return f"must be lowercase letters, digits and hyphens ({unloaded})"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return "must not start or end with a hyphen or hold '--' (Agent Skills)"
    return None


def _check_spec(rel: str, directory: str, meta: Frontmatter, vscode: bool, findings: list[Finding]) -> None:
    unloaded = "Agent Skills; VS Code does not load it" if vscode else "Agent Skills"
    name = meta.data.get("name")
    if is_blank(name):
        findings.append(
            finding(rel, meta.line("name"), "name", "frontmatter 'name' must be a non-blank string (Agent Skills)")
        )
    elif isinstance(name, str):
        if (problem := _name_problem(name, unloaded)) is not None:
            findings.append(finding(rel, meta.line("name"), name, f"skill name {name!r} {problem}"))
        elif name != directory:
            findings.append(
                finding(
                    rel,
                    meta.line("name"),
                    name,
                    f"skill name {name!r} does not match its directory {directory!r} ({unloaded})",
                )
            )
    description = meta.data.get("description")
    if is_blank(description):
        findings.append(
            finding(
                rel,
                meta.line("description"),
                "description",
                "frontmatter 'description' must be a non-blank string (Agent Skills)",
            )
        )
    elif isinstance(description, str) and len(description) > _MAX_DESCRIPTION:
        findings.append(
            finding(
                rel,
                meta.line("description"),
                "description",
                f"skill description is {len(description)} characters; Agent Skills allows at most {_MAX_DESCRIPTION}",
            )
        )


def _missing_skill_files(root: str, visible: Collection[str], findings: list[Finding]) -> None:
    prefix = root + "/"
    below = [rel[len(prefix) :] for rel in visible if rel.startswith(prefix)]
    skills = {rel.split("/", 1)[0] for rel in below if rel.count("/") == 1 and rel.endswith("/" + SKILL_FILE)}
    for directory in sorted({rel.split("/", 1)[0] for rel in below if "/" in rel} - skills):
        findings.append(
            finding(f"{prefix}{directory}", 1, directory, f"skill directory {directory!r} has no SKILL.md (strict)")
        )


def check_root(
    repo_root: Path,
    root: str,
    visible: Collection[str],
    findings: list[Finding],
    *,
    claude: bool,
    spec: bool,
    strict: bool,
) -> list[str]:
    """Check the skills under ``root``; returns the ``SKILL.md`` files read.
    Only ``.github/skills`` findings name VS Code, the reader of that root."""
    vscode = root == GITHUB_SKILLS
    files = skill_files(root, visible)
    for rel in files:
        text = read_text(repo_root, rel, findings)
        if text is None:
            continue
        directory = posixpath.basename(posixpath.dirname(rel))
        read = parse_frontmatter(text)
        if (error := read.failure(strict=strict)) is not None:
            reader = "Claude Code loads the skill without it" if claude else "its name and description cannot be read"
            findings.append(finding(rel, error.line, "frontmatter", f"{error.message}; {reader}"))
        meta = read.meta
        if meta is not None:
            findings.extend(meta.repeat_findings(rel))
        if claude:
            _check_claude(rel, directory, meta, findings)
        if spec:
            if not read.present:
                findings.append(
                    finding(
                        rel,
                        1,
                        "frontmatter",
                        "missing YAML frontmatter: an Agent Skills SKILL.md opens with a --- line, "
                        "then name and description",
                    )
                )
            elif meta is not None:
                _check_spec(rel, directory, meta, vscode, findings)
    if strict:
        _missing_skill_files(root, visible, findings)
    return files
