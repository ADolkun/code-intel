"""Glob matching as Claude Code reads ``paths`` and Copilot reads ``applyTo``:
a verdict is ``no_match`` only when no reading matches a file."""

from __future__ import annotations

import pytest

from stack_intel.agent_globs import (
    claude_glob,
    copilot_glob,
    has_unclosed_bracket,
    literal_directory,
    sample_paths,
    split_globs,
)

FILES = frozenset(
    {
        "README.md",
        "docs/guide.md",
        "docs/deep/notes.md",
        "src/api/handler.py",
        "src/ui/view.tsx",
        "src/ui/view.ts",
        "app/[id]/page.tsx",
        ".github/workflows/ci.yml",
    }
)


@pytest.mark.parametrize(
    ("glob", "verdict"),
    [
        ("*.md", "matches"),
        ("guide.md", "matches"),
        ("notes.md", "matches"),
        ("*.rs", "no_match"),
        ("/*.md", "matches"),
        ("/guide.md", "no_match"),
        ("/docs/guide.md", "matches"),
        ("docs/*.md", "matches"),
        ("docs/*/notes.md", "matches"),
        ("src/*.py", "no_match"),
        ("src/**/*.py", "matches"),
        ("src/**", "matches"),
        ("src/**/handler.py", "matches"),
        ("docs/**/guide.md", "matches"),
        ("src/ui/view.?s", "matches"),
        ("src/ui/view.??x", "matches"),
        ("src/ui/*.{ts,tsx}", "matches"),
        ("src/ui/*.{js,jsx}", "no_match"),
        ("src/{api,web}/*.py", "matches"),
        ("src/{web,lib}/**", "no_match"),
        ("src/{api}/handler.py", "matches"),
        ("src/[au]pi/handler.py", "matches"),
        ("src/[!a]pi/**", "no_match"),
        ("src/[^u]i/**", "no_match"),
        ("src/u[a-j]/**", "matches"),
        ("app/[id]/page.tsx", "matches"),
        ("app/[xy]/page.tsx", "no_match"),
        ("src/api/[id", "invalid"),
        ("src/ui/", "matches"),
        ("./docs/*.md", "matches"),
        (".github/**", "matches"),
        ("**/*.yml", "matches"),
    ],
)
def test_claude_code_semantics(glob: str, verdict: str) -> None:
    assert claude_glob(glob, FILES) == verdict


@pytest.mark.parametrize(
    "glob",
    ["!src/**", "src/@(api|ui)/**", "src/+(api)/**", "src/\\*.py", "src/[[:alpha:]]pi/**", "src/{1..3}/**"],
)
def test_unmodelled_constructs_skip_the_existence_check(glob: str) -> None:
    assert claude_glob(glob, FILES) == "unmodelled"
    assert copilot_glob(glob, FILES) == "unmodelled"


def test_braces_from_a_comma_split_string_are_unmodelled() -> None:
    assert claude_glob("nowhere/*.{a,b}", FILES, split=True) == "unmodelled"
    assert copilot_glob("nowhere/*.{a,b}", FILES, split=True) == "unmodelled"
    assert copilot_glob("nowhere/*.{a,b}", FILES) == "no_match"


@pytest.mark.parametrize(
    ("glob", "verdict"),
    [
        ("**", "matches"),
        ("*.md", "matches"),
        ("*.py", "no_match"),
        ("guide.md", "no_match"),
        ("**/*.py", "matches"),
        ("src/**/*.py", "matches"),
        ("src/*.py", "no_match"),
        ("/src/**", "matches"),
        ("src/ui/*.{ts,tsx}", "matches"),
        ("app/[id]/page.tsx", "matches"),
        ("lib/**", "no_match"),
    ],
)
def test_copilot_semantics(glob: str, verdict: str) -> None:
    assert copilot_glob(glob, FILES) == verdict


def test_an_unclosed_bracket_is_not_invalid_for_copilot() -> None:
    assert copilot_glob("src/api/[id", FILES) == "no_match"
    assert copilot_glob("app/[id", frozenset({"app/[id"})) == "matches"


@pytest.mark.parametrize(
    ("glob", "unclosed"),
    [("a[b", True), ("a[]", True), ("a[!]", True), ("a[]]", False), ("a[!]]", False), ("a[b]c[", True), ("abc", False)],
)
def test_unclosed_brackets(glob: str, unclosed: bool) -> None:
    assert has_unclosed_bracket(glob) is unclosed


@pytest.mark.parametrize(
    ("value", "globs"),
    [
        ("src/**, lib/**", ["src/**", "lib/**"]),
        ("src/**,,lib/**, ", ["src/**", "lib/**"]),
        ("src/*.{ts,tsx}, lib/**", ["src/*.{ts,tsx}", "lib/**"]),
        ("**", ["**"]),
    ],
)
def test_comma_strings_split_outside_braces(value: str, globs: list[str]) -> None:
    assert split_globs(value) == globs


def test_too_many_brace_expansions_skip_the_check() -> None:
    glob = "x/" + "{a,b,c,d}" * 6
    assert claude_glob(glob, FILES) == "unmodelled"


@pytest.mark.parametrize(("glob", "unclosed"), [("photos \\[2024/**", False), ("a\\[b[c", True), ("a\\\\[b", True)])
def test_escaped_brackets_do_not_open_a_class(glob: str, unclosed: bool) -> None:
    assert has_unclosed_bracket(glob) is unclosed


def test_the_escaped_bracket_example_is_unmodelled_not_invalid() -> None:
    assert claude_glob("photos \\[2024/**", FILES) == "unmodelled"


def test_brackets_in_a_comma_string_do_not_split_and_are_unmodelled() -> None:
    assert split_globs("src/[a,b].ts, lib/**") == ["src/[a,b].ts", "lib/**"]
    assert claude_glob("src/[a,b].ts", FILES, split=True) == "unmodelled"
    assert copilot_glob("src/[a,b].ts", FILES, split=True) == "unmodelled"


@pytest.mark.parametrize(
    ("glob", "claude", "copilot"),
    [
        ("src/[z-a].ts", "invalid", "unmodelled"),
        ("src/[a-[]", "invalid", "unmodelled"),
        ("x[a&&b]", "no_match", "no_match"),
    ],
)
def test_a_class_re_cannot_compile_does_not_raise(glob: str, claude: str, copilot: str) -> None:
    assert claude_glob(glob, FILES) == claude
    assert copilot_glob(glob, FILES) == copilot


def test_a_glob_naming_a_directory_matches_a_listed_directory() -> None:
    assert claude_glob("src/api", FILES | {"src", "src/api"}) == "matches"
    assert claude_glob("src/api", FILES) == "no_match"


@pytest.mark.parametrize(
    ("glob", "directory", "samples"),
    [
        ("gen/**/*.ts", "gen", ["gen/.ts", "gen/x.ts"]),
        ("/src/gen/*.py", "src/gen", ["src/gen/.py", "src/gen/x.py"]),
        ("**/.env*", "", [".env", ".envx"]),
        ("src/[ab]/?.md", "src", ["src/a/x.md"]),
        ("src/api", "src", ["src/api"]),
    ],
)
def test_literal_directory_and_sample_paths(glob: str, directory: str, samples: list[str]) -> None:
    assert literal_directory(glob) == directory
    assert sample_paths(glob) == samples
