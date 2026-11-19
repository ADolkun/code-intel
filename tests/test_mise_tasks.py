"""The mise reader (``mise_tasks``): which files configure a directory, the
names file tasks answer to, what a config defines and what makes it
unprovable; and, through the docs check, how mise references resolve,
monorepo references included."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from doc_config import doc_config
from fake_repo_files import repo_files_on_disk

from stack_intel.doc_refs import build_report
from stack_intel.mise_tasks import MiseFile, config_owner, file_task_names, mise_file_from_bytes, task_dir_entry


@pytest.mark.parametrize(
    ("rel", "owner"),
    [
        ("mise.toml", ""),
        (".mise.toml", ""),
        ("mise/config.toml", ""),
        (".mise/config.toml", ""),
        (".config/mise.toml", ""),
        (".config/mise/mise.toml", ""),
        (".config/mise/config.toml", ""),
        ("mise.local.toml", ""),
        (".mise.local.toml", ""),
        (".config/mise/config.local.toml", ""),
        ("mise.dev.toml", ""),
        (".mise.ci.toml", ""),
        ("mise/conf.d/tools.toml", ""),
        (".mise/conf.d/tools.toml", ""),
        (".config/mise/conf.d/tools.toml", ""),
        ("backend/mise.toml", "backend"),
        ("apps/web/.config/mise/config.toml", "apps/web"),
        ("mise-tasks/build.toml", None),
        ("mise/tasks/build.toml", None),
        ("docs/mise.md", None),
        ("pyproject.toml", None),
    ],
)
def test_config_owner(rel: str, owner: str | None) -> None:
    assert config_owner(rel) == owner


@pytest.mark.parametrize(
    ("rel", "entry"),
    [
        ("mise-tasks/build", ("", "build")),
        (".mise-tasks/build", ("", "build")),
        ("mise/tasks/db/migrate.sh", ("", "db/migrate.sh")),
        (".mise/tasks/build", ("", "build")),
        (".config/mise/tasks/build", ("", "build")),
        ("backend/mise-tasks/test", ("backend", "test")),
        ("scripts/build.sh", None),
    ],
)
def test_task_dir_entry(rel: str, entry: tuple[str, str] | None) -> None:
    assert task_dir_entry(rel) == entry


@pytest.mark.parametrize(
    ("path", "names"),
    [
        ("build", {"build"}),
        ("build.sh", {"build.sh", "build"}),
        ("db/migrate.sh", {"db:migrate.sh", "db:migrate"}),
        ("a/b/c.sh", {"a:b:c.sh", "a:b:c"}),
        ("test/_default", {"test:_default", "test"}),
    ],
)
def test_file_task_names(path: str, names: set[str]) -> None:
    assert file_task_names(path) == names


def _parse(text: str, rel: str = "mise.toml") -> MiseFile:
    parsed, problems = mise_file_from_bytes(rel, text.encode())
    assert parsed is not None, problems
    return parsed


def test_a_config_defines_its_tasks_and_their_aliases() -> None:
    parsed = _parse(
        """
        [tasks.build]
        alias = "b"
        run = "make"

        [tasks."db:migrate"]
        alias = ["m", "mig"]
        run = "x"

        [tasks]
        lint = "ruff check"

        [monorepo.path_aliases]
        api = "services/api"
        """.replace("\n        ", "\n")
    )
    assert parsed is not None
    assert parsed.tasks == {"build", "b", "db:migrate", "m", "mig", "lint"}
    assert parsed.opaque is None
    assert dict(parsed.path_aliases) == {"api": "services/api"}


def test_a_task_file_defines_its_top_level_tables() -> None:
    parsed = _parse('task1 = "echo 1"\n[task2]\nrun = "echo 2"\nalias = "t2"\n', rel="tasks/extra.toml")
    assert parsed.tasks == {"task1", "task2", "t2"}


def test_local_includes_are_kept() -> None:
    parsed = _parse('[task_config]\nincludes = ["tasks.toml", "scripts", "more/*.toml"]\n')
    assert parsed.includes == ("tasks.toml", "scripts", "more/*.toml")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            '[task_config]\nincludes = ["git::https://example.com/tasks.git//t.toml"]\n',
            "mise.toml includes the remote tasks 'git::https://example.com/tasks.git//t.toml'",
        ),
        ('[task_config]\nincludes = ["oci::ghcr.io/x/tasks:1"]\n', "mise.toml includes the remote tasks"),
        ('[task_config]\nincludes = ["zip::https://example.com/t.zip"]\n', "mise.toml includes the remote tasks"),
        (
            '[task_config]\nincludes = ["{{ config_root }}/tasks"]\n',
            "mise.toml includes '{{ config_root }}/tasks', outside what the docs check reads",
        ),
        ('[task_config]\nincludes = ["~/tasks"]\n', "mise.toml includes '~/tasks', outside"),
        ('[task_templates.base]\nrun = "x"\n', "mise.toml defines task templates"),
        ('[tasks.build]\nextends = "base"\n', "task 'build' in mise.toml extends another task"),
    ],
)
def test_opaque_reasons(text: str, reason: str) -> None:
    assert str(_parse(text).opaque).startswith(reason)


def test_a_config_that_is_not_toml_is_a_parse_diagnostic() -> None:
    parsed, problems = mise_file_from_bytes("mise.toml", b"[tasks\n")
    assert parsed is None
    assert [(p.kind, p.file) for p in problems] == [("parse_failed", "mise.toml")]


MONOREPO = {
    "mise.toml": 'monorepo_root = true\n[monorepo]\nconfig_roots = ["backend/", "frontend/"]\n'
    '[monorepo.path_aliases]\nbe = "backend"\n[tasks.release]\nrun = "x"\n',
    "backend/mise.toml": '[tasks.install]\nalias = "i"\nrun = "uv sync"\n[tasks.test]\nrun = "pytest"\n',
    "frontend/mise.toml": '[tasks.build]\nrun = "npm run build"\n[tasks."frontend:literal"]\nrun = "x"\n',
    "frontend/mise-tasks/check/_default": "#!/bin/sh\n",
}
COMMANDS_ONLY = {"scope": ["**/*.md"], "reachability": None, "rules": [], "ignore": []}


def _results(tmp_path: Path, doc: str, text: str, files: Mapping[str, str] = MONOREPO) -> list[tuple[str, str, str]]:
    for rel, content in {**files, doc: text}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    cfg = doc_config(COMMANDS_ONLY)
    report = build_report(repo_files_on_disk(tmp_path), [], [], [], [], True, cfg)
    return [(r.text, r.status, r.detail or "") for r in report.docs[doc].references if r.kind == "command"]


@pytest.mark.parametrize(
    ("doc", "command", "status", "detail"),
    [
        ("README.md", "mise //backend:install", "ok", "defined in backend/mise.toml"),
        ("README.md", "mise //backend:i", "ok", "defined in backend/mise.toml"),
        ("README.md", "mise //be:test", "ok", "defined in backend/mise.toml"),
        ("README.md", "mise backend:install", "ok", "defined in backend/mise.toml"),
        ("README.md", "mise be:test", "ok", "defined in backend/mise.toml"),
        ("frontend/README.md", "mise frontend:literal", "ok", "defined in frontend/mise.toml"),
        (
            "README.md",
            "mise frontend:literal",
            "not_checked",
            "defined only in frontend/mise.toml; the doc names no directory",
        ),
        ("README.md", "mise frontend:check", "ok", "defined in frontend/mise-tasks/check/_default"),
        ("README.md", "mise run release", "ok", "defined in mise.toml"),
        ("README.md", "mise :release", "ok", "defined in mise.toml"),
        ("backend/README.md", "mise :test", "ok", "defined in backend/mise.toml"),
        ("backend/README.md", "mise run test", "ok", "defined in backend/mise.toml"),
        ("backend/README.md", "mise run release", "ok", "defined in mise.toml"),
        ("README.md", "mise -C backend run test", "ok", "defined in backend/mise.toml"),
        ("README.md", "cd backend && mise run test", "ok", "defined in backend/mise.toml"),
        (
            "README.md",
            "mise backend:tset",
            "missing",
            "no task 'tset' in backend/mise.toml and no task 'backend:tset' visible from the repository root",
        ),
        ("README.md", "mise //backend:tset", "missing", "no task 'tset' in backend/mise.toml"),
        ("README.md", "mise //frontend:test", "missing", "no task 'test' in frontend/mise.toml"),
        ("README.md", "mise -C backend run build", "missing", "no task 'build' visible from backend"),
        (
            "README.md",
            "mise run test",
            "not_checked",
            "defined only in backend/mise.toml; the doc names no directory",
        ),
        ("README.md", "mise run nope", "missing", "no task 'nope' visible from the repository root"),
        (
            "docs/dev.md",
            "mise run nope",
            "missing",
            "no task 'nope' visible from the repository root or docs",
        ),
        ("README.md", "mise -C nowhere run test", "skipped", "no directory nowhere in the repository"),
        ("README.md", "mise //...:install", "skipped", "a wildcard or a placeholder"),
    ],
)
def test_monorepo_references(tmp_path: Path, doc: str, command: str, status: str, detail: str) -> None:
    assert _results(tmp_path, doc, f"`{command}`\n") == [(command.split("&& ")[-1], status, detail)]


def test_without_config_roots_every_config_directory_counts(tmp_path: Path) -> None:
    files = {
        "mise.toml": "monorepo_root = true\n",
        "services/api/mise.toml": '[tasks.serve]\nrun = "x"\n',
    }
    assert _results(tmp_path, "README.md", "`mise //services/api:serve` `mise services/api:serve`\n", files) == [
        ("mise //services/api:serve", "ok", "defined in services/api/mise.toml"),
        ("mise services/api:serve", "ok", "defined in services/api/mise.toml"),
    ]


def test_every_config_file_location_and_file_task_directory_counts(tmp_path: Path) -> None:
    files = {
        ".mise.toml": '[tasks.a]\nrun = "x"\n',
        "mise/config.toml": '[tasks.b]\nrun = "x"\n',
        ".mise/config.toml": '[tasks.c]\nrun = "x"\n',
        ".config/mise.toml": '[tasks.d]\nrun = "x"\n',
        ".config/mise/config.toml": '[tasks.e]\nrun = "x"\n',
        "mise.local.toml": '[tasks.f]\nrun = "x"\n',
        "mise.ci.toml": '[tasks.g]\nrun = "x"\n',
        ".config/mise/conf.d/extra.toml": '[tasks.h]\nrun = "x"\n',
        "mise-tasks/i.sh": "#!/bin/sh\n",
        ".mise-tasks/j": "#!/bin/sh\n",
        "mise/tasks/k": "#!/bin/sh\n",
        ".mise/tasks/l": "#!/bin/sh\n",
        ".config/mise/tasks/grp/m": "#!/bin/sh\n",
        "mise-tasks/more.toml": 'n = "echo n"\n',
    }
    names = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "grp:m", "n"]
    text = " ".join(f"`mise run {name}`" for name in names) + "\n"
    assert {status for _, status, _ in _results(tmp_path, "README.md", text, files)} == {"ok"}


def test_includes_define_tasks_from_toml_files_directories_and_globs(tmp_path: Path) -> None:
    files = {
        "mise.toml": '[task_config]\nincludes = ["ci/tasks.toml", "tools/tasks", "extra/*.toml"]\n',
        "ci/tasks.toml": 'deploy = "echo"\n',
        "tools/tasks/sub/gen.sh": "#!/bin/sh\n",
        "extra/one.toml": 'one = "echo"\n',
    }
    text = "`mise run deploy` `mise run sub:gen` `mise run one` `mise run two`\n"
    assert _results(tmp_path, "README.md", text, files) == [
        ("mise run deploy", "ok", "defined in ci/tasks.toml"),
        ("mise run one", "ok", "defined in extra/one.toml"),
        ("mise run sub:gen", "ok", "defined in tools/tasks/sub/gen.sh"),
        ("mise run two", "missing", "no task 'two' visible from the repository root"),
    ]


@pytest.mark.parametrize(
    ("config", "detail"),
    [
        (
            '[task_config]\nincludes = ["git::https://example.com/t.git//a.toml"]\n',
            "mise.toml includes the remote tasks 'git::https://example.com/t.git//a.toml', "
            "so its tasks are not all known",
        ),
        ('[tasks.a]\nextends = "b"\n', "task 'a' in mise.toml extends another task, so its tasks are not all known"),
        ('[task_templates.t]\nrun = "x"\n', "mise.toml defines task templates, so its tasks are not all known"),
    ],
)
def test_an_opaque_config_leaves_a_missing_task_unchecked(tmp_path: Path, config: str, detail: str) -> None:
    assert _results(tmp_path, "README.md", "`mise run nope`\n", {"mise.toml": config}) == [
        ("mise run nope", "not_checked", detail)
    ]


def test_a_config_that_does_not_parse_leaves_its_tasks_unknown(tmp_path: Path) -> None:
    files = {"mise.toml": "[tasks\n"}
    assert _results(tmp_path, "README.md", "`mise run build`\n", files) == [
        ("mise run build", "skipped", "mise.toml could not be read")
    ]


def test_a_repository_without_mise_files_has_no_mise_references(tmp_path: Path) -> None:
    assert _results(tmp_path, "README.md", "`mise run build` `mise backend:test`\n", {"x.txt": ""}) == []


@pytest.mark.parametrize(
    ("header", "aliases", "opaque"),
    [
        ('#MISE alias="b"\n', {"b"}, None),
        ('# MISE alias=["b", "bb"]\n', {"b", "bb"}, None),
        ('// [MISE] alias="b"\n', {"b"}, None),
        (':: MISE alias="b"\n', {"b"}, None),
        ('#MISE depends=[\n#MISE   "lint",\n#MISE ]\n#MISE alias="b"\n', {"b"}, None),
        ("# NOMISE alias='b'\n", set(), None),
        ("#MISE alias=\n", set(), "the #MISE header of mise-tasks/build is not TOML the docs check can read"),
        ('#MISE extends="base"\n', set(), "file task mise-tasks/build extends another task"),
        ('-- MISE alias="a7"\n', {"a7"}, None),
        ('echo "$MISE_PROJECT_ROOT"\n# uses MISE settings\n', set(), None),
        (';; MISE alias="b"\n', set(), "mise-tasks/build has a MISE header line the docs check cannot read"),
    ],
)
def test_a_file_task_header_declares_aliases(header: str, aliases: set[str], opaque: str | None) -> None:
    parsed = _parse("#!/bin/sh\n" + header + "echo build\n", rel="mise-tasks/build")
    assert (set(parsed.tasks), parsed.opaque) == (aliases, opaque)


def test_a_binary_file_task_is_read_without_a_diagnostic() -> None:
    parsed, problems = mise_file_from_bytes("mise-tasks/tool", b"\x7fELF\xff\xfe\x00")
    assert (parsed, problems) == (MiseFile(frozenset()), [])


def test_a_file_task_alias_resolves(tmp_path: Path) -> None:
    files = {"mise.toml": "", "mise-tasks/build.sh": '#!/bin/sh\n#MISE alias="b"\ncargo build\n'}
    assert _results(tmp_path, "README.md", "`mise run b` `mise run c`\n", files) == [
        ("mise run b", "ok", "defined in mise-tasks/build.sh"),
        ("mise run c", "missing", "no task 'c' visible from the repository root"),
    ]
