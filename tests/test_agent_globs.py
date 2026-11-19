"""Glob matching under the most lenient reading any known reader uses: a
verdict is ``no_match`` only when no reading matches a file or directory."""

from __future__ import annotations

import time

import pytest

from stack_intel.agent_globs import glob_groups, glob_verdict, literal_directory, sample_paths, split_globs

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
        "packages/web/src/a.ts",
        "web/src/components/a.tsx",
    }
)
DIRS = frozenset({"docs", "docs/deep", "src", "src/api", "src/ui", "app", "app/[id]", "packages/web/src"})
PATHS = FILES | DIRS


@pytest.mark.parametrize(
    "glob",
    [
        "*.md",
        "guide.md",
        "/*.md",
        "/docs/guide.md",
        "docs/*/notes.md",
        "src/**/*.py",
        "src/**",
        "src/ui/view.?s",
        "src/ui/*.{ts,tsx}",
        "src/ui/*.{ js, ts }",
        "src/{api,web}/*.py",
        "src/{api}/handler.py",
        "src/[au]pi/handler.py",
        "src/u[a-j]/**",
        "app/[id]/page.tsx",
        "src/ui/",
        "./docs/*.md",
        "././docs/*.md",
        "**/*.yml",
        "**/*.TS",
        "README.MD",
        "SRC/API/**",
        "src/api",
        "src/components/*.tsx",
        "*.py",
        "*",
        "**",
        "**/*",
        "**/**/**/*.md",
    ],
)
def test_globs_some_reader_matches(glob: str) -> None:
    assert glob_verdict(glob, PATHS) == "matches"


@pytest.mark.parametrize(
    "glob",
    [
        "*.rs",
        "/guide.md",
        "/src/*.ts",
        "src/ui/*.{js,jsx}",
        "src/{web,lib}/**",
        "src/[!a]pi/**",
        "app/[xy]/page.tsx",
        "src/renamed/**",
        "src/api/[id",
        "src/[z-a]/handler.py",
        "lib",
    ],
)
def test_globs_no_reader_matches(glob: str) -> None:
    assert glob_verdict(glob, PATHS) == "no_match"


def test_a_trailing_globstar_matches_the_directory_itself() -> None:
    assert glob_verdict("empty/**", frozenset({"empty"})) == "matches"
    assert glob_verdict("src/**", frozenset({"packages/web/src/a.ts"})) == "matches"


@pytest.mark.parametrize(
    "glob",
    ["!src/**", "src/@(api|ui)/**", "src/+(api)/**", "src/\\*.py", "src/[[:alpha:]]pi/**", "src/{1..3}/**", "x" * 300],
)
def test_unmodelled_constructs_skip_the_existence_check(glob: str) -> None:
    assert glob_verdict(glob, PATHS) == "unmodelled"


@pytest.mark.parametrize(
    "glob",
    ["a/**/b/**/c/**/d/**/e/**/zz", "*a*b*c*d*e*/zz", "x/" + "{a,b,c,d}" * 4],
)
def test_globs_past_the_work_bound_are_unmodelled(glob: str) -> None:
    assert glob_verdict(glob, PATHS) == "unmodelled"


def test_repeated_globstars_collapse_and_stay_fast() -> None:
    deep = "/".join(f"d{i}" for i in range(17)) + "/file"
    started = time.perf_counter()
    assert glob_verdict("**/" * 25 + "zz", frozenset({deep})) == "no_match"
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    "glob", ["src/[z-a].ts", "src/[a-[]", "[", "]", "[]]", "[!]", "a[--z]", "x[a&&b]", "y[a~~b]", "[^]", "{,}", "{"]
)
def test_no_glob_raises(glob: str) -> None:
    assert glob_verdict(glob, PATHS) in ("matches", "no_match", "unmodelled")


@pytest.mark.parametrize(
    ("value", "groups"),
    [
        ("src/**, lib/**", [("src/**", (("src/**", False),)), ("lib/**", (("lib/**", False),))]),
        ("src/**,,lib/**, ", [("src/**", (("src/**", False),)), ("lib/**", (("lib/**", False),))]),
        (
            "src/*.{ts,tsx}, lib/**",
            [("src/*.{ts,tsx}", (("src/*.{ts,tsx}", True),)), ("lib/**", (("lib/**", False),))],
        ),
        ("src/[a,b].ts", [("src/[a,b].ts", (("src/[a,b].ts", True),))]),
        ("src/*.{ts}", [("src/*.{ts}", (("src/*.{ts}", False),))]),
        (
            ["src/**/*.ts, lib/**/*.ts "],
            [("src/**/*.ts, lib/**/*.ts", (("src/**/*.ts", False), ("lib/**/*.ts", False)))],
        ),
        ([["src/**"], 3, None, " "], [("src/**", (("src/**", False),))]),
        (3, []),
        (None, []),
    ],
)
def test_glob_groups_flatten_split_and_drop(value: object, groups: list[object]) -> None:
    assert glob_groups(value) == groups


def test_comma_strings_split_outside_braces_and_brackets() -> None:
    assert split_globs("src/[a,b].ts, x/*.{a,b}, y") == ["src/[a,b].ts", "x/*.{a,b}", "y"]


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
