"""Checks of the agent instruction files Claude Code and GitHub Copilot load.

The conventional locations at the repository root are found and checked on
every run, with or without an ``[agents]`` table, which only adds skill
roots, the ``strict`` policy and the instruction map. A default check fires
only where the file is broken for a documented reader of its location, so a
finding is never wrong for any of them:

- ``.claude/rules/**/*.md`` (Claude Code; Copilot CLI): frontmatter, when
  present, closes and parses as a mapping with no repeated key; ``paths``,
  when present, is a non-blank string or a list of non-blank strings; each
  glob has closed brackets and matches a git-visible file (``agent_globs``).
- ``.claude/agents/**/*.md`` (Claude Code; Copilot CLI): frontmatter that
  parses; with a ``name``, a non-blank ``description``, a name of at most 256
  characters, not starting with ``-``, without ``:``, unique in the tree.
- skills: ``agent_skills``.
- ``.github/instructions/**/*.instructions.md`` (Copilot): when the
  frontmatter parses, no repeated key, ``applyTo`` a string or a list of
  non-blank strings whose globs match a file, ``excludeAgent`` one of
  ``code-review`` and ``cloud-agent``. Copilot CLI recovers its keys from
  YAML that does not parse, so a parse failure is not reported.
- ``.github/agents/**/*.md`` (Copilot): a base name of ``.``, ``-``, ``_``,
  ASCII letters and digits; parsed frontmatter has a non-blank
  ``description`` and a ``target`` of ``vscode`` or ``github-copilot``.
- MCP configs and hooks: ``agent_json``. ``@path`` imports: ``agent_imports``.
- ``strict``: every rule has frontmatter with ``paths`` and no ``applyTo``,
  the Agent Skills rules apply to ``.claude/skills`` too, and every directory
  under a skill root holds a ``SKILL.md``.
- The ``instruction_map`` section's ``- `label`: `glob`, ...`` lines name
  exactly the root ``.claude/rules`` files' stems, each with that file's ``paths``.

Nested ``.claude/`` directories (a sub-project launched on its own) are not read.
A finding is a ``doc_report.Finding`` (``path:line: message`` when printed),
so the docs check reports it with the docs findings.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from stack_intel import agent_imports, agent_json, agent_skills
from stack_intel.agent_frontmatter import Frontmatter, finding, is_blank, parse_frontmatter, read_text
from stack_intel.agent_globs import claude_glob, copilot_glob, split_globs
from stack_intel.config import AgentsConfig, InstructionMap
from stack_intel.doc_report import Finding
from stack_intel.markdown_facts import doc_facts_from_text
from stack_intel.repo_files import RepoFiles

RULES_DIR = ".claude/rules"
AGENTS_DIR = ".claude/agents"
INSTRUCTIONS_DIR = ".github/instructions"
INSTRUCTIONS_SUFFIX = ".instructions.md"
COPILOT_AGENTS_DIR = ".github/agents"

_MAP_LINE = re.compile(r"^\s*[-*+]\s+`([^`]+)`\s*:(.*)$")
_AGENT_FILE_NAME = re.compile(r"[A-Za-z0-9._-]+")
_EXCLUDE_AGENTS = ("code-review", "cloud-agent")
_TARGETS = ("vscode", "github-copilot")
_MAX_SUBAGENT_NAME = 256


@dataclass(frozen=True)
class AgentReport:
    findings: list[Finding]
    files: tuple[str, ...]  # every agent file read


def _files_under(directory: str, visible: Collection[str], suffix: str) -> list[str]:
    prefix = directory + "/"
    return sorted(rel for rel in visible if rel.startswith(prefix) and rel.endswith(suffix))


def _globs(meta: Frontmatter, key: str) -> list[tuple[str, int, bool]] | None:
    """``(glob, line, from a split string)`` for each glob in ``key``'s
    string or list of non-blank strings (none when absent); None for any
    other value."""
    value = meta.data.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [(glob, meta.line(key), True) for glob in split_globs(value)]
    if isinstance(value, list) and all(not is_blank(item) for item in value):
        return [(str(item), meta.item_line(key, index), False) for index, item in enumerate(value)]
    return None


# ── .claude/rules ────────────────────────────────────────────────────────


def _check_rule(
    rel: str, text: str, visible: Collection[str], strict: bool, findings: list[Finding]
) -> tuple[str, ...] | None:
    """The rule's globs for the instruction map: None when they cannot be read."""
    read = parse_frontmatter(text)
    if read.error is not None:
        message = f"{read.error.message}; Claude Code ignores it and loads the rule for every file"
        findings.append(finding(rel, read.error.line, "frontmatter", message))
        return None
    if not read.present:
        if strict:
            message = "missing YAML frontmatter: the file must open with a --- line (strict)"
            findings.append(finding(rel, 1, "frontmatter", message))
            return None
        return ()
    meta = read.meta
    if meta is None:
        return None
    findings.extend(meta.repeat_findings(rel))
    if strict and "applyTo" in meta.keys:
        message = "frontmatter key 'applyTo' is not allowed; use 'paths' (strict)"
        findings.append(finding(rel, meta.line("applyTo"), "applyTo", message))
    value = meta.data.get("paths")
    globs = None if value == "" or (isinstance(value, str) and value.isspace()) else _globs(meta, "paths")
    if globs is None:
        message = "frontmatter 'paths' must be a non-blank string or a list of non-blank strings"
        findings.append(finding(rel, meta.line("paths"), "paths", message))
        return None
    if strict and not globs:
        findings.append(finding(rel, meta.line("paths"), "paths", "frontmatter 'paths' must name a glob (strict)"))
        return None
    for glob, line, split in globs:
        verdict = claude_glob(glob, visible, split=split)
        if verdict == "invalid":
            message = f"paths glob {glob!r} has a '[' that no ']' closes; Claude Code matches no file with it"
            findings.append(finding(rel, line, glob, message))
        elif verdict == "no_match":
            findings.append(finding(rel, line, glob, f"paths glob {glob!r} matches no git-visible file"))
    return tuple(glob for glob, _, _ in globs)


# ── .claude/agents ───────────────────────────────────────────────────────


def _check_subagent(rel: str, text: str, first_by_name: dict[str, str], findings: list[Finding]) -> None:
    read = parse_frontmatter(text)
    if read.error is not None:
        findings.append(
            finding(rel, read.error.line, "frontmatter", f"{read.error.message}; Claude Code skips the subagent")
        )
        return
    meta = read.meta
    name = None if meta is None else meta.data.get("name")
    if meta is None or not isinstance(name, str):
        return
    line = meta.line("name")
    description = meta.data.get("description")
    if description is None or (isinstance(description, str) and not description.strip()):
        findings.append(
            finding(rel, meta.line("description"), name, f"subagent {name!r} has no description; Claude Code skips it")
        )
    if len(name) > _MAX_SUBAGENT_NAME:
        message = f"subagent name {name[:40]!r}... is longer than {_MAX_SUBAGENT_NAME} characters (Claude Code)"
        findings.append(finding(rel, line, name, message))
    if name.startswith("-"):
        findings.append(finding(rel, line, name, f"subagent name {name!r} must not start with '-' (Claude Code)"))
    if ":" in name:
        findings.append(finding(rel, line, name, f"subagent name {name!r} must not contain ':' (Claude Code)"))
    if name in first_by_name:
        message = f"subagent name {name!r} is also used by {first_by_name[name]}; Claude Code loads only one of them"
        findings.append(finding(rel, line, name, message))
    else:
        first_by_name[name] = rel


# ── .github/instructions and .github/agents ──────────────────────────────


def _check_instructions(rel: str, text: str, visible: Collection[str], findings: list[Finding]) -> None:
    meta = parse_frontmatter(text).meta
    if meta is None:
        return
    findings.extend(meta.repeat_findings(rel))
    if meta.data.get("applyTo") is not None:
        globs = _globs(meta, "applyTo")
        if globs is None:
            message = "frontmatter 'applyTo' must be a string or a list of non-blank strings"
            findings.append(finding(rel, meta.line("applyTo"), "applyTo", message))
        for glob, line, split in globs or ():
            if copilot_glob(glob, visible, split=split) == "no_match":
                findings.append(finding(rel, line, glob, f"applyTo glob {glob!r} matches no git-visible file"))
    exclude = meta.data.get("excludeAgent")
    if isinstance(exclude, str) and exclude not in _EXCLUDE_AGENTS:
        message = f"excludeAgent {exclude!r} must be 'code-review' or 'cloud-agent'"
        findings.append(finding(rel, meta.line("excludeAgent"), exclude, message))


def _check_copilot_agent(rel: str, text: str, findings: list[Finding]) -> None:
    filename = posixpath.basename(rel)
    base = filename.removesuffix(".md").removesuffix(".agent")
    if not _AGENT_FILE_NAME.fullmatch(base):
        message = f"custom agent file name {filename!r} may use only ASCII letters, digits, '.', '-' and '_' (Copilot)"
        findings.append(finding(rel, 1, filename, message))
    meta = parse_frontmatter(text).meta
    if meta is None:
        return
    if is_blank(meta.data.get("description")):
        message = f"custom agent {base!r} has no description; Copilot requires one"
        findings.append(finding(rel, meta.line("description"), "description", message))
    target = meta.data.get("target")
    if target is not None and target not in _TARGETS:
        message = f"custom agent target {target!r} must be 'vscode' or 'github-copilot'"
        findings.append(finding(rel, meta.line("target"), str(target), message))


# ── Instruction map ──────────────────────────────────────────────────────


def _map_entries(lines: list[str], first_line: int) -> list[tuple[int, str, frozenset[str]]]:
    entries = []
    for offset, text in enumerate(lines):
        match = _MAP_LINE.match(text)
        if match is not None:
            entries.append((first_line + offset, match.group(1), frozenset(re.findall(r"`([^`]+)`", match.group(2)))))
    return entries


def _check_map(
    repo_root: Path,
    instruction_map: InstructionMap,
    rule_paths: dict[str, tuple[str, ...] | None],
    findings: list[Finding],
) -> None:
    rel = instruction_map.file
    text = read_text(repo_root, rel, findings)
    if text is None:
        return
    facts, problem = doc_facts_from_text(rel, text)
    if facts is None:
        findings.append(finding(rel, 1, rel, problem.message if problem is not None else "cannot parse the file"))
        return
    heading = next((h for h in facts.headings if h.text == instruction_map.heading), None)
    if heading is None:
        findings.append(finding(rel, 1, instruction_map.heading, f"no heading {instruction_map.heading!r}"))
        return
    end = next((h.line for h in facts.headings if h.line > heading.line and h.level <= heading.level), None)
    body = text.splitlines()[heading.line : (end - 1) if end is not None else None]
    seen: set[str] = set()
    for line, label, globs in _map_entries(body, heading.line + 1):
        rule = f"{RULES_DIR}/{label}.md"
        if label in seen:
            findings.append(finding(rel, line, label, f"instruction map lists {label!r} more than once"))
        seen.add(label)
        if label not in rule_paths:
            findings.append(finding(rel, line, label, f"instruction map lists {label!r} but {rule} does not exist"))
            continue
        paths = rule_paths[label]
        if paths is not None and globs != set(paths):
            only_map = ", ".join(sorted(globs - set(paths))) or "none"
            only_rule = ", ".join(sorted(set(paths) - globs)) or "none"
            findings.append(
                finding(
                    rel,
                    line,
                    label,
                    f"instruction map globs for {label!r} differ from the paths in {rule} "
                    f"(only in the map: {only_map}; only in the rule: {only_rule})",
                )
            )
    for label in sorted(set(rule_paths) - seen):
        findings.append(finding(rel, heading.line, label, f"instruction map has no entry for {RULES_DIR}/{label}.md"))


# ── Entry ────────────────────────────────────────────────────────────────


def check(repo_root: Path, agents: AgentsConfig, files: RepoFiles) -> AgentReport:
    """Every agent-file finding in ``files``' checkout."""
    visible = files.files
    findings: list[Finding] = []
    read: list[str] = []

    rule_paths: dict[str, tuple[str, ...] | None] = {}
    for rel in _files_under(RULES_DIR, visible, ".md"):
        read.append(rel)
        text = read_text(repo_root, rel, findings)
        stem = rel[len(RULES_DIR) + 1 : -len(".md")]
        rule_paths[stem] = None if text is None else _check_rule(rel, text, visible, agents.strict, findings)

    first_by_name: dict[str, str] = {}
    for rel in _files_under(AGENTS_DIR, visible, ".md"):
        read.append(rel)
        if (text := read_text(repo_root, rel, findings)) is not None:
            _check_subagent(rel, text, first_by_name, findings)

    strict = agents.strict
    read += agent_skills.check_root(
        repo_root, agent_skills.CLAUDE_SKILLS, visible, findings, claude=True, spec=strict, strict=strict
    )
    for root in (*agent_skills.SPEC_ROOTS, *agents.skill_dirs):
        read += agent_skills.check_root(repo_root, root, visible, findings, claude=False, spec=True, strict=strict)

    for rel in _files_under(INSTRUCTIONS_DIR, visible, INSTRUCTIONS_SUFFIX):
        read.append(rel)
        if (text := read_text(repo_root, rel, findings)) is not None:
            _check_instructions(rel, text, visible, findings)
    for rel in _files_under(COPILOT_AGENTS_DIR, visible, ".md"):
        read.append(rel)
        if (text := read_text(repo_root, rel, findings)) is not None:
            _check_copilot_agent(rel, text, findings)

    json_checks = (
        (agent_json.CLAUDE_MCP, agent_json.check_claude_mcp),
        (agent_json.COPILOT_MCP, agent_json.check_copilot_mcp),
        (agent_json.VSCODE_MCP, agent_json.check_vscode_mcp),
    )
    for rel, check_json in json_checks:
        if rel in visible:
            read.append(rel)
            if (text := read_text(repo_root, rel, findings)) is not None:
                check_json(text, findings)
    for rel in _files_under(agent_json.HOOKS_DIR, visible, ".json"):
        if "/" not in rel[len(agent_json.HOOKS_DIR) + 1 :]:
            read.append(rel)
            if (text := read_text(repo_root, rel, findings)) is not None:
                agent_json.check_hooks(rel, text, findings)

    read += agent_imports.check(repo_root, files, findings)

    if agents.instruction_map is not None:
        _check_map(repo_root, agents.instruction_map, rule_paths, findings)
    findings.sort(key=lambda f: (f.file, f.line, f.kind, f.text))
    return AgentReport(findings, tuple(sorted(set(read))))
