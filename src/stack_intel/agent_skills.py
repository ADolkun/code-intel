"""Skills: ``<root>/<dir>/SKILL.md``, one directory below a skill root.

A ``SKILL.md`` deeper down (a template under ``references/``) is not a skill.

- ``.claude/skills`` (Claude Code): frontmatter that Claude Code cannot read
  even after its retry; a ``synced`` directory, an effective name (``name``,
  else the directory) of ``anthropic-skills`` or ``anthropic-skills:*``, or
  one with a ``<``, control, zero-width, bidi or private-use character, which
  Claude Code does not load.
- ``.agents/skills`` and ``.github/skills`` (VS Code): only frontmatter that
  does not parse, which makes VS Code skip the skill. VS Code loads a skill
  with a missing, mismatched or upper-case name under its folder name and
  cuts long names and descriptions, so the Agent Skills rules are not
  default findings there.
- The Agent Skills rules apply to configured ``skill_dirs`` and, under
  ``strict``, to every root: frontmatter present; ``name`` 1-64 lowercase
  letters, digits and single inner hyphens, equal to the directory;
  ``description`` a non-blank string of at most 1024 characters.
- ``strict`` also requires a ``SKILL.md`` in every directory directly under a
  root, and reports repeated keys and frontmatter that is not clean YAML.
"""

from __future__ import annotations

import posixpath
import unicodedata
from collections.abc import Collection
from pathlib import Path

from stack_intel.agent_frontmatter import (
    Frontmatter,
    finding,
    is_blank,
    parse_frontmatter,
    read_text,
    strict_parse_findings,
)
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


def _name_problem(name: str) -> str | None:
    if len(name) > _MAX_NAME:
        return f"must be at most {_MAX_NAME} characters (Agent Skills)"
    if any(c != "-" and (not c.isalnum() or c.isupper()) for c in name):
        return "must be lowercase letters, digits and hyphens (Agent Skills)"
    if name.startswith("-") or name.endswith("-") or "--" in name:
        return "must not start or end with a hyphen or hold '--' (Agent Skills)"
    return None


def _check_spec(rel: str, directory: str, meta: Frontmatter, findings: list[Finding]) -> None:
    name = meta.data.get("name")
    if is_blank(name):
        findings.append(
            finding(rel, meta.line("name"), "name", "frontmatter 'name' must be a non-blank string (Agent Skills)")
        )
    elif isinstance(name, str):
        if (problem := _name_problem(name)) is not None:
            findings.append(finding(rel, meta.line("name"), name, f"skill name {name!r} {problem}"))
        elif name != directory:
            findings.append(
                finding(
                    rel,
                    meta.line("name"),
                    name,
                    f"skill name {name!r} does not match its directory {directory!r} (Agent Skills)",
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
    """Check the skills under ``root``: ``claude`` for ``.claude/skills``,
    ``spec`` for the Agent Skills rules. Returns the ``SKILL.md`` files read."""
    files = skill_files(root, visible)
    for rel in files:
        text = read_text(repo_root, rel, findings)
        if text is None:
            continue
        directory = posixpath.basename(posixpath.dirname(rel))
        read = parse_frontmatter(text, claude=claude, retry=True)
        if read.error is not None:
            reader = (
                "Claude Code loads the skill without it"
                if claude
                else "the Agent Skills reader cannot read its name and description"
                if root not in SPEC_ROOTS
                else "VS Code skips the skill"
            )
            findings.append(finding(rel, read.error.line, "frontmatter", f"{read.error.message}; {reader}"))
        meta = read.meta
        if strict:
            findings.extend(strict_parse_findings(rel, read))
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
                _check_spec(rel, directory, meta, findings)
    if strict:
        _missing_skill_files(root, visible, findings)
    return files
