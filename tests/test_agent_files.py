"""The agent-file checks: discovery at every Claude Code and Copilot location,
rules, subagents, Copilot instructions and agents, ``strict`` and the
instruction map, each failure with its exact ``path:line: message`` line."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fake_repo_files import FakeRepoFiles, repo_files_on_disk
from mcp_fixtures import FakeAnalyzers, config_toml, make_repo, make_runtime, write, write_config

from stack_intel import agent_files
from stack_intel.config import AgentsConfig, InstructionMap

MAP_FILE = "AGENTS.md"
INSTRUCTION_MAP = InstructionMap(MAP_FILE, "Scoped instruction map")
DEFAULT = AgentsConfig()
MAPPED = AgentsConfig(instruction_map=INSTRUCTION_MAP)
STRICT = AgentsConfig(strict=True, instruction_map=INSTRUCTION_MAP)

API_RULE = """\
---
paths:
  - "src/api/**"
---

# API
"""

UI_RULE = """\
---
paths:
  - "src/ui/**"
  - "src/ui-tests/*.{ts,tsx}"
---

# UI
"""

SKILL = """\
---
name: deploy
description: Ship a release.
---

# Deploy
"""

MAP = """\
# Agents

## Working rules

### Scoped instruction map

Rules load when a matching file is read.

- `api`: `src/api/**`
- `ui`: `src/ui-tests/*.{ts,tsx}`, `src/ui/**`

### Next section

- `ghost`: `nowhere/**`
"""


def layout(root: Path) -> Path:
    write(root / "src" / "api" / "handler.py", "x = 1\n")
    write(root / "src" / "ui" / "view.tsx", "export {};\n")
    write(root / "src" / "ui-tests" / "view.test.tsx", "export {};\n")
    write(root / ".claude" / "rules" / "api.md", API_RULE)
    write(root / ".claude" / "rules" / "ui.md", UI_RULE)
    write(root / ".claude" / "skills" / "deploy" / "SKILL.md", SKILL)
    write(root / MAP_FILE, MAP)
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return layout(tmp_path / "repo")


def lines(root: Path, agents: AgentsConfig = STRICT) -> list[str]:
    report = agent_files.check(root, agents, repo_files_on_disk(root))
    return [f"{f.file}:{f.line}: {f.message}" for f in report.findings]


def put(root: Path, rel: str, text: str) -> None:
    write(root / rel, text)


def test_a_consistent_set_of_agent_files_has_no_findings(repo: Path) -> None:
    assert lines(repo) == []
    assert lines(repo, DEFAULT) == []


def test_a_checkout_without_agent_files_has_no_findings_and_no_files(tmp_path: Path) -> None:
    write(tmp_path / "x.txt", "x")
    report = agent_files.check(tmp_path, DEFAULT, repo_files_on_disk(tmp_path))
    assert (report.findings, report.files) == ([], ())


# ── Discovery ────────────────────────────────────────────────────────────


def test_every_conventional_location_is_found_without_configuration(tmp_path: Path) -> None:
    files = {
        ".claude/rules/a.md": "# A\n",
        ".claude/rules/web/b.md": "# B\n",
        ".claude/agents/review.md": "---\nname: review\ndescription: Reviews.\n---\n",
        ".claude/skills/one/SKILL.md": "# One\n",
        ".agents/skills/two/SKILL.md": "---\nname: two\ndescription: Two.\n---\n",
        ".github/skills/three/SKILL.md": "---\nname: three\ndescription: Three.\n---\n",
        ".github/instructions/py.instructions.md": "# Py\n",
        ".github/agents/helper.agent.md": "---\ndescription: Helps.\n---\n",
        ".github/hooks/hooks.json": '{"version": 1, "hooks": {}}',
        ".mcp.json": '{"mcpServers": {}}',
        ".github/mcp.json": '{"mcpServers": {}}',
        ".vscode/mcp.json": '{"servers": {}}',
        "CLAUDE.md": "@AGENTS.md\n",
        "AGENTS.md": "# Agents\n",
        ".claude/CLAUDE.md": "# Claude\n",
        ".github/copilot-instructions.md": "# Copilot\n",
        "web/AGENTS.md": "# Web\n",
    }
    for rel, text in files.items():
        put(tmp_path, rel, text)
    report = agent_files.check(tmp_path, DEFAULT, repo_files_on_disk(tmp_path))
    assert report.findings == []
    assert report.files == tuple(sorted(files))


def test_other_files_at_the_locations_are_not_agent_files(tmp_path: Path) -> None:
    put(tmp_path, ".github/instructions/notes.md", "---\napplyTo: 3\n---\n")
    put(tmp_path, ".github/hooks/nested/x.json", "{")
    put(tmp_path, ".claude/skills/deploy/references/SKILL.md", "---\nname: Not A Skill\n---\n")
    put(tmp_path, ".agents/skills/deploy/references/SKILL.md", "no frontmatter\n")
    put(tmp_path, ".claude/rules/notes.txt", "x")
    put(tmp_path, ".github/prompts/x.prompt.md", "---\nagent: [\n---\n")
    put(tmp_path, ".claude/commands/x.md", "---\nname: [\n---\n")
    put(tmp_path, ".claude/settings.json", "{")
    put(tmp_path, ".github/copilot/settings.json", "{")
    report = agent_files.check(tmp_path, DEFAULT, repo_files_on_disk(tmp_path))
    assert (report.findings, report.files) == ([], ())


def test_nested_claude_directories_are_not_read(tmp_path: Path) -> None:
    put(tmp_path, "sub/.claude/rules/a.md", "---\npaths: [\n---\n")
    put(tmp_path, "sub/.claude/agents/a.md", "---\nname: a\n---\n")
    put(tmp_path, "sub/.mcp.json", "{")
    assert lines(tmp_path, DEFAULT) == []


def test_a_file_git_does_not_see_is_not_checked(repo: Path) -> None:
    put(repo, ".mcp.json", "{")
    files = repo_files_on_disk(repo)
    visible = files.__class__(
        repo_root=repo, files=files.files - {".mcp.json"}, tracked=files.tracked - {".mcp.json"}, dirs=files.dirs
    )
    assert agent_files.check(repo, STRICT, visible).findings == []


# ── Rules: default checks ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\npaths:\n  - src/api/**\n",
            ".claude/rules/api.md:1: YAML frontmatter is not closed by a --- line; "
            "Claude Code ignores it and loads the rule for every file",
        ),
        (
            "---\n- a\n---\n",
            ".claude/rules/api.md:1: YAML frontmatter must be a mapping; "
            "Claude Code ignores it and loads the rule for every file",
        ),
        (
            "---\npaths: [src/api/**\n---\n",
            ".claude/rules/api.md:2: invalid YAML frontmatter: expected ',' or ']', but got '<stream end>'; "
            "Claude Code ignores it and loads the rule for every file",
        ),
        (
            '---\npaths: ""\n---\n',
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings",
        ),
        (
            '---\npaths:\n  - "src/api/**"\n  - " "\n---\n',
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings",
        ),
        (
            "---\npaths:\n  - 3\n---\n",
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings",
        ),
        (
            "---\npaths: {a: b}\n---\n",
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings",
        ),
        (
            '---\npaths:\n  - "src/api/**"\npaths:\n  - "src/ui/**"\n---\n',
            ".claude/rules/api.md:4: frontmatter key 'paths' is repeated (first at line 2)",
        ),
        (
            '---\npaths:\n  - "src/api/**"\n  - "src/renamed/**"\n---\n',
            ".claude/rules/api.md:4: paths glob 'src/renamed/**' matches no git-visible file",
        ),
        (
            '---\npaths: "src/api/**, src/renamed/**"\n---\n',
            ".claude/rules/api.md:2: paths glob 'src/renamed/**' matches no git-visible file",
        ),
        (
            '---\npaths:\n  - "src/api/[id"\n---\n',
            ".claude/rules/api.md:3: paths glob 'src/api/[id' has an invalid bracket expression; "
            "Claude Code matches no file with it",
        ),
    ],
    ids=[
        "unclosed",
        "not-a-mapping",
        "invalid-yaml",
        "blank-string",
        "blank-item",
        "non-string-item",
        "mapping",
        "repeated-key",
        "no-match-item",
        "no-match-comma-string",
        "unclosed-bracket",
    ],
)
def test_a_broken_rule_is_reported_by_default(repo: Path, text: str, expected: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    assert lines(repo, DEFAULT) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "# API, loaded for every file\n",
        "---\ndescription: x\n---\n",
        "---\n---\n",
        "---\npaths:\n---\n",
        "---\npaths: []\n---\n",
        '---\npaths: "src/api/**, src/ui/**"\n---\n',
        '---\napplyTo: "src/**"\npaths:\n  - "src/api/**"\n---\n',
        '---\napplyTo: "src/**"\n---\n',
        "---\npaths: **/*.py\n---\n",
        "---\npaths:\n  - *.tsx\n---\n",
        '---\npaths: ["*.py"]\nunknown: 1\nalwaysApply: true\n---\n',
        '---\npaths: ["!src/**", "src/@(a|b)/**", "src/\\\\[x\\\\]", "src/{1..3}/**"]\n---\n',
        '---\npaths: "src/**/*.{ts,js}, src/api/**"\n---\n',
        '---\npaths: ["/src/api/handler.py", "view.tsx", "src/ui/[vw]iew.tsx"]\n---\n',
    ],
    ids=[
        "no-frontmatter",
        "no-paths",
        "empty-frontmatter",
        "null-paths",
        "empty-list",
        "comma-string",
        "apply-to-beside-paths",
        "apply-to-alone",
        "unquoted-glob",
        "unquoted-glob-item",
        "unknown-keys",
        "unmodelled-globs",
        "braces-in-a-comma-string",
        "anchored-any-depth-and-class",
    ],
)
def test_a_rule_the_readers_accept_is_not_reported_by_default(repo: Path, text: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    assert lines(repo, DEFAULT) == []


def test_a_glob_is_matched_against_git_visible_files_only(repo: Path) -> None:
    (repo / "src/api/handler.py").unlink()
    (repo / "src/api").rmdir()
    put(repo, "local/api/handler.py", "x = 1\n")
    put(repo, ".claude/rules/api.md", '---\npaths:\n  - "src/api/**"\n---\n')
    files = repo_files_on_disk(repo)
    hidden = replace(
        files, files=files.files - {"local/api/handler.py"}, tracked=files.tracked - {"local/api/handler.py"}
    )
    found = [f"{f.file}:{f.line}: {f.message}" for f in agent_files.check(repo, STRICT, hidden).findings]
    assert found == [".claude/rules/api.md:3: paths glob 'src/api/**' matches no git-visible file"]


# ── Rules: strict ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# API\n", ".claude/rules/api.md:1: missing YAML frontmatter: the file must open with a --- line (strict)"),
        ("---\ndescription: x\n---\n", ".claude/rules/api.md:1: frontmatter 'paths' must name a glob (strict)"),
        ("---\n---\n", ".claude/rules/api.md:1: frontmatter 'paths' must name a glob (strict)"),
        ("---\npaths: []\n---\n", ".claude/rules/api.md:2: frontmatter 'paths' must name a glob (strict)"),
        (
            '---\napplyTo: "src/**"\npaths:\n  - "src/api/**"\n---\n',
            ".claude/rules/api.md:2: frontmatter key 'applyTo' is not allowed; use 'paths' (strict)",
        ),
    ],
    ids=["no-frontmatter", "no-paths", "empty-frontmatter", "empty-paths", "apply-to"],
)
def test_strict_requires_scoped_rules(repo: Path, text: str, expected: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    assert lines(repo, AgentsConfig(strict=True)) == [expected]


def test_strict_accepts_a_comma_string(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\npaths: "src/api/**"\n---\n')
    assert lines(repo) == []


# ── Subagents ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\nname: review\n---\n",
            ".claude/agents/review.md:1: subagent 'review' has no description; Claude Code skips it",
        ),
        (
            "---\nname: review\ndescription: ' '\n---\n",
            ".claude/agents/review.md:3: subagent 'review' has no description; Claude Code skips it",
        ),
        (
            "---\nname: -review\ndescription: x\n---\n",
            ".claude/agents/review.md:2: subagent name '-review' must not start with '-' (Claude Code)",
        ),
        (
            "---\nname: team:review\ndescription: x\n---\n",
            ".claude/agents/review.md:2: subagent name 'team:review' must not contain ':' (Claude Code)",
        ),
        (
            f"---\nname: {'r' * 257}\ndescription: x\n---\n",
            f".claude/agents/review.md:2: subagent name {'r' * 40!r}... is longer than 256 characters (Claude Code)",
        ),
        (
            "---\nname: [review\n---\n",
            ".claude/agents/review.md:2: invalid YAML frontmatter: expected ',' or ']', but got '<stream end>'; "
            "Claude Code skips the subagent",
        ),
        (
            "---\nname: review\n",
            ".claude/agents/review.md:1: YAML frontmatter is not closed by a --- line; Claude Code skips the subagent",
        ),
    ],
    ids=["no-description", "blank-description", "leading-dash", "colon", "too-long", "invalid-yaml", "unclosed"],
)
def test_a_broken_subagent_is_reported(tmp_path: Path, text: str, expected: str) -> None:
    put(tmp_path, ".claude/agents/review.md", text)
    assert lines(tmp_path, DEFAULT) == [expected]


def test_a_subagent_name_used_twice_is_reported_at_the_second(tmp_path: Path) -> None:
    put(tmp_path, ".claude/agents/a/review.md", "---\nname: review\ndescription: x\n---\n")
    put(tmp_path, ".claude/agents/review.md", "---\nname: review\ndescription: y\n---\n")
    assert lines(tmp_path, DEFAULT) == [
        ".claude/agents/review.md:2: subagent name 'review' is also used by .claude/agents/a/review.md; "
        "Claude Code loads only one of them"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "# Agents in this folder\n",
        "---\ndescription: no name, so documentation\n---\n",
        "---\nname: r\ndescription: x\ntools: Read, Grep\nmodel: sonnet\ncustomKey: 1\n---\n",
        f"---\nname: {'r' * 256}\ndescription: x\n---\n",
    ],
    ids=["readme", "no-name", "unknown-keys", "256-characters"],
)
def test_a_valid_subagent_or_a_readme_is_not_reported(tmp_path: Path, text: str) -> None:
    put(tmp_path, ".claude/agents/sub/dir/review.md", text)
    assert lines(tmp_path, DEFAULT) == []


# ── Copilot instructions ─────────────────────────────────────────────────

INSTRUCTIONS = ".github/instructions/py.instructions.md"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "---\napplyTo: 3\n---\n",
            f"{INSTRUCTIONS}:2: frontmatter 'applyTo' must be a string or a list of non-blank strings",
        ),
        (
            '---\napplyTo: ["src/**", ""]\n---\n',
            f"{INSTRUCTIONS}:2: frontmatter 'applyTo' must be a string or a list of non-blank strings",
        ),
        (
            '---\napplyTo: "src/**, lib/**"\n---\n',
            f"{INSTRUCTIONS}:2: applyTo glob 'lib/**' matches no git-visible file",
        ),
        ('---\napplyTo:\n  - "*.py"\n---\n', f"{INSTRUCTIONS}:3: applyTo glob '*.py' matches no git-visible file"),
        (
            '---\napplyTo: "**"\nexcludeAgent: coding-agent\n---\n',
            f"{INSTRUCTIONS}:3: excludeAgent 'coding-agent' must be 'code-review' or 'cloud-agent'",
        ),
        (
            '---\napplyTo: "**"\napplyTo: "lib/**"\n---\n',
            f"{INSTRUCTIONS}:3: applyTo glob 'lib/**' matches no git-visible file",
        ),
    ],
    ids=["number", "blank-item", "comma-string", "star-is-the-root-only", "exclude-agent", "repeated-key-last-wins"],
)
def test_broken_copilot_instructions_are_reported(repo: Path, text: str, expected: str) -> None:
    put(repo, INSTRUCTIONS, text)
    assert lines(repo, DEFAULT) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "# No frontmatter\n",
        "---\ndescription: manual only\n---\n",
        "---\napplyTo: [src/**\n---\n",
        "---\napplyTo: **/*.py\n---\n",
        '---\napplyTo: ["src/**/*.py", "**/*.tsx"]\nexcludeAgent: "code-review"\nname: x\n---\n',
        '---\napplyTo: "src/**/*.{ts,tsx}, src/api/**"\n---\n',
        '---\napplyTo: ""\n---\n',
        '---\napplyTo: "  "\n---\n',
        "---\napplyTo:\nexcludeAgent:\n---\n",
        '---\nexcludeAgent: ["code-review"]\n---\n',
    ],
    ids=[
        "no-frontmatter",
        "no-apply-to",
        "invalid-yaml",
        "unquoted-glob",
        "valid",
        "braces-in-string",
        "empty",
        "blank",
        "null-keys",
        "exclude-agent-list",
    ],
)
def test_copilot_instructions_the_readers_accept_are_not_reported(repo: Path, text: str) -> None:
    put(repo, INSTRUCTIONS, text)
    put(repo, ".github/instructions/sub/dir/x.instructions.md", "# Nested\n")
    assert lines(repo, DEFAULT) == []


# ── Copilot custom agents ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "text", "expected"),
    [
        (
            ".github/agents/my agent.agent.md",
            "---\ndescription: x\n---\n",
            ".github/agents/my agent.agent.md:1: custom agent file name 'my agent.agent.md' may use only "
            "ASCII letters, digits, '.', '-' and '_' (Copilot)",
        ),
        (
            ".github/agents/helper.agent.md",
            "---\nname: helper\n---\n",
            ".github/agents/helper.agent.md:1: custom agent 'helper' has no description; Copilot requires one",
        ),
        (
            ".github/agents/helper.md",
            "---\ndescription: x\ntarget: claude\n---\n",
            ".github/agents/helper.md:3: custom agent target 'claude' must be 'vscode' or 'github-copilot'",
        ),
    ],
    ids=["file-name", "no-description", "target"],
)
def test_broken_copilot_agents_are_reported(tmp_path: Path, rel: str, text: str, expected: str) -> None:
    put(tmp_path, rel, text)
    assert lines(tmp_path, DEFAULT) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "# Agents\n",
        "---\ndescription: [x\n---\n",
        "---\ndescription: x\ntarget: vscode\ntools: [read, edit]\nunknown: 1\n---\n",
    ],
    ids=["readme", "invalid-yaml", "valid"],
)
def test_copilot_agents_the_readers_accept_are_not_reported(tmp_path: Path, text: str) -> None:
    put(tmp_path, ".github/agents/team/helper.agent.md", text)
    assert lines(tmp_path, DEFAULT) == []


# ── Instruction map ──────────────────────────────────────────────────────


def test_a_rule_without_a_map_entry_is_reported_at_the_map_heading(repo: Path) -> None:
    put(repo, ".claude/rules/extra.md", '---\npaths:\n  - "src/api/**"\n---\n')
    assert lines(repo) == ["AGENTS.md:5: instruction map has no entry for .claude/rules/extra.md"]


def test_a_map_entry_without_a_rule_is_reported_at_its_line(repo: Path) -> None:
    put(repo, MAP_FILE, MAP.replace("### Next section", "- `docs`: `docs/**`\n\n### Next section"))
    assert lines(repo) == ["AGENTS.md:12: instruction map lists 'docs' but .claude/rules/docs.md does not exist"]


def test_the_map_must_list_a_rules_paths_exactly(repo: Path) -> None:
    put(repo, MAP_FILE, MAP.replace("- `api`: `src/api/**`", "- `api`: `src/api/**`, `src/extra/**`"))
    assert lines(repo) == [
        "AGENTS.md:9: instruction map globs for 'api' differ from the paths in .claude/rules/api.md "
        "(only in the map: src/extra/**; only in the rule: none)"
    ]
    put(repo, MAP_FILE, MAP.replace("- `api`: `src/api/**`", "- `api`: `src/other/**`"))
    assert lines(repo) == [
        "AGENTS.md:9: instruction map globs for 'api' differ from the paths in .claude/rules/api.md "
        "(only in the map: src/other/**; only in the rule: src/api/**)"
    ]


def test_a_comma_string_is_compared_with_the_map_as_its_split_set(repo: Path) -> None:
    put(repo, ".claude/rules/ui.md", '---\npaths: "src/ui/**,  src/ui-tests/*.{ts,tsx}"\n---\n')
    assert lines(repo) == []


def test_the_order_of_globs_in_the_map_does_not_matter(repo: Path) -> None:
    put(repo, MAP_FILE, MAP.replace("`src/ui-tests/*.{ts,tsx}`, `src/ui/**`", "`src/ui/**`, `src/ui-tests/*.{ts,tsx}`"))
    assert lines(repo) == []


def test_a_label_listed_twice_is_reported(repo: Path) -> None:
    put(repo, MAP_FILE, MAP.replace("### Next section", "- `api`: `src/api/**`\n\n### Next section"))
    assert lines(repo) == ["AGENTS.md:12: instruction map lists 'api' more than once"]


def test_the_map_section_ends_at_the_next_heading_of_its_level_or_above(repo: Path) -> None:
    put(repo, MAP_FILE, MAP.replace("### Next section", "#### Detail\n\n- `ui2`: `src/ui/**`\n\n### Next section"))
    assert lines(repo) == ["AGENTS.md:14: instruction map lists 'ui2' but .claude/rules/ui2.md does not exist"]


def test_a_missing_map_heading_is_reported(repo: Path) -> None:
    put(repo, MAP_FILE, "# Agents\n")
    assert lines(repo, MAPPED) == ["AGENTS.md:1: no heading 'Scoped instruction map'"]


def test_a_missing_map_file_is_reported(repo: Path) -> None:
    (repo / MAP_FILE).unlink()
    found = lines(repo)
    assert len(found) == 1
    assert found[0].startswith("AGENTS.md:1: cannot read the file (FileNotFoundError")


def test_a_rule_with_unreadable_paths_is_reported_once_not_also_as_a_map_mismatch(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", "---\npaths: 3\n---\n")
    assert lines(repo) == [
        ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings"
    ]


def test_nested_rule_files_are_labelled_by_their_path_below_the_rules_directory(repo: Path) -> None:
    put(repo, ".claude/rules/web/forms.md", '---\npaths:\n  - "src/ui/**"\n---\n')
    put(repo, MAP_FILE, MAP.replace("### Next section", "- `web/forms`: `src/ui/**`\n\n### Next section"))
    assert lines(repo) == []


def test_without_an_instruction_map_only_the_files_are_checked(repo: Path) -> None:
    put(repo, MAP_FILE, "# Agents\n")
    assert lines(repo, AgentsConfig(strict=True)) == []


# ── Through the runtime ──────────────────────────────────────────────────

AGENTS_TABLE = """\
[agents]
strict = true
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
"""


def test_the_runtime_reads_the_agents_table_and_lists_the_findings(tmp_path: Path) -> None:
    root = layout(make_repo(tmp_path / "repo"))
    write_config(root, config_toml() + AGENTS_TABLE)
    rt = make_runtime(root, FakeAnalyzers())
    assert rt.get_agent_report().findings == []
    put(root, ".claude/rules/api.md", "# API\n")
    (finding,) = rt.get_agent_report().findings
    assert (finding.file, finding.line, finding.kind, finding.status) == (".claude/rules/api.md", 1, "agent", "invalid")


def test_the_runtime_checks_the_conventional_locations_without_the_agents_table(tmp_path: Path) -> None:
    root = layout(make_repo(tmp_path / "repo"))
    put(root, ".claude/rules/api.md", "# API\n")
    rt = make_runtime(root, FakeAnalyzers())
    assert rt.get_agent_report().findings == []
    put(root, ".mcp.json", '{"mcpServers": {"docs": {"url": "https://example.com/mcp"}}}')
    (finding,) = rt.get_agent_report().findings
    assert (finding.file, finding.kind, finding.status) == (".mcp.json", "agent", "invalid")
    assert ".claude/rules/api.md" in rt.get_agent_report().files


# ── Globs a reader may still match ───────────────────────────────────────


def rule_lines(root: Path, paths: str, files: FakeRepoFiles | None = None) -> list[str]:
    put(root, ".claude/rules/api.md", f"---\npaths: {paths}\n---\n")
    report = agent_files.check(root, DEFAULT, files or repo_files_on_disk(root))
    return [f"{f.file}:{f.line}: {f.message}" for f in report.findings]


def test_a_glob_under_an_ignored_directory_is_not_reported(repo: Path) -> None:
    files = replace(repo_files_on_disk(repo), ignored_paths=frozenset({"gen"}))
    assert rule_lines(repo, '["gen/**/*.ts"]', files) == []
    assert rule_lines(repo, '["gen/**/*.ts"]') == [
        ".claude/rules/api.md:2: paths glob 'gen/**/*.ts' matches no git-visible file"
    ]


def test_a_glob_for_ignored_files_is_not_reported(repo: Path) -> None:
    files = replace(repo_files_on_disk(repo), ignored_paths=frozenset({".env"}))
    assert rule_lines(repo, '["**/.env*"]', files) == []


def test_a_glob_through_a_symlinked_directory_is_not_reported(repo: Path, tmp_path: Path) -> None:
    put(tmp_path / "elsewhere", "shared/a.ts", "export {};\n")
    (repo / "linked").symlink_to(tmp_path / "elsewhere" / "shared", target_is_directory=True)
    assert rule_lines(repo, '["linked/**/*.ts"]') == []


def test_a_glob_under_an_untracked_directory_on_disk_is_not_reported(repo: Path) -> None:
    put(repo, "scratch/a.ts", "export {};\n")
    files = repo_files_on_disk(repo)
    hidden = replace(files, files=files.files - {"scratch/a.ts"}, tracked=files.tracked - {"scratch/a.ts"})
    assert rule_lines(repo, '["scratch/**/*.py"]', hidden) == []


def test_a_glob_naming_a_directory_matches_it(repo: Path) -> None:
    assert rule_lines(repo, "['src/api']") == []
    assert rule_lines(repo, "['src/api', 'api']") == []


def test_a_comma_string_does_not_split_inside_brackets(repo: Path) -> None:
    assert rule_lines(repo, "src/[a,b].ts") == []
    assert rule_lines(repo, '"src/[a,b].ts, src/api/**"') == []


def test_the_claude_code_docs_escaped_bracket_example_is_not_reported(repo: Path) -> None:
    assert rule_lines(repo, "['photos \\[2024/**']") == []


@pytest.mark.parametrize(
    "glob",
    ["src/[z-a].ts", "src/[a-[]", "[", "]", "[]]", "[!]", "a[--z]", "x[a&&b]", "y[a~~b]", "z[a||b]", "[^]", "{,}", "{"],
)
def test_no_glob_crashes_the_check(repo: Path, glob: str) -> None:
    put(repo, ".github/instructions/x.instructions.md", f"---\napplyTo: [{glob!r}]\n---\n")
    assert isinstance(rule_lines(repo, f"[{glob!r}]"), list)


@pytest.mark.parametrize("glob", ["src/[z-a].ts", "src/[a-[]"])
def test_a_class_the_matcher_cannot_compile_is_an_invalid_pattern(repo: Path, glob: str) -> None:
    assert rule_lines(repo, f"[{glob!r}]") == [
        f".claude/rules/api.md:2: paths glob {glob!r} has an invalid bracket expression; "
        "Claude Code matches no file with it"
    ]
    put(repo, ".claude/rules/api.md", API_RULE)
    put(repo, INSTRUCTIONS, f"---\napplyTo: [{glob!r}]\n---\n")
    assert lines(repo, DEFAULT) == []


# ── Frontmatter that does not parse ──────────────────────────────────────


def test_a_yaml_error_outside_a_glob_value_is_reported(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\ndescription: `npm test` runner\npaths: ["src/api/**"]\n---\n')
    assert lines(repo, DEFAULT) == [
        ".claude/rules/api.md:2: invalid YAML frontmatter: found character '`' that cannot start any token; "
        "Claude Code ignores it and loads the rule for every file"
    ]


@pytest.mark.parametrize(
    "text", ["---\npaths: **/*.py\n---\n", "---\npaths:\n  - *.py\n---\n", "---\npaths: {src,lib}/**\n---\n"]
)
def test_an_unquoted_glob_value_is_reported_only_under_strict(repo: Path, text: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    assert lines(repo, DEFAULT) == []
    (finding,) = [line for line in lines(repo, AgentsConfig(strict=True)) if "invalid YAML" in line]
    assert finding.startswith(".claude/rules/api.md:")
    assert finding.endswith("; Claude Code ignores it and loads the rule for every file")


def test_an_unquoted_alias_outside_a_glob_key_is_reported(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\ndescription: *anchor\npaths: ["src/api/**"]\n---\n')
    (finding,) = lines(repo, DEFAULT)
    assert finding.startswith(".claude/rules/api.md:2: invalid YAML frontmatter:")


def test_repeated_instruction_keys_are_reported_only_under_strict(repo: Path) -> None:
    put(repo, INSTRUCTIONS, '---\napplyTo: "lib/**"\napplyTo: "**"\n---\n')
    assert lines(repo, DEFAULT) == []
    assert lines(repo, AgentsConfig(strict=True)) == [
        f"{INSTRUCTIONS}:3: frontmatter key 'applyTo' is repeated (first at line 2)"
    ]


def test_yaml_1_1_booleans_are_read_as_strings(tmp_path: Path) -> None:
    put(tmp_path, ".claude/agents/a.md", "---\nname: yes\ndescription: on\n---\n")
    put(tmp_path, ".github/agents/b.md", "---\ndescription: no\n---\n")
    assert lines(tmp_path, DEFAULT) == []
