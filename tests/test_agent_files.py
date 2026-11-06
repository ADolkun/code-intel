"""The agent-file checks: rule frontmatter and globs, skills, duplicate keys and
the instruction map, each failure with its exact ``path:line: message`` line."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_fixtures import FakeAnalyzers, config_toml, make_repo, make_runtime, write, write_config

from code_intel import agent_files
from code_intel.config import AgentsConfig, InstructionMap

MAP_FILE = "AGENTS.md"
AGENTS = AgentsConfig(".claude/rules", ".claude/skills", InstructionMap(MAP_FILE, "Scoped instruction map"))

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


def lines(root: Path, agents: AgentsConfig = AGENTS) -> list[str]:
    visible = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    return [f"{f.file}:{f.line}: {f.message}" for f in agent_files.check(root, agents, visible)]


def put(root: Path, rel: str, text: str) -> None:
    write(root / rel, text)


def test_a_consistent_set_of_agent_files_has_no_findings(repo: Path) -> None:
    assert lines(repo) == []


def test_a_missing_rules_and_skills_directory_is_not_a_finding(tmp_path: Path) -> None:
    write(tmp_path / "x.txt", "x")
    assert lines(tmp_path, AgentsConfig(".claude/rules", ".claude/skills", None)) == []


# ── Rule frontmatter ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# API\n", ".claude/rules/api.md:1: missing YAML frontmatter: the file must open with a --- line"),
        ("---\npaths:\n  - src/api/**\n", ".claude/rules/api.md:1: YAML frontmatter is not closed by a --- line"),
        ("---\n---\n", ".claude/rules/api.md:1: YAML frontmatter must be a mapping"),
        ("---\n- a\n---\n", ".claude/rules/api.md:1: YAML frontmatter must be a mapping"),
        (
            "---\npaths: [src/api/**\n---\n",
            ".claude/rules/api.md:2: invalid YAML frontmatter: expected ',' or ']', but got '<stream end>'",
        ),
        (
            "---\ndescription: x\n---\n",
            ".claude/rules/api.md:1: frontmatter 'paths' must be a non-empty list of non-blank strings",
        ),
        (
            "---\npaths: []\n---\n",
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-empty list of non-blank strings",
        ),
        (
            "---\npaths: src/api/**\n---\n",
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-empty list of non-blank strings",
        ),
        (
            '---\npaths:\n  - "src/api/**"\n  - " "\n---\n',
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-empty list of non-blank strings",
        ),
        (
            "---\npaths:\n  - 3\n---\n",
            ".claude/rules/api.md:2: frontmatter 'paths' must be a non-empty list of non-blank strings",
        ),
    ],
    ids=[
        "no-frontmatter",
        "unclosed",
        "empty",
        "not-a-mapping",
        "invalid-yaml",
        "no-paths",
        "empty-paths",
        "paths-is-a-string",
        "blank-path",
        "non-string-path",
    ],
)
def test_a_rule_without_valid_paths_is_reported(repo: Path, text: str, expected: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    assert lines(repo)[0] == expected


def test_apply_to_is_rejected_beside_valid_paths(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\napplyTo: "src/**"\npaths:\n  - "src/api/**"\n---\n')
    assert lines(repo) == [".claude/rules/api.md:2: frontmatter key 'applyTo' is not allowed; use 'paths'"]


def test_a_repeated_frontmatter_key_is_reported_at_the_repeat(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\npaths:\n  - "src/api/**"\npaths:\n  - "src/ui/**"\n---\n')
    assert lines(repo)[0] == ".claude/rules/api.md:4: frontmatter key 'paths' is repeated (first at line 2)"


def test_a_glob_matching_no_file_is_reported_at_its_item(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\npaths:\n  - "src/api/**"\n  - "src/renamed/**"\n---\n')
    assert lines(repo)[0] == ".claude/rules/api.md:4: paths glob 'src/renamed/**' matches no git-visible file"


def test_a_glob_is_matched_against_git_visible_files_only(repo: Path) -> None:
    visible = {"src/ui/view.tsx", ".claude/rules/api.md", ".claude/rules/ui.md", MAP_FILE}
    found = [f"{f.file}:{f.line}: {f.message}" for f in agent_files.check(repo, AGENTS, visible)]
    assert ".claude/rules/api.md:3: paths glob 'src/api/**' matches no git-visible file" in found


@pytest.mark.parametrize(
    ("glob", "matches"),
    [
        ("src/ui-tests/*.{ts,tsx}", True),
        ("src/ui-tests/*.{ts,js}", False),
        ("src/**/view.tsx", True),
        ("src/*", False),
        ("src/api/handler.py", True),
        ("**/*.py", True),
    ],
)
def test_globs_expand_braces_and_stars(repo: Path, glob: str, matches: bool) -> None:
    put(repo, ".claude/rules/api.md", f'---\npaths:\n  - "{glob}"\n---\n')
    found = [m for m in lines(repo) if "matches no git-visible file" in m]
    assert (found == []) is matches


# ── Skills ───────────────────────────────────────────────────────────────


def test_a_skill_name_must_equal_its_directory(repo: Path) -> None:
    put(repo, ".claude/skills/deploy/SKILL.md", SKILL.replace("name: deploy", "name: release"))
    assert lines(repo) == [
        ".claude/skills/deploy/SKILL.md:2: skill name 'release' does not match its directory 'deploy'"
    ]


def test_a_skill_name_must_be_present(repo: Path) -> None:
    put(repo, ".claude/skills/deploy/SKILL.md", "---\ndescription: Ship.\n---\n")
    assert lines(repo) == [".claude/skills/deploy/SKILL.md:1: frontmatter 'name' must be a non-blank string"]


@pytest.mark.parametrize("description", ["description:", "description: ' '", "description: [a]"])
def test_a_skill_needs_a_description(repo: Path, description: str) -> None:
    put(repo, ".claude/skills/deploy/SKILL.md", f"---\nname: deploy\n{description}\n---\n")
    assert lines(repo) == [".claude/skills/deploy/SKILL.md:3: frontmatter 'description' must be a non-blank string"]


def test_a_skill_without_a_description_key_is_reported_at_line_1(repo: Path) -> None:
    put(repo, ".claude/skills/deploy/SKILL.md", "---\nname: deploy\n---\n")
    assert lines(repo) == [".claude/skills/deploy/SKILL.md:1: frontmatter 'description' must be a non-blank string"]


def test_two_skills_with_one_name_are_reported_once_at_the_second(repo: Path) -> None:
    put(repo, ".claude/skills/a/deeper/SKILL.md", SKILL.replace("name: deploy", "name: deeper"))
    put(repo, ".claude/skills/deeper/SKILL.md", SKILL.replace("name: deploy", "name: deeper"))
    assert lines(repo) == [
        ".claude/skills/deeper/SKILL.md:2: duplicate skill name 'deeper' (also in .claude/skills/a/deeper/SKILL.md)"
    ]


def test_a_repeated_skill_key_is_reported(repo: Path) -> None:
    put(repo, ".claude/skills/deploy/SKILL.md", "---\nname: deploy\ndescription: a\nname: deploy\n---\n")
    assert lines(repo) == [".claude/skills/deploy/SKILL.md:4: frontmatter key 'name' is repeated (first at line 2)"]


def test_a_skill_directory_without_a_skill_md_is_reported(repo: Path) -> None:
    put(repo, ".claude/skills/notes/README.md", "# Notes\n")
    assert lines(repo) == [".claude/skills/notes:1: skill directory 'notes' has no SKILL.md"]


def test_a_skill_md_in_the_wrong_case_is_not_a_skill(repo: Path) -> None:
    put(repo, ".claude/skills/notes/skill.md", SKILL.replace("name: deploy", "name: notes"))
    assert lines(repo) == [".claude/skills/notes:1: skill directory 'notes' has no SKILL.md"]


def test_a_directory_grouping_skills_and_loose_files_are_not_reported(repo: Path) -> None:
    put(repo, ".claude/skills/group/inner/SKILL.md", SKILL.replace("name: deploy", "name: inner"))
    put(repo, ".claude/skills/README.md", "# Skills\n")
    assert lines(repo) == []


# ── MCP servers ──────────────────────────────────────────────────────────

MCP_JSON = """\
{
  "mcpServers": {
    "local": {"command": "uv", "args": ["run", "server"]},
    "remote": {"url": "https://example.com/mcp"}
  }
}
"""


def test_a_valid_mcp_json_has_no_findings(repo: Path) -> None:
    put(repo, ".mcp.json", MCP_JSON)
    assert lines(repo) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{", ".mcp.json:1: invalid JSON: Expecting property name enclosed in double quotes"),
        (
            '{\n  "mcpServers": {\n    "a": 1,\n  }\n}\n',
            ".mcp.json:3: invalid JSON: Illegal trailing comma before end of object",
        ),
        ("[]", ".mcp.json:1: the file must be a JSON object with an 'mcpServers' object"),
        ("{}", ".mcp.json:1: the file must be a JSON object with an 'mcpServers' object"),
        ('{"mcpServers": []}', ".mcp.json:1: the file must be a JSON object with an 'mcpServers' object"),
        ('{"servers": {}}', ".mcp.json:1: the file must be a JSON object with an 'mcpServers' object"),
    ],
    ids=["unclosed", "trailing-comma", "array", "no-servers-key", "servers-not-an-object", "wrong-key"],
)
def test_an_mcp_json_that_is_not_an_object_with_servers_is_reported(repo: Path, text: str, expected: str) -> None:
    put(repo, ".mcp.json", text)
    assert lines(repo) == [expected]


@pytest.mark.parametrize(
    "server",
    [
        '"uv"',
        "null",
        "{}",
        '{"args": ["x"]}',
        '{"command": ""}',
        '{"command": "  "}',
        '{"command": 3}',
        '{"url": ["x"]}',
    ],
)
def test_a_server_needs_a_string_command_or_url(repo: Path, server: str) -> None:
    put(repo, ".mcp.json", f'{{\n  "mcpServers": {{\n    "ok": {{"command": "uv"}},\n    "bad": {server}\n  }}\n}}\n')
    assert lines(repo) == [".mcp.json:4: MCP server 'bad' must be an object with a string 'command' or 'url'"]


def test_an_empty_mcp_servers_object_is_valid(repo: Path) -> None:
    put(repo, ".mcp.json", '{"mcpServers": {}}')
    assert lines(repo) == []


def test_an_mcp_json_git_does_not_see_is_not_checked(repo: Path) -> None:
    put(repo, ".mcp.json", "{")
    visible = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()} - {".mcp.json"}
    assert agent_files.check(repo, AGENTS, visible) == []


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
    assert lines(repo) == ["AGENTS.md:1: no heading 'Scoped instruction map'"]


def test_a_missing_map_file_is_reported(repo: Path) -> None:
    (repo / MAP_FILE).unlink()
    found = lines(repo)
    assert len(found) == 1
    assert found[0].startswith("AGENTS.md:1: cannot read the file (FileNotFoundError")


def test_a_rule_with_invalid_paths_is_reported_once_not_also_as_a_map_mismatch(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", "---\npaths: []\n---\n")
    assert lines(repo) == [".claude/rules/api.md:2: frontmatter 'paths' must be a non-empty list of non-blank strings"]


def test_nested_rule_files_are_labelled_by_their_path_below_the_rules_directory(repo: Path) -> None:
    put(repo, ".claude/rules/web/forms.md", '---\npaths:\n  - "src/ui/**"\n---\n')
    put(repo, MAP_FILE, MAP.replace("### Next section", "- `web/forms`: `src/ui/**`\n\n### Next section"))
    assert lines(repo) == []


def test_without_an_instruction_map_only_the_files_are_checked(repo: Path) -> None:
    put(repo, MAP_FILE, "# Agents\n")
    assert lines(repo, AgentsConfig(".claude/rules", ".claude/skills", None)) == []


# ── Through the runtime ──────────────────────────────────────────────────

AGENTS_TABLE = """\
[agents]
rules_dir = ".claude/rules"
skills_dir = ".claude/skills"
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
"""


def test_the_runtime_reads_the_agents_table_and_lists_the_findings(tmp_path: Path) -> None:
    root = layout(make_repo(tmp_path / "repo"))
    write_config(root, config_toml() + AGENTS_TABLE)
    rt = make_runtime(root, FakeAnalyzers())
    assert rt.get_agent_findings() == []
    put(root, ".claude/rules/api.md", "# API\n")
    (finding,) = rt.get_agent_findings()
    assert (finding.file, finding.line, finding.kind, finding.status) == (".claude/rules/api.md", 1, "agent", "invalid")


def test_the_runtime_has_no_agent_findings_without_the_agents_table(tmp_path: Path) -> None:
    root = layout(make_repo(tmp_path / "repo"))
    put(root, ".claude/rules/api.md", "# API\n")
    assert make_runtime(root, FakeAnalyzers()).get_agent_findings() == []
