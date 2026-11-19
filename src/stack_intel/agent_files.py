"""Checks of the agent instruction files Claude Code and GitHub Copilot load.

The conventional locations at the repository root are found and checked on
every run, with or without an ``[agents]`` table, which only adds skill
roots, the ``strict`` policy and the instruction map. A default finding
states a consequence for a named reader (Claude Code, Copilot CLI or VS
Code) that is known for that reader; behavior that is not known is never a
finding. Frontmatter is read as each reader reads it (``agent_frontmatter``):

- ``.claude/rules/**/*.md`` (Claude Code; Copilot CLI), through a symlinked
  ``.claude/rules`` inside the repository: frontmatter Claude Code cannot
  read even after its retry, a ``---`` inside a value that ends its
  frontmatter before ``paths``, or a ``paths`` with no string glob left, each
  of which makes it load the rule for every file; a ``paths`` glob that
  matches no git-visible file under any reading (``agent_globs``), including
  a ``[...]`` value the retry turned into one string.
- ``.claude/agents/**/*.md`` (Claude Code; Copilot CLI): with a ``name``,
  frontmatter that Claude Code can read, a non-blank ``description``, a name
  of at most 256 characters, not starting with ``-``, without ``:``, unique
  among the subagents Claude Code loads.
- skills: ``agent_skills``.
- ``.github/instructions/**/*.instructions.md`` (Copilot): when the
  frontmatter parses, ``applyTo`` globs that match a file and an
  ``excludeAgent`` of ``code-review``, ``cloud-agent`` or ``coding-agent``.
  Copilot CLI recovers its keys from YAML that does not parse, so a parse
  failure is not reported.
- ``.github/agents/**/*.md`` (Copilot): a base name of ``.``, ``-``, ``_``,
  ASCII letters and digits; parsed frontmatter has a non-blank
  ``description`` and a ``target`` of ``vscode`` or ``github-copilot``.
- MCP configs and hooks: ``agent_json``. ``@path`` imports: ``agent_imports``.
- ``strict``: every rule has frontmatter with ``paths`` of the documented
  shape and no ``applyTo``; ``applyTo`` has the documented shape; the Agent
  Skills rules apply to every skill root; every directory under a skill root
  holds a ``SKILL.md``; frontmatter that is not clean YAML and repeated keys
  are reported.
- The ``instruction_map`` section's ``- `label`: `glob`, ...`` lines name
  exactly the root ``.claude/rules`` files' stems, each with that file's ``paths``.

A glob that matches no git-visible path is still not reported when a reader
may see files git does not list (``_Checkout.unproven``).

Nested ``.claude/`` directories (a sub-project launched on its own) are not read.
A finding is a ``doc_report.Finding`` (``path:line: message`` when printed),
so the docs check reports it with the docs findings.
"""

from __future__ import annotations

import functools
import posixpath
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from stack_intel import agent_imports, agent_json, agent_skills
from stack_intel.agent_frontmatter import (
    Frontmatter,
    FrontmatterRead,
    finding,
    is_blank,
    parse_frontmatter,
    read_text,
    strict_parse_findings,
)
from stack_intel.agent_globs import (
    GlobGroup,
    glob_groups,
    glob_verdict,
    literal_directory,
    sample_paths,
    split_globs,
)
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
_EXCLUDE_AGENTS = ("code-review", "cloud-agent", "coding-agent")
_TARGETS = ("vscode", "github-copilot")
_MAX_SUBAGENT_NAME = 256


@dataclass(frozen=True)
class AgentReport:
    findings: list[Finding]
    files: tuple[str, ...]  # every agent file read


@dataclass(frozen=True)
class _Checkout:
    repo_root: Path
    files: RepoFiles

    @functools.cached_property
    def paths(self) -> frozenset[str]:
        """Git-visible files and directories: a glob naming a directory matches."""
        return self.files.files | self.files.dirs

    @functools.cached_property
    def tracked_dirs(self) -> frozenset[str]:
        return frozenset(parent for rel in self.files.tracked for parent in _parents(rel))

    def unproven(self, glob: str) -> bool:
        """Whether a glob that matches no git-visible path may still match
        files the reader sees: its literal directory is or lies under a
        symlink, exists on disk without being tracked, or git ignores the
        directory or paths the glob would match."""
        directory = literal_directory(glob)
        if directory:
            parts = directory.split("/")
            if any((self.repo_root / "/".join(parts[:end])).is_symlink() for end in range(1, len(parts) + 1)):
                return True
            if (self.repo_root / directory).exists() and directory not in self.tracked_dirs:
                return True
        asked = sample_paths(glob) + ([directory + "/"] if directory else [])
        return bool(self.files.ignored(asked))


def _parents(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]


def _files_under(directory: str, visible: Collection[str], suffix: str) -> list[str]:
    prefix = directory + "/"
    return sorted(rel for rel in visible if rel.startswith(prefix) and rel.endswith(suffix))


def _glob_items(meta: Frontmatter, key: str) -> list[tuple[GlobGroup, int]]:
    """``key``'s ``glob_groups``, each with its line."""
    value = meta.data.get(key)
    if isinstance(value, list):
        return [
            (group, meta.item_line(key, index)) for index, item in enumerate(value) for group in glob_groups([item])
        ]
    return [(group, meta.line(key)) for group in glob_groups(value)]


def _is_glob_value(value: object) -> bool:
    """A non-blank string or a list of non-blank strings: the documented shape."""
    if isinstance(value, list):
        return all(not is_blank(item) for item in value)
    return not is_blank(value)


def _any_piece_may_match(pieces: Iterable[tuple[str, bool]], checkout: _Checkout) -> bool:
    return any(
        unmodelled or glob_verdict(glob, checkout.paths) != "no_match" or checkout.unproven(glob)
        for glob, unmodelled in pieces
    )


def _check_globs(
    rel: str, key: str, globs: list[tuple[GlobGroup, int]], checkout: _Checkout, findings: list[Finding]
) -> None:
    for (text, pieces), line in globs:
        if not _any_piece_may_match(pieces, checkout):
            findings.append(finding(rel, line, text, f"{key} glob {text!r} matches no git-visible file"))


# ── .claude/rules ────────────────────────────────────────────────────────


def _check_rule(
    rel: str, text: str, checkout: _Checkout, strict: bool, findings: list[Finding]
) -> tuple[str, ...] | None:
    """The rule's globs for the instruction map: None when they cannot be read."""
    read = parse_frontmatter(text, claude=True)
    if read.error is not None:
        message = f"{read.error.message}; Claude Code ignores the frontmatter and loads the rule for every file"
        findings.append(finding(rel, read.error.line, "frontmatter", message))
        return None
    if not read.present:
        if strict:
            message = "missing YAML frontmatter: the file must open with a --- line (strict)"
            findings.append(finding(rel, 1, "frontmatter", message))
            return None
        return ()
    if strict:
        findings.extend(strict_parse_findings(rel, read))
    meta = read.meta
    if meta is None:
        return None
    if _paths_cut_off(rel, read, meta, text, findings) or _paths_cut_inside(rel, read, meta, checkout, findings):
        return None
    if strict:
        findings.extend(meta.repeat_findings(rel))
        if "applyTo" in meta.keys:
            message = "frontmatter key 'applyTo' is not allowed; use 'paths' (strict)"
            findings.append(finding(rel, meta.line("applyTo"), "applyTo", message))
    value = meta.data.get("paths")
    if "paths" in read.as_text and isinstance(value, str):
        return _check_paths_as_text(rel, read, meta, value, checkout, findings)
    globs = _glob_items(meta, "paths")
    if not _is_placeholder(value) and not globs:
        message = "Claude Code finds no glob in 'paths' and loads the rule for every file"
        findings.append(finding(rel, meta.line("paths"), "paths", message))
        return None
    if strict:
        if value is not None and value != [] and not _is_glob_value(value):
            message = "frontmatter 'paths' must be a non-blank string or a list of non-blank strings (strict)"
            findings.append(finding(rel, meta.line("paths"), "paths", message))
            return None
        if not globs:
            findings.append(finding(rel, meta.line("paths"), "paths", "frontmatter 'paths' must name a glob (strict)"))
            return None
    _check_globs(rel, "paths", globs, checkout, findings)
    return tuple(glob for (_, pieces), _ in globs for glob, _ in pieces)


def _is_placeholder(value: object) -> bool:
    """``paths:``, ``paths: ""``, ``paths: []`` or ``paths: [""]``: left empty, a strict finding only."""
    if isinstance(value, list):
        return all(isinstance(item, str) and not item.strip() for item in value)
    return value is None or (isinstance(value, str) and not value.strip())


def _paths_cut_inside(
    rel: str, read: FrontmatterRead, meta: Frontmatter, checkout: _Checkout, findings: list[Finding]
) -> bool:
    """A ``---`` inside the ``paths`` value cuts it short: report it when what
    is left names no glob that may match."""
    if read.cut is None or read.cut[1] != "paths":
        return False
    globs = _glob_items(meta, "paths")
    if any(_any_piece_may_match(pieces, checkout) for (_, pieces), _ in globs):
        return False
    if globs:
        names = ", ".join(repr(text) for (text, _), _ in globs)
        left = f"{names}, which matches no git-visible file"
    else:
        left = "no glob, so it loads the rule for every file"
    message = f"Claude Code ends the frontmatter at the '---' inside 'paths', which leaves {left}; quote the value or avoid '---' in it"
    findings.append(finding(rel, read.cut[0], "paths", message))
    return True


def _paths_cut_off(rel: str, read: FrontmatterRead, meta: Frontmatter, text: str, findings: list[Finding]) -> bool:
    """A ``---`` inside a value ends Claude Code's frontmatter there; report
    it when that drops a ``paths`` the full frontmatter has."""
    if read.cut is None or "paths" in meta.keys:
        return False
    full = parse_frontmatter(text, claude=False, retry=True).meta
    if full is None or "paths" not in full.keys:
        return False
    line, key = read.cut
    where = f"inside {key!r}" if key else "inside a value"
    message = (
        f"Claude Code ends the frontmatter at the '---' {where}, drops 'paths' after it and loads the rule "
        "for every file; quote the value or avoid '---' in it"
    )
    findings.append(finding(rel, line, key or "frontmatter", message))
    return True


def _check_paths_as_text(
    rel: str,
    read: FrontmatterRead,
    meta: Frontmatter,
    value: str,
    checkout: _Checkout,
    findings: list[Finding],
) -> tuple[str, ...] | None:
    """``paths: [...]`` that is not a list on its own, which Claude Code's
    retry quoted into one string: reported only when no piece of it matches."""
    pieces = {glob: False for glob in split_globs(value)} | {
        piece.strip(): False for piece in value.split(",") if piece.strip()
    }
    if _any_piece_may_match(list(pieces.items()), checkout):
        return tuple(split_globs(value))
    culprit = read.culprit
    if culprit is not None and culprit.key != "paths":
        cause = (
            f"the unquoted {culprit.reason} in {culprit.key!r} (line {culprit.line}) makes the frontmatter "
            "invalid YAML, and Claude Code's recovery then quotes 'paths' as text. "
            f"Quote the {culprit.key} (\"{culprit.value}\") and write 'paths' as a valid list."
        )
    else:
        cause = (
            "'paths' is not a valid YAML list, so Claude Code's recovery quotes it as text. "
            "Write 'paths' as a valid list."
        )
    message = f"Claude Code reads 'paths' as the single string {value!r}, which matches no git-visible file: {cause}"
    findings.append(finding(rel, meta.line("paths"), "paths", message))
    return None


# ── .claude/agents ───────────────────────────────────────────────────────


def _check_subagent(rel: str, text: str, first_by_name: dict[str, str], strict: bool, findings: list[Finding]) -> None:
    read = parse_frontmatter(text, claude=True)
    if read.error is not None:
        # Without a `name:` line the file is documentation, which Claude Code never loads as a subagent.
        if read.has_name:
            message = f"{read.error.message}; Claude Code skips the subagent"
            findings.append(finding(rel, read.error.line, "frontmatter", message))
        return
    meta = read.meta
    name = None if meta is None else meta.data.get("name")
    if strict and (read.has_name or isinstance(name, str)):
        findings.extend(strict_parse_findings(rel, read))
    if meta is None or not isinstance(name, str):
        return
    if strict:
        findings.extend(meta.repeat_findings(rel))
    line = meta.line("name")
    loads = True
    description = meta.data.get("description")
    if description is None or (isinstance(description, str) and not description.strip()):
        loads = False
        findings.append(
            finding(rel, meta.line("description"), name, f"subagent {name!r} has no description; Claude Code skips it")
        )
    if len(name) > _MAX_SUBAGENT_NAME:
        loads = False
        message = f"subagent name {name[:40]!r}... is longer than {_MAX_SUBAGENT_NAME} characters (Claude Code)"
        findings.append(finding(rel, line, name, message))
    if name.startswith("-"):
        loads = False
        findings.append(finding(rel, line, name, f"subagent name {name!r} must not start with '-' (Claude Code)"))
    if ":" in name:
        loads = False
        findings.append(finding(rel, line, name, f"subagent name {name!r} must not contain ':' (Claude Code)"))
    if not loads:
        return
    if name in first_by_name:
        message = f"subagent name {name!r} is also used by {first_by_name[name]}; Claude Code loads only one of them"
        findings.append(finding(rel, line, name, message))
    else:
        first_by_name[name] = rel


# ── .github/instructions and .github/agents ──────────────────────────────


def _check_instructions(rel: str, text: str, checkout: _Checkout, strict: bool, findings: list[Finding]) -> None:
    meta = parse_frontmatter(text, claude=False).meta
    if meta is None:
        return
    if strict:
        findings.extend(meta.repeat_findings(rel))
        value = meta.data.get("applyTo")
        if value is not None and value != "" and value != [] and not _is_glob_value(value):
            message = "frontmatter 'applyTo' must be a string or a list of non-blank strings (strict)"
            findings.append(finding(rel, meta.line("applyTo"), "applyTo", message))
    _check_globs(rel, "applyTo", _glob_items(meta, "applyTo"), checkout, findings)
    exclude = meta.data.get("excludeAgent")
    if isinstance(exclude, str) and exclude not in _EXCLUDE_AGENTS:
        message = f"excludeAgent {exclude!r} must be 'code-review', 'cloud-agent' or 'coding-agent' (Copilot)"
        findings.append(finding(rel, meta.line("excludeAgent"), exclude, message))


def _check_copilot_agent(rel: str, text: str, findings: list[Finding]) -> None:
    filename = posixpath.basename(rel)
    base = filename.removesuffix(".md").removesuffix(".agent")
    if not _AGENT_FILE_NAME.fullmatch(base):
        message = f"custom agent file name {filename!r} may use only ASCII letters, digits, '.', '-' and '_' (Copilot)"
        findings.append(finding(rel, 1, filename, message))
    meta = parse_frontmatter(text, claude=False).meta
    if meta is None:
        return
    if is_blank(meta.data.get("description")):
        message = f"custom agent {base!r} has no description; Copilot requires one"
        findings.append(finding(rel, meta.line("description"), "description", message))
    target = meta.data.get("target")
    if target is not None and target not in _TARGETS:
        message = f"custom agent target {target!r} must be 'vscode' or 'github-copilot'"
        findings.append(finding(rel, meta.line("target"), str(target), message))


def _rule_files(checkout: _Checkout) -> list[str]:
    """The ``.claude/rules`` Markdown files, through a symlinked directory
    inside the repository as Claude Code follows it (git lists only the link)."""
    link = checkout.repo_root / RULES_DIR
    if not link.is_symlink():
        return _files_under(RULES_DIR, checkout.files.files, ".md")
    try:
        target = link.resolve().relative_to(checkout.repo_root.resolve()).as_posix()
    except OSError, ValueError:
        return []
    return sorted(RULES_DIR + rel[len(target) :] for rel in _files_under(target, checkout.files.files, ".md"))


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
    checkout = _Checkout(repo_root, files)
    findings: list[Finding] = []
    read: list[str] = []

    rule_paths: dict[str, tuple[str, ...] | None] = {}
    for rel in _rule_files(checkout):
        read.append(rel)
        text = read_text(repo_root, rel, findings)
        stem = rel[len(RULES_DIR) + 1 : -len(".md")]
        rule_paths[stem] = None if text is None else _check_rule(rel, text, checkout, agents.strict, findings)

    first_by_name: dict[str, str] = {}
    for rel in _files_under(AGENTS_DIR, visible, ".md"):
        read.append(rel)
        if (text := read_text(repo_root, rel, findings)) is not None:
            _check_subagent(rel, text, first_by_name, agents.strict, findings)

    strict = agents.strict
    read += agent_skills.check_root(
        repo_root, agent_skills.CLAUDE_SKILLS, visible, findings, claude=True, spec=strict, strict=strict
    )
    for root in agent_skills.SPEC_ROOTS:
        read += agent_skills.check_root(repo_root, root, visible, findings, claude=False, spec=strict, strict=strict)
    for root in agents.skill_dirs:
        read += agent_skills.check_root(repo_root, root, visible, findings, claude=False, spec=True, strict=strict)

    for rel in _files_under(INSTRUCTIONS_DIR, visible, INSTRUCTIONS_SUFFIX):
        read.append(rel)
        if (text := read_text(repo_root, rel, findings)) is not None:
            _check_instructions(rel, text, checkout, agents.strict, findings)
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
