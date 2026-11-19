"""The agent-file checks: discovery at every Claude Code and Copilot location,
rules, subagents, Copilot instructions and agents, ``strict`` and the
instruction map, each failure with its exact ``path:line: message`` line."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fake_repo_files import FakeRepoFiles, repo_files_on_disk
from fixture_repo import git_init, hermetic_git
from mcp_fixtures import FakeAnalyzers, config_toml, make_repo, make_runtime, write, write_config

from stack_intel import agent_files
from stack_intel.config import AgentsConfig, InstructionMap
from stack_intel.repo_files import list_repo_files

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
            "Claude Code ignores the frontmatter and loads the rule for every file",
        ),
        (
            "---\npaths:\n  - **/*.py\n---\n",
            ".claude/rules/api.md:3: invalid YAML frontmatter: expected alphabetic or numeric character, "
            "but found '*'; Claude Code ignores the frontmatter and loads the rule for every file",
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
            ".claude/rules/api.md:3: paths glob 'src/api/[id' matches no git-visible file",
        ),
        (
            "---\npaths: [src/api/**\n---\n",
            ".claude/rules/api.md:2: paths glob '[src/api/**' matches no git-visible file",
        ),
    ],
    ids=["unclosed", "block-list-item", "no-match-item", "no-match-comma-string", "unclosed-bracket", "recovered"],
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
        "---\n- a\n---\n",
        '---\npaths:\n  - "src/api/**"\n  - " "\n---\n',
        '---\npaths:\n  - "src/renamed/**"\npaths:\n  - "src/api/**"\n---\n',
        '---\npaths: "src/api/**, src/ui/**"\n---\n',
        '---\napplyTo: "src/**"\npaths:\n  - "src/api/**"\n---\n',
        '---\napplyTo: "src/**"\n---\n',
        "---\npaths: **/*.py\n---\n",
        '---\npaths: ["*.py"]\nunknown: 1\nalwaysApply: true\n---\n',
        '---\npaths: ["!src/**", "src/@(a|b)/**", "src/\\\\[x\\\\]", "src/{1..3}/**"]\n---\n',
        '---\npaths: "src/**/*.{ts,js}, src/api/**"\n---\n',
        '---\npaths: ["/src/api/handler.py", "view.tsx", "src/ui/[vw]iew.tsx"]\n---\n',
        '---\ndescription: API rules: keep handlers thin\npaths:\n  - "src/api/**"\n---\n',
        '---\ndescription: `npm test` runner\npaths:\n  - "src/api/**"\n---\n',
        '---\npaths: ["src/**/*.py, lib/**/*.ts"]\n---\n',
        '---\npaths: ["src/ui/*.{ tsx, ts }"]\n---\n',
        '---\npaths: [["src/**"], 3]\n---\n',
        '---\npaths: ["src/api/**", 3]\n---\n',
        "---\n\n# API\n\nRules for the handlers.\n",
        '---\npaths: ["src/api/**"]\ndescription: Backend --- API layer\n---\n',
        '---\npaths: ["**/*.PY", "SRC/API/HANDLER.PY"]\n---\n',
        '---\npaths: ["src/**"]\n-----\n',
    ],
    ids=[
        "no-frontmatter",
        "no-paths",
        "empty-frontmatter",
        "null-paths",
        "empty-list",
        "not-a-mapping",
        "blank-item",
        "repeated-key-last-wins",
        "comma-string",
        "apply-to-beside-paths",
        "apply-to-alone",
        "recovered-unquoted-glob",
        "unknown-keys",
        "unmodelled-globs",
        "braces-in-a-comma-string",
        "anchored-any-depth-and-class",
        "colon-in-description",
        "backtick-description",
        "comma-in-an-item",
        "spaced-brace-options",
        "nested-list-and-number",
        "mixed-list-keeps-a-string",
        "horizontal-rule-unclosed",
        "dashes-after-paths",
        "case-insensitive",
        "five-dash-close",
    ],
)
def test_a_rule_the_readers_accept_is_not_reported_by_default(repo: Path, text: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    assert lines(repo, DEFAULT) == []


@pytest.mark.parametrize(
    ("paths", "file"),
    [
        ('["src/**"]', "packages/web/src/a.ts"),
        ('["**/*.TS"]', "src/a.ts"),
        ('["README.MD"]', "readme.md"),
        ('["src/components/*.tsx"]', "web/src/components/a.tsx"),
        ('["src/**/*.ts, lib/**/*.ts"]', "src/a.ts"),
        ('["src/*.{ js, ts }"]', "src/a.ts"),
        ('[["src/**"], 3]', "src/a.ts"),
    ],
)
def test_a_rule_glob_matching_under_a_lenient_reading_is_not_reported(tmp_path: Path, paths: str, file: str) -> None:
    put(tmp_path, file, "x\n")
    put(tmp_path, ".claude/rules/api.md", f"---\npaths: {paths}\n---\n")
    assert lines(tmp_path, DEFAULT) == []


@pytest.mark.parametrize(
    "paths",
    ['"src/**"', "'src/**/*.ts'", '["src/api/**"]', "['src/**', 'src/api/**']"],
    ids=["double-quoted", "single-quoted", "flow-list", "single-quoted-flow-list"],
)
def test_the_retry_leaves_quoted_values_and_valid_lists_alone(tmp_path: Path, paths: str) -> None:
    put(tmp_path, "src/api/a.ts", "x\n")
    put(tmp_path, ".claude/rules/api.md", f"---\ndescription: API rules: keep handlers thin\npaths: {paths}\n---\n")
    assert lines(tmp_path, DEFAULT) == []


def test_a_bracket_value_the_retry_quotes_is_reported_when_no_piece_matches(tmp_path: Path) -> None:
    put(tmp_path, "src/api/a.ts", "x\n")
    put(
        tmp_path,
        ".claude/rules/api.md",
        "---\ndescription: API rules: keep handlers thin\npaths: [src/api/**, {lib]\n---\n",
    )
    assert lines(tmp_path, DEFAULT) == [
        ".claude/rules/api.md:3: Claude Code reads 'paths' as the single string '[src/api/**, {lib]', which "
        "matches no git-visible file: the unquoted ': ' in 'description' (line 2) makes the frontmatter invalid "
        "YAML, and Claude Code's recovery then quotes 'paths' as text. Quote the description "
        "(\"API rules: keep handlers thin\") and write 'paths' as a valid list."
    ]


def test_a_bracket_value_the_retry_quotes_is_not_reported_when_a_piece_matches(tmp_path: Path) -> None:
    put(tmp_path, "src/api/a.ts", "x\n")
    put(tmp_path, ".claude/rules/api.md", "---\ndescription: a: b\npaths: [x, src/api/**, {lib]\n---\n")
    assert lines(tmp_path, DEFAULT) == []


def test_a_bracket_value_that_breaks_alone_names_paths_as_the_cause(tmp_path: Path) -> None:
    put(tmp_path, "src/a.ts", "x\n")
    put(tmp_path, ".claude/rules/api.md", "---\npaths: [lib/**, {x]\n---\n")
    (found,) = lines(tmp_path, DEFAULT)
    assert found.endswith(
        "'paths' is not a valid YAML list, so Claude Code's recovery quotes it as text. Write 'paths' as a valid list."
    )


@pytest.mark.parametrize("paths", ["123", "\n  nested: map", "[3, 4]"], ids=["number", "map", "numbers"])
def test_paths_with_no_string_glob_left_is_reported(repo: Path, paths: str) -> None:
    put(repo, ".claude/rules/api.md", f"---\npaths: {paths}\n---\n")
    assert lines(repo, DEFAULT) == [
        ".claude/rules/api.md:2: Claude Code finds no glob in 'paths' and loads the rule for every file"
    ]


@pytest.mark.parametrize(
    "paths", ['""', "'  '", '[""]', "[' ', '']"], ids=["blank", "whitespace", "blank-item", "blank-items"]
)
def test_an_empty_paths_placeholder_is_reported_only_under_strict(repo: Path, paths: str) -> None:
    put(repo, ".claude/rules/api.md", f"---\npaths: {paths}\n---\n")
    assert lines(repo, DEFAULT) == []
    assert lines(repo, AgentsConfig(strict=True)) == [
        ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings (strict)"
    ]


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        (".claude/rules/r.md", '---\npaths: ["docs/[B-a]*.md"]\n---\n'),
        (".claude/rules/r.md", '---\npaths: ["docs/[Z-a]*.md", "docs/*.md"]\n---\n'),
        (".claude/rules/r.md", '---\npaths: ["docs/[Z-a]*.md"]\n---\n'),
        (".claude/rules/r.md", '---\npaths: ["docs/[A-_]*.md"]\n---\n'),
        (".github/instructions/d.instructions.md", '---\napplyTo: "docs/[B-a]*.md"\n---\n'),
    ],
    ids=["rule-b-a", "rule-z-a", "rule-z-a-underscore", "rule-a-underscore", "instructions-b-a"],
)
def test_a_range_spanning_cases_is_read_case_insensitively(tmp_path: Path, rel: str, text: str) -> None:
    put(tmp_path, "docs/x.md", "x\n")
    put(tmp_path, "docs/_x.md", "x\n")
    put(tmp_path, rel, text)
    assert lines(tmp_path, DEFAULT) == []


@pytest.mark.parametrize(
    ("paths", "file"),
    [('["docs/[A-z]*.md"]', "docs/_index.md"), ('["docs/?stanbul.md"]', "docs/İstanbul.md")],
)
def test_case_folding_never_narrows_a_match(tmp_path: Path, paths: str, file: str) -> None:
    put(tmp_path, file, "x\n")
    put(tmp_path, ".claude/rules/r.md", f"---\npaths: {paths}\n---\n")
    assert lines(tmp_path, DEFAULT) == []


@pytest.mark.parametrize("paths", ['["src/---/**"]', "[src/---/**, lib/**]"], ids=["quoted", "flow"])
def test_dashes_inside_paths_name_the_cut_as_the_cause(repo: Path, paths: str) -> None:
    put(repo, ".claude/rules/api.md", f"---\npaths: {paths}\n---\n")
    (found,) = lines(repo, DEFAULT)
    assert found.startswith(".claude/rules/api.md:2: Claude Code ends the frontmatter at the '---' inside 'paths', ")
    assert found.endswith(", which matches no git-visible file; quote the value or avoid '---' in it")


def test_a_mixed_paths_list_is_reported_only_under_strict(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\npaths: ["src/api/**", 3]\n---\n')
    assert lines(repo, DEFAULT) == []
    assert lines(repo, AgentsConfig(strict=True)) == [
        ".claude/rules/api.md:2: frontmatter 'paths' must be a non-blank string or a list of non-blank strings (strict)"
    ]


def test_dashes_inside_a_value_before_paths_are_reported(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", '---\ndescription: Backend --- API layer\npaths: ["src/api/**"]\n---\n')
    assert lines(repo, DEFAULT) == [
        ".claude/rules/api.md:2: Claude Code ends the frontmatter at the '---' inside 'description', drops "
        "'paths' after it and loads the rule for every file; quote the value or avoid '---' in it"
    ]


def test_frontmatter_the_library_cannot_load_during_the_retry_is_unknown(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", f'---\ndescription: a: b\npaths: ["src/**"]\nn: {"9" * 5001}\n---\n')
    assert lines(repo, DEFAULT) == []
    (found,) = lines(repo, AgentsConfig(strict=True))
    assert found.startswith(".claude/rules/api.md:1: frontmatter could not be checked (ValueError")


def test_a_subagent_the_library_cannot_load_is_reported_under_strict(tmp_path: Path) -> None:
    put(tmp_path, ".claude/agents/r.md", "---\nname: r\ndescription: x\nx: !!float abc\n---\n")
    assert lines(tmp_path, DEFAULT) == []
    (found,) = lines(tmp_path, AgentsConfig(strict=True))
    assert found.startswith(".claude/agents/r.md:1: frontmatter could not be checked (")


def test_frontmatter_that_is_not_a_mapping_is_reported_under_strict(repo: Path) -> None:
    put(repo, ".claude/rules/api.md", "---\n- a\n---\n")
    assert lines(repo, DEFAULT) == []
    assert ".claude/rules/api.md:2: YAML frontmatter is not a mapping of keys (strict)" in lines(
        repo, AgentsConfig(strict=True)
    )


def test_a_comma_in_a_list_item_is_reported_only_when_no_piece_matches(tmp_path: Path) -> None:
    put(tmp_path, "src/a.ts", "x\n")
    put(tmp_path, ".claude/rules/api.md", '---\npaths: ["lib/**, docs/**"]\n---\n')
    assert lines(tmp_path, DEFAULT) == [
        ".claude/rules/api.md:2: paths glob 'lib/**, docs/**' matches no git-visible file"
    ]


def test_a_five_dash_line_closes_a_rules_frontmatter(tmp_path: Path) -> None:
    put(tmp_path, "src/a.ts", "x\n")
    put(tmp_path, ".claude/rules/api.md", '---\npaths: ["lib/**"]\n-----\n# API\n')
    assert lines(tmp_path, DEFAULT) == [".claude/rules/api.md:2: paths glob 'lib/**' matches no git-visible file"]


def test_a_latin_1_rule_is_read_with_replacement_characters(tmp_path: Path) -> None:
    write(tmp_path / "src/a.ts", "x\n")
    (tmp_path / ".claude/rules").mkdir(parents=True)
    (tmp_path / ".claude/rules/api.md").write_bytes('---\npaths: ["src/**"]\n---\n# Caf\u00e9\n'.encode("latin-1"))
    (tmp_path / "CLAUDE.md").write_bytes("# R\u00e9sum\u00e9 of the rules\n".encode("latin-1"))
    assert lines(tmp_path, DEFAULT) == []


def test_a_symlinked_rules_directory_is_followed(tmp_path: Path) -> None:
    put(tmp_path, "src/a.ts", "x\n")
    put(tmp_path, "shared/rules/api.md", '---\npaths: ["lib/**"]\n---\n')
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/rules").symlink_to("../shared/rules", target_is_directory=True)
    put(tmp_path, MAP_FILE, "# Agents\n\n## Scoped instruction map\n\n- `api`: `lib/**`\n")
    found = lines(tmp_path, AgentsConfig(instruction_map=InstructionMap(MAP_FILE, "Scoped instruction map")))
    assert found == [".claude/rules/api.md:2: paths glob 'lib/**' matches no git-visible file"]


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
            "---\nname: review\ndescription: x\ntools:\n  - **\n---\n",
            ".claude/agents/review.md:5: invalid YAML frontmatter: expected alphabetic or numeric character, "
            "but found '*'; Claude Code skips the subagent",
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


def test_a_subagent_claude_code_skips_does_not_count_as_a_duplicate(tmp_path: Path) -> None:
    put(tmp_path, ".claude/agents/a/review.md", "---\nname: review\n---\n")
    put(tmp_path, ".claude/agents/review.md", "---\nname: review\ndescription: y\n---\n")
    assert lines(tmp_path, DEFAULT) == [
        ".claude/agents/a/review.md:1: subagent 'review' has no description; Claude Code skips it"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "# Agents in this folder\n",
        "---\ndescription: no name, so documentation\n---\n",
        "---\nname: r\ndescription: x\ntools: Read, Grep\nmodel: sonnet\ncustomKey: 1\n---\n",
        f"---\nname: {'r' * 256}\ndescription: x\n---\n",
        "---\nname: r\ndescription: Use this agent when: the user asks for a review\n---\n",
        "---\nname: r\ndescription: [WIP] reviews diffs\n---\n",
        "---\nname: r\ndescription: @reviewer checks diffs\n---\n",
        "---\nname: r\ndescription: `review` the diff\n---\n",
        "---\nname: r\ndescription: x\ntools:\n\t- Read\n\t- Grep\n---\n",
        "---\nname: r\ndescription: Reviews. Examples:\\n\\n<example>\\nContext: a diff\n---\n",
        "---\nThese files define the project's subagents.\n",
        "---\n\n# Subagents\n\nOne file per subagent.\n\n---\n\nMore notes.\n",
        "---\nname: [review\ndescription: x\n---\n",
    ],
    ids=[
        "readme",
        "no-name",
        "unknown-keys",
        "256-characters",
        "colon-in-description",
        "leading-bracket",
        "leading-at",
        "leading-backtick",
        "tab-indented-list",
        "examples-on-one-line",
        "rule-line-readme",
        "rule-lines-readme",
        "recovered-name",
    ],
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
            '---\napplyTo: "src/**, lib/**"\n---\n',
            f"{INSTRUCTIONS}:2: applyTo glob 'lib/**' matches no git-visible file",
        ),
        ('---\napplyTo:\n  - "*.rs"\n---\n', f"{INSTRUCTIONS}:3: applyTo glob '*.rs' matches no git-visible file"),
        (
            '---\napplyTo: "**"\nexcludeAgent: reviewer\n---\n',
            f"{INSTRUCTIONS}:3: excludeAgent 'reviewer' must be 'code-review', 'cloud-agent' or 'coding-agent' (Copilot)",
        ),
        (
            '---\napplyTo: "**"\napplyTo: "lib/**"\n---\n',
            f"{INSTRUCTIONS}:3: applyTo glob 'lib/**' matches no git-visible file",
        ),
    ],
    ids=["comma-string", "no-match-item", "exclude-agent", "repeated-key-last-wins"],
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
        "---\napplyTo: 3\n---\n",
        '---\napplyTo: ["src/**", ""]\n---\n',
        '---\napplyTo:\n  - "*.py"\n---\n',
        '---\napplyTo: "**"\nexcludeAgent: coding-agent\n---\n',
        '---\napplyTo: "**"\nexcludeAgent: cloud-agent\n---\n',
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
        "number",
        "blank-item",
        "star-at-any-depth",
        "exclude-coding-agent",
        "exclude-cloud-agent",
    ],
)
def test_copilot_instructions_the_readers_accept_are_not_reported(repo: Path, text: str) -> None:
    put(repo, INSTRUCTIONS, text)
    put(repo, ".github/instructions/sub/dir/x.instructions.md", "# Nested\n")
    assert lines(repo, DEFAULT) == []


@pytest.mark.parametrize(
    ("apply_to", "file"),
    [("'*.py'", "src/a.py"), ("'src/**/*.ts'", "packages/a/src/x.ts")],
)
def test_vscode_prepends_a_globstar_to_apply_to(tmp_path: Path, apply_to: str, file: str) -> None:
    put(tmp_path, file, "x\n")
    put(tmp_path, INSTRUCTIONS, f"---\napplyTo: {apply_to}\n---\n")
    assert lines(tmp_path, DEFAULT) == []


def test_instruction_type_problems_are_reported_only_under_strict(repo: Path) -> None:
    put(repo, INSTRUCTIONS, "---\napplyTo: 3\n---\n")
    assert lines(repo, AgentsConfig(strict=True)) == [
        f"{INSTRUCTIONS}:2: frontmatter 'applyTo' must be a string or a list of non-blank strings (strict)"
    ]


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
        ".claude/rules/api.md:2: Claude Code finds no glob in 'paths' and loads the rule for every file"
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
def test_a_class_the_matcher_cannot_compile_is_read_literally(repo: Path, glob: str) -> None:
    assert rule_lines(repo, f"[{glob!r}]") == [
        f".claude/rules/api.md:2: paths glob {glob!r} matches no git-visible file"
    ]


# ── Frontmatter that does not parse ──────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        (".claude/agents/r.md", "---\nname: r\ndescription: *Use proactively* for reviews\n---\n"),
        (".claude/agents/r.md", "---\nname: r\ndescription: `npm test` runner\n---\n"),
        (".claude/agents/r.md", "---\nname: r\ndescription: @mention\n---\n"),
        (".claude/agents/r.md", "---\nname: r\ndescription: %x\n---\n"),
        (".claude/agents/r.md", "---\nname: r\ndescription: {a,b}/**\n---\n"),
        (".claude/skills/s/SKILL.md", "---\nname: s\ndescription: *Important* skill\n---\n"),
        (".claude/rules/api.md", "---\npaths: **/*.py\ndescription: *x*\n---\n"),
        (".claude/rules/api.md", "---\npaths: {src,lib}/**\n---\n"),
    ],
    ids=[
        "subagent-star",
        "subagent-backtick",
        "subagent-at",
        "subagent-percent",
        "subagent-brace",
        "skill-star",
        "rule-two-values",
        "rule-braces",
    ],
)
def test_a_value_claude_codes_retry_quotes_is_reported_only_under_strict(repo: Path, rel: str, text: str) -> None:
    put(repo, rel, text)
    assert lines(repo, DEFAULT) == []
    (finding,) = [line for line in lines(repo, AgentsConfig(strict=True)) if "invalid YAML" in line]
    assert finding.startswith(f"{rel}:")
    assert finding.endswith("; Claude Code recovers by quoting the value")


@pytest.mark.parametrize(
    "text",
    ["---\npaths:\n  - *.py\n---\n", "---\npaths:\n  - {src,lib}/**\n---\n", "---\n  paths: **/*.py\n---\n"],
    ids=["block-item", "block-item-braces", "indented-key"],
)
def test_a_value_the_retry_does_not_quote_breaks_the_rule(repo: Path, text: str) -> None:
    put(repo, ".claude/rules/api.md", text)
    (finding,) = lines(repo, DEFAULT)
    assert finding.startswith(".claude/rules/api.md:")
    assert finding.endswith("; Claude Code ignores the frontmatter and loads the rule for every file")


@pytest.mark.parametrize("rel", [".claude/rules/api.md", ".claude/skills/deploy/SKILL.md", INSTRUCTIONS])
def test_repeated_keys_are_reported_only_under_strict(repo: Path, rel: str) -> None:
    put(repo, rel, '---\nname: deploy\ndescription: x\npaths: "lib/**"\npaths: "src/**"\napplyTo: "**"\n---\n')
    assert lines(repo, DEFAULT) == []
    assert f"{rel}:5: frontmatter key 'paths' is repeated (first at line 4); the last value wins" in lines(
        repo, AgentsConfig(strict=True)
    )


@pytest.mark.parametrize(
    "line",
    [
        "created: 2024-13-45",
        "x: 2024-02-30",
        "x: !!float abc",
        "x: !!timestamp foo",
        "x: !!bool maybe",
        "x: " + "9" * 5001,
        "x: " + "[" * 3000,
    ],
    ids=["month-13", "february-30", "float-tag", "timestamp-tag", "bool-tag", "huge-integer", "deep-nesting"],
)
@pytest.mark.parametrize("rel", [".claude/rules/api.md", ".claude/agents/r.md", ".claude/skills/deploy/SKILL.md"])
def test_frontmatter_the_yaml_library_cannot_load_is_not_a_finding(repo: Path, rel: str, line: str) -> None:
    put(repo, rel, f'---\nname: deploy\ndescription: x\npaths: ["src/api/**"]\n{line}\n---\n')
    assert lines(repo, DEFAULT) == []
    assert all("could not be checked" in found or "invalid YAML" in found for found in lines(repo, STRICT))


def test_dates_are_read_as_strings(tmp_path: Path) -> None:
    put(tmp_path, ".claude/agents/a.md", "---\nname: 2024-01-02\ndescription: x\n---\n")
    put(tmp_path, ".claude/agents/b.md", "---\nname: 2024-01-02\ndescription: y\n---\n")
    assert lines(tmp_path, DEFAULT) == [
        ".claude/agents/b.md:2: subagent name '2024-01-02' is also used by .claude/agents/a.md; "
        "Claude Code loads only one of them"
    ]


def test_yaml_1_1_booleans_are_read_as_strings(tmp_path: Path) -> None:
    put(tmp_path, ".claude/agents/a.md", "---\nname: yes\ndescription: on\n---\n")
    put(tmp_path, ".github/agents/b.md", "---\ndescription: no\n---\n")
    assert lines(tmp_path, DEFAULT) == []


# ── Paths git must read as data ──────────────────────────────────────────


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic_git(monkeypatch, tmp_path / "home")
    return git_init(tmp_path / "repo")


@pytest.mark.parametrize("glob", [":!y/**", ":(exclude)y/**"])
def test_a_glob_starting_with_a_colon_is_asked_about_as_a_path(git_repo: Path, glob: str) -> None:
    put(git_repo, ".claude/rules/api.md", f"---\npaths: [{glob!r}]\n---\n")
    report = agent_files.check(git_repo, DEFAULT, list_repo_files(git_repo))
    assert [f"{f.file}:{f.line}: {f.message}" for f in report.findings] == [
        f".claude/rules/api.md:2: paths glob {glob!r} matches no git-visible file"
    ]


def test_an_import_starting_with_a_colon_is_asked_about_as_a_path(git_repo: Path) -> None:
    put(git_repo, "CLAUDE.md", "See @:!x.md\n")
    report = agent_files.check(git_repo, DEFAULT, list_repo_files(git_repo))
    assert [f"{f.file}:{f.line}: {f.message}" for f in report.findings] == [
        "CLAUDE.md:1: @:!x.md imports a file that does not exist"
    ]
