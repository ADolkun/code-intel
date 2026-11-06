"""Checks of the agent instruction files named by the config's ``[agents]`` table.

- Every rule file under ``rules_dir`` opens with YAML frontmatter holding a
  non-empty ``paths`` list, no ``applyTo``, and each glob in it matches at
  least one git-visible file (a renamed directory would otherwise stop the
  rule from loading without a sound).
- Every ``SKILL.md`` under ``skills_dir`` has a ``name`` equal to its
  directory, unique among skills, and a non-empty ``description``; every
  directory under ``skills_dir`` has a ``SKILL.md``.
- No frontmatter key repeats.
- The ``instruction_map`` section's ``- `label`: `glob`, ...`` lines name
  exactly the rule files' stems, each with exactly that file's ``paths``.
- A ``.mcp.json`` at the repository root is a JSON object whose ``mcpServers``
  object holds only servers with a non-blank string ``command`` or ``url``.

A finding is a ``doc_report.Finding`` (``path:line: message`` when printed),
so the docs check reports it with the docs findings.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from code_intel.config import AgentsConfig, InstructionMap
from code_intel.doc_report import Finding
from code_intel.doc_rules import glob_match
from code_intel.markdown_facts import doc_facts_from_text

KIND = "agent"
STATUS = "invalid"
MCP_CONFIG = ".mcp.json"

_MAP_LINE = re.compile(r"^\s*[-*+]\s+`([^`]+)`\s*:(.*)$")
_BRACES = re.compile(r"\{([^{}]*)\}")


def _finding(rel: str, line: int, text: str, message: str) -> Finding:
    return Finding(rel, line, KIND, STATUS, text, message)


@dataclass(frozen=True)
class _Frontmatter:
    data: dict[object, object]
    keys: dict[str, yaml.Node]
    values: dict[str, yaml.Node]

    def line(self, key: str) -> int:
        node = self.keys.get(key)
        return 1 if node is None else node.start_mark.line + 2

    def item_line(self, key: str, index: int) -> int:
        node = self.values.get(key)
        if isinstance(node, yaml.SequenceNode) and index < len(node.value):
            return node.value[index].start_mark.line + 2
        return self.line(key)


def _frontmatter(rel: str, text: str, findings: list[Finding]) -> _Frontmatter | None:
    """The frontmatter between the opening ``---`` on line 1 and the next
    ``---`` line, or None after adding why it cannot be read. Line numbers in
    the YAML are shifted by the opening fence."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        findings.append(_finding(rel, 1, "frontmatter", "missing YAML frontmatter: the file must open with a --- line"))
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        findings.append(_finding(rel, 1, "frontmatter", "YAML frontmatter is not closed by a --- line"))
        return None
    body = "\n".join(lines[1:end])
    try:
        root = yaml.compose(body, Loader=yaml.SafeLoader)
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        line = 1 if mark is None else mark.line + 2
        findings.append(_finding(rel, line, "frontmatter", f"invalid YAML frontmatter: {reason}"))
        return None
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        findings.append(_finding(rel, 1, "frontmatter", "YAML frontmatter must be a mapping"))
        return None
    keys: dict[str, yaml.Node] = {}
    values: dict[str, yaml.Node] = {}
    for key_node, value_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        key = key_node.value
        if key in keys:
            line, first = key_node.start_mark.line + 2, keys[key].start_mark.line + 2
            findings.append(_finding(rel, line, key, f"frontmatter key {key!r} is repeated (first at line {first})"))
        else:
            keys[key], values[key] = key_node, value_node
    return _Frontmatter(data, keys, values)


def _expand_braces(pattern: str) -> list[str]:
    """``a/*.{ts,tsx}`` as ``a/*.ts`` and ``a/*.tsx``; braces do not nest."""
    match = _BRACES.search(pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    return [expanded for option in match.group(1).split(",") for expanded in _expand_braces(head + option + tail)]


def _matches_a_file(glob: str, visible: Iterable[str]) -> bool:
    patterns = _expand_braces(glob)
    return any(glob_match(rel, pattern) for rel in visible for pattern in patterns)


def _read(repo_root: Path, rel: str, findings: list[Finding]) -> str | None:
    try:
        return (repo_root / rel).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        findings.append(_finding(rel, 1, rel, f"cannot read the file ({type(exc).__name__}: {exc})"))
        return None


def _files_under(directory: str, visible: Collection[str], suffix: str) -> list[str]:
    prefix = posixpath.normpath(directory) + "/"
    return sorted(rel for rel in visible if rel.startswith(prefix) and rel.endswith(suffix))


def _check_rule(rel: str, text: str, visible: Collection[str], findings: list[Finding]) -> tuple[str, ...] | None:
    """The rule's ``paths`` when they are valid, after reporting each problem."""
    meta = _frontmatter(rel, text, findings)
    if meta is None:
        return None
    if "applyTo" in meta.keys:
        findings.append(
            _finding(rel, meta.line("applyTo"), "applyTo", "frontmatter key 'applyTo' is not allowed; use 'paths'")
        )
    paths = meta.data.get("paths")
    if not (isinstance(paths, list) and paths and all(isinstance(p, str) and p.strip() for p in paths)):
        findings.append(
            _finding(
                rel, meta.line("paths"), "paths", "frontmatter 'paths' must be a non-empty list of non-blank strings"
            )
        )
        return None
    for index, glob in enumerate(paths):
        if not _matches_a_file(glob, visible):
            findings.append(
                _finding(rel, meta.item_line("paths", index), glob, f"paths glob {glob!r} matches no git-visible file")
            )
    return tuple(paths)


def _check_skills(repo_root: Path, skills_dir: str, visible: Collection[str], findings: list[Finding]) -> None:
    prefix = posixpath.normpath(skills_dir) + "/"
    below = [rel[len(prefix) :] for rel in visible if rel.startswith(prefix)]
    skills = {rel.split("/", 1)[0] for rel in below if rel.endswith("/SKILL.md")}
    for directory in sorted({rel.split("/", 1)[0] for rel in below if "/" in rel} - skills):
        findings.append(
            _finding(f"{prefix}{directory}", 1, directory, f"skill directory {directory!r} has no SKILL.md")
        )
    first_by_name: dict[str, str] = {}
    for rel in _files_under(skills_dir, visible, "/SKILL.md"):
        text = _read(repo_root, rel, findings)
        meta = None if text is None else _frontmatter(rel, text, findings)
        if meta is None:
            continue
        name = meta.data.get("name")
        if not isinstance(name, str) or not name.strip():
            findings.append(_finding(rel, meta.line("name"), "name", "frontmatter 'name' must be a non-blank string"))
        else:
            directory = posixpath.basename(posixpath.dirname(rel))
            if name != directory:
                findings.append(
                    _finding(
                        rel, meta.line("name"), name, f"skill name {name!r} does not match its directory {directory!r}"
                    )
                )
            if name in first_by_name:
                findings.append(
                    _finding(
                        rel, meta.line("name"), name, f"duplicate skill name {name!r} (also in {first_by_name[name]})"
                    )
                )
            else:
                first_by_name[name] = rel
        description = meta.data.get("description")
        if not isinstance(description, str) or not description.strip():
            findings.append(
                _finding(
                    rel, meta.line("description"), "description", "frontmatter 'description' must be a non-blank string"
                )
            )


def _is_command_or_url(server: object) -> bool:
    return isinstance(server, dict) and any(
        isinstance(server.get(key), str) and server[key].strip() for key in ("command", "url")
    )


def _check_mcp_config(repo_root: Path, findings: list[Finding]) -> None:
    text = _read(repo_root, MCP_CONFIG, findings)
    if text is None:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        findings.append(_finding(MCP_CONFIG, exc.lineno, MCP_CONFIG, f"invalid JSON: {exc.msg}"))
        return
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        findings.append(
            _finding(MCP_CONFIG, 1, "mcpServers", "the file must be a JSON object with an 'mcpServers' object")
        )
        return
    lines = text.splitlines()
    for name, server in servers.items():
        line = next((i for i, row in enumerate(lines, 1) if json.dumps(name) in row), 1)
        if not _is_command_or_url(server):
            findings.append(
                _finding(
                    MCP_CONFIG, line, name, f"MCP server {name!r} must be an object with a string 'command' or 'url'"
                )
            )


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
    rules_dir: str,
    rule_paths: dict[str, tuple[str, ...] | None],
    findings: list[Finding],
) -> None:
    rel = instruction_map.file
    text = _read(repo_root, rel, findings)
    if text is None:
        return
    facts, problem = doc_facts_from_text(rel, text)
    if facts is None:
        findings.append(_finding(rel, 1, rel, problem.message if problem is not None else "cannot parse the file"))
        return
    heading = next((h for h in facts.headings if h.text == instruction_map.heading), None)
    if heading is None:
        findings.append(_finding(rel, 1, instruction_map.heading, f"no heading {instruction_map.heading!r}"))
        return
    end = next((h.line for h in facts.headings if h.line > heading.line and h.level <= heading.level), None)
    body = text.splitlines()[heading.line : (end - 1) if end is not None else None]
    seen: set[str] = set()
    for line, label, globs in _map_entries(body, heading.line + 1):
        rule = f"{rules_dir}/{label}.md"
        if label in seen:
            findings.append(_finding(rel, line, label, f"instruction map lists {label!r} more than once"))
        seen.add(label)
        if label not in rule_paths:
            findings.append(_finding(rel, line, label, f"instruction map lists {label!r} but {rule} does not exist"))
            continue
        paths = rule_paths[label]
        if paths is not None and globs != set(paths):
            only_map = ", ".join(sorted(globs - set(paths))) or "none"
            only_rule = ", ".join(sorted(set(paths) - globs)) or "none"
            findings.append(
                _finding(
                    rel,
                    line,
                    label,
                    f"instruction map globs for {label!r} differ from the paths in {rule} "
                    f"(only in the map: {only_map}; only in the rule: {only_rule})",
                )
            )
    for label in sorted(set(rule_paths) - seen):
        findings.append(_finding(rel, heading.line, label, f"instruction map has no entry for {rules_dir}/{label}.md"))


def check(repo_root: Path, agents: AgentsConfig, visible: Collection[str]) -> list[Finding]:
    """Every finding for ``agents`` in a checkout whose git-visible files are ``visible``."""
    findings: list[Finding] = []
    rules_dir = posixpath.normpath(agents.rules_dir)
    rule_paths: dict[str, tuple[str, ...] | None] = {}
    for rel in _files_under(rules_dir, visible, ".md"):
        text = _read(repo_root, rel, findings)
        stem = rel[len(rules_dir) + 1 : -len(".md")]
        rule_paths[stem] = None if text is None else _check_rule(rel, text, visible, findings)
    _check_skills(repo_root, agents.skills_dir, visible, findings)
    if MCP_CONFIG in visible:
        _check_mcp_config(repo_root, findings)
    if agents.instruction_map is not None:
        _check_map(repo_root, agents.instruction_map, rules_dir, rule_paths, findings)
    return sorted(findings, key=lambda f: (f.file, f.line, f.kind, f.text))
