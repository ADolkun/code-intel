"""Command references end to end, one case per form whose verdict real npm,
pnpm, GNU make or mise settles: a repository on disk, every Markdown file in
scope, and the status (and where it matters, the reason) of each reference."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from doc_config import doc_config
from fake_repo_files import repo_files_on_disk

from stack_intel import package_manifest
from stack_intel.definition_files import Unreadable
from stack_intel.doc_refs import build_report

ALL_DOCS = {"scope": ["**/*.md"], "reachability": None, "rules": [], "ignore": []}


def _write(tmp_path: Path, files: Mapping[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def _commands(tmp_path: Path, files: Mapping[str, str | bytes]) -> list[tuple[str, str, str, str]]:
    _write(tmp_path, files)
    report = build_report(repo_files_on_disk(tmp_path), [], [], [], [], True, doc_config(ALL_DOCS))
    assert report.complete
    assert report.diagnostics == []
    return sorted(
        (doc, r.text, r.status, r.detail or "")
        for doc, entry in report.docs.items()
        for r in entry.references
        if r.kind == "command"
    )


def _statuses(tmp_path: Path, files: Mapping[str, str | bytes]) -> list[tuple[str, str, str]]:
    return [(doc, text, status) for doc, text, status, _ in _commands(tmp_path, files)]


def _task(*names: str) -> str:
    return "".join(f'[tasks.{name}]\nrun = "true"\n' for name in names)


def _scripts(*names: str) -> str:
    return '{"scripts": {' + ", ".join(f'"{name}": "true"' for name in names) + "}}"


CASES = [
    pytest.param(
        {
            "mise.toml": '[task_config]\nincludes = ["scripts"]\n',
            "scripts/deploy.sh": "#!/bin/sh\n",
            "scripts/package.json": _scripts("lint"),
            "README.md": "`mise run deploy` then `npm run lint`\n",
        },
        [("README.md", "mise run deploy", "ok"), ("README.md", "npm run lint", "not_checked")],
        id="C1-one-file-read-as-a-mise-task-and-a-package-json",
    ),
    pytest.param(
        {
            "mise.toml": '[task_config]\nincludes = ["scripts"]\n',
            "scripts/deploy.sh": "#!/bin/sh\n",
            "scripts/Makefile": "lint:\n",
            "README.md": "`mise run deploy` then `make -C scripts lint`\n",
        },
        [("README.md", "make -C scripts lint", "ok"), ("README.md", "mise run deploy", "ok")],
        id="C1-one-file-read-as-a-mise-task-and-a-makefile",
    ),
    pytest.param(
        {
            "Makefile": b"# (c) 2020 M\xfcller\nall:\n\t@true\n",
            "README.md": "`make all`\n",
        },
        [("README.md", "make all", "ok")],
        id="C3-latin1-makefile",
    ),
    pytest.param(
        {
            "Makefile": "all:\n",
            "dist/Makefile": "srpm:\n",
            "README.md": "`make srpm`\n",
            "dist/README.md": "`make srpm`\n",
        },
        [("README.md", "make srpm", "not_checked"), ("dist/README.md", "make srpm", "ok")],
        id="M1-tracked-makefile-under-a-skip-dir",
    ),
    pytest.param(
        {
            "e2e/Makefile": "other:\n",
            "frontend/e2e/Makefile": "smoke:\n",
            "frontend/README.md": "`cd e2e && make smoke` `make -C e2e smoke` `make -C e2e nope`\n",
        },
        [
            ("frontend/README.md", "make -C e2e nope", "missing"),
            ("frontend/README.md", "make -C e2e smoke", "ok"),
            ("frontend/README.md", "make smoke", "ok"),
        ],
        id="M2-make-directory-from-the-doc",
    ),
    pytest.param(
        {
            "e2e/package.json": _scripts("other"),
            "frontend/e2e/package.json": _scripts("smoke"),
            "frontend/README.md": "`npm run smoke --prefix e2e` `cd e2e && pnpm run smoke`\n",
        },
        [("frontend/README.md", "npm run smoke --prefix e2e", "ok"), ("frontend/README.md", "pnpm run smoke", "ok")],
        id="M2-package-directory-from-the-doc",
    ),
    pytest.param(
        {
            "e2e/mise.toml": _task("other"),
            "frontend/e2e/mise.toml": _task("smoke"),
            "frontend/README.md": "`mise -C e2e run smoke` `cd e2e && mise run smoke`\n",
        },
        [("frontend/README.md", "mise -C e2e run smoke", "ok"), ("frontend/README.md", "mise run smoke", "ok")],
        id="M2-mise-directory-from-the-doc",
    ),
    pytest.param(
        {
            ".config/mise/mise.local.toml": _task("lint"),
            "backend/mise.toml": _task("other"),
            "README.md": "`cd backend && mise run lint` `mise -C backend run lint`\n",
        },
        [("README.md", "mise -C backend run lint", "ok"), ("README.md", "mise run lint", "ok")],
        id="M3-config-mise-local-configures-the-root",
    ),
    pytest.param(
        {
            "mise.toml": _task("build", "test", "a"),
            "backend/mise.toml": _task("bt"),
            "docker/x.txt": "",
            "README.md": "`mise run docker:build` `mise run backend:test` `mise run //backend:bt` `mise run :a`\n",
        },
        [
            ("README.md", "mise run //backend:bt", "not_checked"),
            ("README.md", "mise run :a", "not_checked"),
            ("README.md", "mise run backend:test", "missing"),
            ("README.md", "mise run docker:build", "missing"),
        ],
        id="O1-no-monorepo-root",
    ),
    pytest.param(
        {
            "mise.toml": 'monorepo_root = true\n[monorepo]\nconfig_roots = ["backend", "frontend"]\n' + _task("a"),
            "backend/mise.toml": _task("bt"),
            "frontend/mise.toml": _task("ft"),
            "libs/x/mise.toml": _task("xt"),
            "README.md": (
                "`mise run //backend:a` `mise run //libs/x:xt` `mise run libs/x:xt` `cd backend && mise run a`\n"
                "`mise run //backend:bt` `mise run backend:bt` `mise run a` `mise run :a`\n"
            ),
        },
        [
            ("README.md", "mise run //backend:a", "missing"),
            ("README.md", "mise run //backend:bt", "ok"),
            ("README.md", "mise run //libs/x:xt", "missing"),
            ("README.md", "mise run :a", "ok"),
            ("README.md", "mise run a", "missing"),
            ("README.md", "mise run a", "ok"),
            ("README.md", "mise run backend:bt", "ok"),
            ("README.md", "mise run libs/x:xt", "missing"),
        ],
        id="O1-monorepo-root-with-config-roots",
    ),
    pytest.param(
        {
            "mise.toml": 'experimental_monorepo_root = true\n[monorepo]\nconfig_roots = ["pk/*"]\n',
            "pk/one/mise.toml": _task("ot"),
            "backend/mise.toml": _task("bt"),
            "README.md": "`mise run //pk/one:ot` `mise run //backend:bt`\n",
        },
        [("README.md", "mise run //backend:bt", "missing"), ("README.md", "mise run //pk/one:ot", "ok")],
        id="O1-config-root-patterns",
    ),
    pytest.param(
        {
            "mise.toml": '[task_config]\nincludes = ["more"]\n',
            "mise-tasks/deflt": "#!/bin/sh\n",
            "more/inct": "#!/bin/sh\n",
            "README.md": "`mise run deflt` `mise run inct`\n",
        },
        [("README.md", "mise run deflt", "missing"), ("README.md", "mise run inct", "ok")],
        id="O2-includes-replace-the-default-task-directories",
    ),
    pytest.param(
        {
            "mise.toml": _task("build"),
            "mise.production.toml": _task("deploy"),
            "README.md": (
                "`mise run deploy` `MISE_ENV=production mise run deploy` `mise -E production run deploy` "
                "`MISE_ENV=staging mise run deploy`\n"
            ),
        },
        [
            ("README.md", "mise -E production run deploy", "ok"),
            ("README.md", "mise run deploy", "not_checked"),
            ("README.md", "mise run deploy", "not_checked"),
            ("README.md", "mise run deploy", "ok"),
        ],
        id="O3-environment-configs",
    ),
    pytest.param(
        {
            "Makefile": (
                ".PHONY: ph\n.PRECIOUS: prec\n.SECONDARY: sec\n.INTERMEDIATE: inter\n.NOTPARALLEL: np\n"
                ".SILENT: sil\nvarx: X = 1\nall:\n"
            ),
            "README.md": "`make ph` `make prec` `make sec` `make inter` `make np` `make sil` `make varx`\n",
        },
        [
            ("README.md", "make inter", "missing"),
            ("README.md", "make np", "missing"),
            ("README.md", "make ph", "ok"),
            ("README.md", "make prec", "missing"),
            ("README.md", "make sec", "missing"),
            ("README.md", "make sil", "missing"),
            ("README.md", "make varx", "missing"),
        ],
        id="O4-special-target-prerequisites-and-target-specific-variables",
    ),
    pytest.param(
        {"web/package.json": _scripts("wbuild"), "README.md": "`npm run wbuild -C web` `npm -C web run wbuild`\n"},
        [("README.md", "npm -C web run wbuild", "ok"), ("README.md", "npm run wbuild -C web", "ok")],
        id="O5-npm-C-is-prefix",
    ),
    pytest.param(
        {
            "package.json": _scripts("build"),
            "README.md": "`npm run nope --if-pres` `npm run nope --if-prese` `npm run nope --ws`\n",
        },
        [
            ("README.md", "npm run nope --if-pres", "not_checked"),
            ("README.md", "npm run nope --if-prese", "not_checked"),
            ("README.md", "npm run nope --ws", "not_checked"),
        ],
        id="abbreviated-npm-options",
    ),
    pytest.param(
        {"Makefile": "all:\n", "README.md": "```sh\nmake all\\\n```\n"},
        [("README.md", "make all", "ok")],
        id="trailing-backslash-at-the-end-of-a-fence",
    ),
    pytest.param(
        {
            "Makefile": "deploy:\nall:\n",
            "README.md": '`make MSG="a \\"b\\" c" deploy` `make CFLAGS="-DX=\\"1\\"" all`\n',
        },
        [("README.md", 'make CFLAGS="-DX=\\"1\\"" all', "ok"), ("README.md", 'make MSG="a \\"b\\" c" deploy', "ok")],
        id="N1-escaped-quote-inside-double-quotes",
    ),
    pytest.param(
        {
            "mise.toml": 'monorepo_root = true\n[monorepo]\nconfig_roots = ["backend", "frontend"]\n',
            "backend/mise.toml": _task("test"),
            "backend/sub/mise.toml": _task("subt"),
            "frontend/mise.toml": _task("build"),
            "libs/x/mise.toml": _task("xt"),
            "README.md": "`cd backend && mise run backend:test` `cd backend && mise run frontend:build`\n",
            "backend/sub/NOTES.md": "`mise run //backend/sub:subt` `mise run backend:test`\n",
            "libs/x/NOTES.md": "`mise run //libs/x:xt`\n",
        },
        [
            ("README.md", "mise run backend:test", "ok"),
            ("README.md", "mise run frontend:build", "missing"),
            ("backend/sub/NOTES.md", "mise run //backend/sub:subt", "ok"),
            ("backend/sub/NOTES.md", "mise run backend:test", "ok"),
            ("libs/x/NOTES.md", "mise run //libs/x:xt", "ok"),
        ],
        id="N2-N3-names-from-inside-a-project",
    ),
    pytest.param(
        {"Makefile": "test:\n", "README.md": "`make te\"st\"` `make 'te'st`\n"},
        [("README.md", "make 'te'st", "ok"), ("README.md", 'make te"st"', "ok")],
        id="quote-concatenation",
    ),
    pytest.param(
        {"Makefile": "all:\n", "docs/x.md": "`make Docs`\n"},
        [("docs/x.md", "make Docs", "not_checked")],
        id="case-insensitive-file-system",
    ),
    pytest.param(
        {
            "Makefile": "all:\n",
            "package.json": _scripts("build"),
            "README.md": "`make target` `make your-target` `npm run script-name` `make task-name`\n",
        },
        [
            ("README.md", "make target", "skipped"),
            ("README.md", "make task-name", "skipped"),
            ("README.md", "make your-target", "skipped"),
            ("README.md", "npm run script-name", "skipped"),
        ],
        id="placeholder-names",
    ),
    pytest.param(
        {
            "mise.toml": "",
            "mise-tasks/notes.md": "notes\n",
            "mise-tasks/build.sh": "#!/bin/sh\n",
            "mise-tasks/odd.zzz": "#!/bin/sh\n",
            "README.md": "`mise run notes` `mise run notes.md` `mise run build` `mise run odd`\n",
        },
        [
            ("README.md", "mise run build", "ok"),
            ("README.md", "mise run notes", "missing"),
            ("README.md", "mise run notes.md", "ok"),
            ("README.md", "mise run odd", "not_checked"),
        ],
        id="mise-file-task-names",
    ),
]


@pytest.mark.parametrize(("files", "expected"), CASES)
def test_command_references(
    tmp_path: Path, files: Mapping[str, str | bytes], expected: list[tuple[str, str, str]]
) -> None:
    assert _statuses(tmp_path, files) == expected


@pytest.mark.parametrize(
    ("files", "detail"),
    [
        pytest.param(
            {"mise.toml": '[tasks]\nbuild = {\n  run = "echo b",\n  description = "x",\n}\n'},
            "mise.toml could not be read: Invalid initial character for a key part (at line 2, column 10)",
            id="C2-toml-1.1-inline-table",
        ),
        pytest.param(
            {"mise.toml": "a = " + "[" * 5000 + "]" * 5000 + "\n"},
            "mise.toml could not be read: nested too deeply to parse",
            id="C4-deep-mise-config",
        ),
        pytest.param(
            {"mise.toml": '[task_config]\nincludes = ["' + "{a,b}" * 22 + '"]\n'},
            "mise.toml includes '"
            + "{a,b}" * 22
            + "', which expands to too many paths, so its tasks are not all known",
            id="brace-explosion-in-includes",
        ),
    ],
)
def test_a_mise_file_that_cannot_be_settled_says_why(tmp_path: Path, files: Mapping[str, str], detail: str) -> None:
    (row,) = _commands(tmp_path, {**files, "README.md": "`mise run build`\n"})
    assert row[2:] == ("not_checked", detail)


@pytest.mark.parametrize(
    ("files", "text", "detail"),
    [
        pytest.param(
            {"package.json": _scripts("other"), ".npmrc": b"if-present=\xff\n"},
            "`npm run build`",
            ".npmrc could not be read: not UTF-8 (byte 0xff at offset 11)",
            id="npmrc-not-utf8",
        ),
        pytest.param(
            {
                "Makefile": "A0 := x\n"
                + "".join(f"A{i} = " + f"$(A{i - 1})" * 8 + "\n" for i in range(1, 13))
                + "$(A12)y:\n",
            },
            "`make nope`",
            "Makefile has variables too large to expand, so its targets are not all known",
            id="exponential-make-variables",
        ),
        pytest.param(
            {
                "Makefile": "A0 :=\n"
                + "".join(f"A{i} = " + f"$(A{i - 1})" * 8 + "\n" for i in range(1, 13))
                + "$(A12)y:\n",
            },
            "`make nope`",
            "Makefile has variables too large to expand, so its targets are not all known",
            id="N5-exponential-empty-make-variables",
        ),
        pytest.param(
            {"mise.toml": _task("build", "test")},
            "`mise run test:e2e`",
            "no task 'test:e2e' visible from the repository root",
            id="mise-detail-names-the-task-as-written",
        ),
    ],
)
def test_the_reason_names_the_file_and_the_cause(
    tmp_path: Path, files: Mapping[str, str | bytes], text: str, detail: str
) -> None:
    (row,) = _commands(tmp_path, {**files, "README.md": text + "\n"})
    assert row[3] == detail


def test_a_package_json_nested_too_deeply_to_parse_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    # How deep json can nest depends on the interpreter build, so the depth
    # failure is raised directly.
    def too_deep(_data: bytes) -> object:
        raise RecursionError

    monkeypatch.setattr(package_manifest.json, "loads", too_deep)
    assert package_manifest.package_scripts_from_bytes("package.json", b"[]") == Unreadable(
        "package.json", "nested too deeply to parse"
    )
