"""Skills at ``<root>/<dir>/SKILL.md``: Claude Code's load rules for
``.claude/skills``, the Agent Skills rules for the other roots, and ``strict``."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_repo_files import repo_files_on_disk
from fixture_repo import write

from stack_intel import agent_files
from stack_intel.config import AgentsConfig

DEFAULT = AgentsConfig()
STRICT = AgentsConfig(strict=True)


BROKEN = "---\nname: deploy\ndescription: x\nallowed-tools:\n  - **\n---\n"


def skill(name: str = "deploy", description: str = "Ship a release.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Skill\n"


def lines(root: Path, agents: AgentsConfig = DEFAULT) -> list[str]:
    report = agent_files.check(root, agents, repo_files_on_disk(root))
    return [f"{f.file}:{f.line}: {f.message}" for f in report.findings]


# ── .claude/skills ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "text", "expected"),
    [
        (
            ".claude/skills/synced/SKILL.md",
            skill("synced"),
            ".claude/skills/synced/SKILL.md:1: skill directory 'synced' is reserved; Claude Code does not load it",
        ),
        (
            ".claude/skills/Synced/SKILL.md",
            skill("other"),
            ".claude/skills/Synced/SKILL.md:1: skill directory 'Synced' is reserved; Claude Code does not load it",
        ),
        (
            ".claude/skills/deploy/SKILL.md",
            skill("anthropic-skills"),
            ".claude/skills/deploy/SKILL.md:2: skill name 'anthropic-skills' is reserved; Claude Code does not load it",
        ),
        (
            ".claude/skills/deploy/SKILL.md",
            skill("anthropic-skills:pdf"),
            ".claude/skills/deploy/SKILL.md:2: skill name 'anthropic-skills:pdf' is reserved; "
            "Claude Code does not load it",
        ),
        (
            ".claude/skills/anthropic-skills/SKILL.md",
            "# No frontmatter, so the directory is the name\n",
            ".claude/skills/anthropic-skills/SKILL.md:1: skill name 'anthropic-skills' is reserved; "
            "Claude Code does not load it",
        ),
        (
            ".claude/skills/deploy/SKILL.md",
            skill("'de<ploy'"),
            ".claude/skills/deploy/SKILL.md:2: skill name 'de<ploy' has a '<', control, zero-width, bidi or "
            "private-use character; Claude Code does not load it",
        ),
        (
            ".claude/skills/deploy/SKILL.md",
            skill('"de\\u200bploy"'),
            ".claude/skills/deploy/SKILL.md:2: skill name 'de\\u200bploy' has a '<', control, zero-width, bidi or "
            "private-use character; Claude Code does not load it",
        ),
        (
            ".claude/skills/deploy/SKILL.md",
            BROKEN,
            ".claude/skills/deploy/SKILL.md:5: invalid YAML frontmatter: expected alphabetic or numeric character, "
            "but found '*'; Claude Code loads the skill without it",
        ),
    ],
    ids=[
        "synced",
        "synced-any-case",
        "reserved-name",
        "reserved-prefix",
        "reserved-directory-name",
        "angle-bracket",
        "zero-width",
        "invalid-yaml",
    ],
)
def test_a_skill_claude_code_does_not_load_is_reported(tmp_path: Path, rel: str, text: str, expected: str) -> None:
    write(tmp_path / rel, text)
    assert lines(tmp_path) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        skill("release"),
        skill("Deploy Things"),
        "# No frontmatter\n",
        "---\nname: deploy\n---\n",
        "---\nname: deploy\nwhen_to_use: x\nallowed-tools: Read\ncustom: 1\n---\n",
        skill("x" * 100),
        skill(description="Deploys: builds, then ships"),
        skill(description="[WIP] ships a release"),
        "---\nname: deploy\ndescription: a\nname: deploy\n---\n",
    ],
    ids=[
        "name-differs-from-directory",
        "title-case-name",
        "no-frontmatter",
        "no-description",
        "unknown-keys",
        "long",
        "colon-in-description",
        "leading-bracket",
        "repeated-key",
    ],
)
def test_a_claude_only_skill_is_not_held_to_the_agent_skills_rules(tmp_path: Path, text: str) -> None:
    write(tmp_path / ".claude/skills/deploy/SKILL.md", text)
    write(tmp_path / ".claude/skills/group/inner/SKILL.md", skill("inner"))
    write(tmp_path / ".claude/skills/notes/README.md", "# Notes\n")
    assert lines(tmp_path) == []


# ── Agent Skills roots ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("root", "agents"),
    [("skills", AgentsConfig(skill_dirs=("skills",))), (".agents/skills", STRICT), (".github/skills", STRICT)],
    ids=["configured", "agents-strict", "github-strict"],
)
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            skill("Deploy"),
            "SKILL.md:2: skill name 'Deploy' must be lowercase letters, digits and hyphens (Agent Skills)",
        ),
        (
            skill("de_ploy"),
            "SKILL.md:2: skill name 'de_ploy' must be lowercase letters, digits and hyphens (Agent Skills)",
        ),
        (skill("x" * 65), f"SKILL.md:2: skill name {'x' * 65!r} must be at most 64 characters (Agent Skills)"),
        (
            skill("-deploy"),
            "SKILL.md:2: skill name '-deploy' must not start or end with a hyphen or hold '--' (Agent Skills)",
        ),
        (
            skill("deploy-"),
            "SKILL.md:2: skill name 'deploy-' must not start or end with a hyphen or hold '--' (Agent Skills)",
        ),
        (
            skill("de--ploy"),
            "SKILL.md:2: skill name 'de--ploy' must not start or end with a hyphen or hold '--' (Agent Skills)",
        ),
        (
            skill("release"),
            "SKILL.md:2: skill name 'release' does not match its directory 'deploy' (Agent Skills)",
        ),
        ("---\ndescription: x\n---\n", "SKILL.md:1: frontmatter 'name' must be a non-blank string (Agent Skills)"),
        (
            "---\nname: 3\ndescription: x\n---\n",
            "SKILL.md:2: frontmatter 'name' must be a non-blank string (Agent Skills)",
        ),
        ("---\nname: deploy\n---\n", "SKILL.md:1: frontmatter 'description' must be a non-blank string (Agent Skills)"),
        (skill(description="''"), "SKILL.md:3: frontmatter 'description' must be a non-blank string (Agent Skills)"),
        (
            skill(description="x" * 1025),
            "SKILL.md:3: skill description is 1025 characters; Agent Skills allows at most 1024",
        ),
        (
            "# Deploy\n",
            "SKILL.md:1: missing YAML frontmatter: an Agent Skills SKILL.md opens with a --- line, "
            "then name and description",
        ),
    ],
    ids=[
        "upper-case",
        "underscore",
        "too-long",
        "leading-hyphen",
        "trailing-hyphen",
        "double-hyphen",
        "directory",
        "no-name",
        "number-name",
        "no-description",
        "blank-description",
        "long-description",
        "no-frontmatter",
    ],
)
def test_an_agent_skills_violation_is_reported(
    tmp_path: Path, root: str, agents: AgentsConfig, text: str, expected: str
) -> None:
    write(tmp_path / root / "deploy" / "SKILL.md", text)
    assert lines(tmp_path, agents) == [f"{root}/deploy/{expected}"]


@pytest.mark.parametrize("root", [".agents/skills", ".github/skills"])
@pytest.mark.parametrize("text", [skill("Deploy"), skill("release"), "---\nname: deploy\n---\n", "# Deploy\n"])
def test_the_agent_skills_rules_are_not_default_findings_for_the_conventional_roots(
    tmp_path: Path, root: str, text: str
) -> None:
    write(tmp_path / root / "deploy" / "SKILL.md", text)
    assert lines(tmp_path) == []


def test_frontmatter_that_does_not_parse_is_reported_for_a_configured_root(tmp_path: Path) -> None:
    write(tmp_path / "skills/deploy/SKILL.md", BROKEN)
    assert lines(tmp_path, AgentsConfig(skill_dirs=("skills",))) == [
        "skills/deploy/SKILL.md:5: invalid YAML frontmatter: expected alphabetic or numeric character, "
        "but found '*'; the Agent Skills reader cannot read its name and description"
    ]


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        (".github/skills/fmt/SKILL.md", "---\nname: fmt\ndescription: 'it''s unclosed\n---\n"),
        (".agents/skills/lint/SKILL.md", "---\nname: lint\ndescription: x\nmetadata:\n  a: 1\n b: 2\n---\n"),
        (".github/skills/deploy/SKILL.md", BROKEN),
        (".agents/skills/deploy/SKILL.md", "---\nname: deploy\ndescription: x\n"),
    ],
    ids=["unclosed-quote", "mis-indented-map", "block-item", "unclosed-header"],
)
def test_vscode_loads_a_skill_whose_frontmatter_does_not_parse(tmp_path: Path, rel: str, text: str) -> None:
    write(tmp_path / rel, text)
    assert lines(tmp_path) == []
    (found,) = lines(tmp_path, STRICT)
    assert found.startswith(f"{rel}:")
    assert found.endswith(" (strict)")
    assert "VS Code" not in found


@pytest.mark.parametrize("root", [".agents/skills", ".github/skills"])
def test_frontmatter_claude_codes_retry_reads_is_not_reported_for_the_conventional_roots(
    tmp_path: Path, root: str
) -> None:
    write(tmp_path / root / "deploy" / "SKILL.md", skill(description="Ships: `npm run release`"))
    assert lines(tmp_path) == []


def test_a_valid_agent_skill_is_not_reported(tmp_path: Path) -> None:
    write(tmp_path / ".agents/skills/deploy-2/SKILL.md", skill("deploy-2", "x" * 1024) + "license: MIT\n")
    write(tmp_path / ".agents/skills/deploy-2/references/SKILL.md", "# A template, not a skill\n")
    write(tmp_path / ".agents/skills/deploy-2/scripts/run.sh", "echo\n")
    assert lines(tmp_path) == []


def test_a_configured_root_is_not_checked_unless_listed(tmp_path: Path) -> None:
    write(tmp_path / "skills/deploy/SKILL.md", "# Not frontmatter\n")
    assert lines(tmp_path) == []


def test_one_name_in_two_roots_is_not_reported(tmp_path: Path) -> None:
    for root in (".claude/skills", ".agents/skills", ".github/skills"):
        write(tmp_path / root / "deploy" / "SKILL.md", skill())
    assert lines(tmp_path, STRICT) == []


# ── strict ───────────────────────────────────────────────────────────────


def test_strict_holds_claude_skills_to_the_agent_skills_rules(tmp_path: Path) -> None:
    write(tmp_path / ".claude/skills/deploy/SKILL.md", skill("release"))
    assert lines(tmp_path, STRICT) == [
        ".claude/skills/deploy/SKILL.md:2: skill name 'release' does not match its directory 'deploy' (Agent Skills)"
    ]


@pytest.mark.parametrize("root", [".claude/skills", ".agents/skills", ".github/skills"])
def test_strict_requires_a_skill_md_in_every_directory(tmp_path: Path, root: str) -> None:
    write(tmp_path / root / "deploy" / "SKILL.md", skill())
    write(tmp_path / root / "notes" / "README.md", "# Notes\n")
    write(tmp_path / root / "lower" / "skill.md", skill("lower"))
    write(tmp_path / root / "group" / "inner" / "SKILL.md", skill("inner"))
    write(tmp_path / root / "README.md", "# Skills\n")
    assert lines(tmp_path, STRICT) == [
        f"{root}/group:1: skill directory 'group' has no SKILL.md (strict)",
        f"{root}/lower:1: skill directory 'lower' has no SKILL.md (strict)",
        f"{root}/notes:1: skill directory 'notes' has no SKILL.md (strict)",
    ]
    assert lines(tmp_path) == []


def test_yaml_1_1_booleans_are_names_not_booleans(tmp_path: Path) -> None:
    for name in ("on", "no", "y"):
        write(tmp_path / ".agents/skills" / name / "SKILL.md", skill(name))
    assert lines(tmp_path) == []


def test_an_unquoted_glob_in_a_skills_paths_is_reported_only_under_strict(tmp_path: Path) -> None:
    write(tmp_path / ".claude/skills/deploy/SKILL.md", "---\nname: deploy\ndescription: x\npaths: **/*.py\n---\n")
    assert lines(tmp_path) == []
    assert lines(tmp_path, STRICT) == [
        ".claude/skills/deploy/SKILL.md:4: invalid YAML frontmatter: expected alphabetic or numeric character, "
        "but found '*'; Claude Code recovers by quoting the value"
    ]
