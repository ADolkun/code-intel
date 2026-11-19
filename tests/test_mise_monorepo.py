"""mise monorepo names against what real mise does: each layout below was run
with mise 2026.11.10 (a clean HOME, no global config), running every task
name from every working directory, and ``ok`` lists the names that exited 0
there. The docs check must give ``ok`` for exactly those and ``missing`` for
the rest, except the names a layout lists as ``unsettled``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from doc_config import doc_config
from fake_repo_files import repo_files_on_disk

from stack_intel.doc_refs import build_report

ALL_DOCS = {"scope": ["**/*.md"], "reachability": None, "rules": [], "ignore": []}
FILE_TASK = "#!/bin/sh\ntrue\n"


@dataclass(frozen=True)
class Layout:
    """``ok``: per working directory, the names real mise ran. ``unsettled``:
    names mise did not run that the docs check leaves ``not_checked``
    (a task only an environment config defines)."""

    files: Mapping[str, str]
    tasks: str
    ok: Mapping[str, str]
    unsettled: Mapping[str, str] = field(default_factory=dict)


def _task(*names: str) -> str:
    return "".join(f'[tasks."{name}"]\nrun = "true"\n' for name in names)


def _monorepo(roots: str, *tasks: str, aliases: str = "") -> str:
    return f"monorepo_root = true\n[monorepo]\nconfig_roots = {roots}\n{aliases}" + _task(*tasks)


# L2: config roots `backend` and `frontend/`; `backend/sub` and `libs/x` have
# configs but are not config roots.
L2 = Layout(
    files={
        "mise.toml": _monorepo('["backend", "frontend/"]', "a"),
        "backend/mise.toml": _task("test", "bt"),
        "backend/sub/mise.toml": _task("subt"),
        "backend/src/x": "",
        "frontend/mise.toml": _task("build"),
        "libs/x/mise.toml": _task("xt"),
        "other/x": "",
    },
    tasks="a :a //:a test :test backend:test //backend:test frontend:build //frontend:build subt :subt "
    "//backend/sub:subt backend/sub:subt sub:subt xt :xt //libs/x:xt libs/x:xt x:xt bt",
    ok={
        ".": "a :a //:a backend:test //backend:test frontend:build //frontend:build",
        "backend": "//:a test :test backend:test //backend:test //frontend:build bt",
        "backend/src": "//:a test :test backend:test //backend:test //frontend:build bt",
        "backend/sub": "//:a backend:test //backend:test //frontend:build subt :subt //backend/sub:subt",
        "frontend": "//:a //backend:test frontend:build //frontend:build",
        "libs": "a :a //:a backend:test //backend:test frontend:build //frontend:build",
        "libs/x": "//:a //backend:test //frontend:build xt :xt //libs/x:xt",
        "other": "a :a //:a backend:test //backend:test frontend:build //frontend:build",
    },
)

# L4: glob config roots; mise ignores the `apps/**` entry, and `packages/c`
# has file tasks but no config file.
L4 = Layout(
    files={
        "mise.toml": _monorepo('["packages/*", "apps/**"]', "a"),
        "packages/a/mise.toml": _task("pa"),
        "packages/c/mise-tasks/pc": FILE_TASK,
        "packages/c/inner/x": "",
        "apps/web/mise.toml": _task("w"),
        "other/z/mise.toml": _task("zt"),
    },
    tasks="a :a //:a pa :pa //packages/a:pa packages/a:pa pc :pc //packages/c:pc w :w //apps/web:w "
    "apps/web:w zt :zt //other/z:zt",
    ok={
        ".": "a :a //:a //packages/a:pa //packages/c:pc",
        "packages": "a :a //:a //packages/a:pa //packages/c:pc",
        "packages/a": "//:a pa :pa //packages/a:pa //packages/c:pc",
        "packages/c": "//:a //packages/a:pa pc :pc //packages/c:pc",
        "packages/c/inner": "//:a //packages/a:pa pc :pc //packages/c:pc",
        "apps": "a :a //:a //packages/a:pa //packages/c:pc",
        "apps/web": "//:a //packages/a:pa //packages/c:pc w :w //apps/web:w",
        "other": "a :a //:a //packages/a:pa //packages/c:pc",
        "other/z": "//:a //packages/a:pa //packages/c:pc zt :zt //other/z:zt",
    },
)

# L5: path aliases `api` → backend and `web` → frontend.
L5 = Layout(
    files={
        "mise.toml": _monorepo(
            '["backend", "frontend"]', "a", aliases='[monorepo.path_aliases]\napi = "backend"\nweb = "frontend"\n'
        ),
        "backend/mise.toml": _task("test"),
        "backend/src/x": "",
        "backend/sub/mise.toml": _task("subt"),
        "backend/sub/deep/x": "",
        "frontend/mise.toml": _task("build"),
    },
    tasks="api:test //api:test backend:test web:build //web:build frontend:build //backend/sub:subt :subt subt "
    "api:subt",
    ok={
        ".": "api:test //api:test backend:test web:build //web:build frontend:build",
        "backend": "api:test //api:test backend:test //web:build",
        "backend/src": "api:test //api:test backend:test //web:build",
        "backend/sub": "api:test //api:test backend:test //web:build //backend/sub:subt :subt subt",
        "backend/sub/deep": "api:test //api:test backend:test //web:build //backend/sub:subt :subt subt",
        "frontend": "//api:test web:build //web:build frontend:build",
    },
)

# L6: a non-root project `z`, and `tk` with file tasks only (not a config
# root, so not a project of its own).
L6 = Layout(
    files={
        "mise.toml": _monorepo('["backend"]', "a"),
        "z/mise.toml": _task("zt"),
        "z/inner/x": "",
        "backend/mise.toml": _task("bt"),
        "tk/mise-tasks/x": FILE_TASK,
    },
    tasks="z:zt //z:zt backend:bt x :x //tk:x tk:x a",
    ok={
        ".": "backend:bt a",
        "z": "z:zt //z:zt",
        "z/inner": "z:zt //z:zt",
        "backend": "backend:bt",
        "tk": "backend:bt //tk:x tk:x a",
    },
)

# L7: no config_roots, so mise finds every directory with mise files; `tk`
# has only file tasks.
L7 = Layout(
    files={
        "mise.toml": "monorepo_root = true\n" + _task("a"),
        "tk/mise-tasks/x": FILE_TASK,
        "tk/inner/y": "",
        "cfg/mise.toml": _task("ct"),
    },
    tasks="x :x //tk:x tk:x a ct //cfg:ct cfg:ct",
    ok={
        ".": "//tk:x tk:x a //cfg:ct cfg:ct",
        "tk": "//tk:x tk:x a //cfg:ct cfg:ct",
        "tk/inner": "//tk:x tk:x a //cfg:ct cfg:ct",
        "cfg": "//tk:x ct //cfg:ct cfg:ct",
    },
)


# L8: nested config roots (`svc/inner` under `svc`), aliases to paths with a
# `/`, a file-task-only config root `tk`, and an environment config that a
# clean environment does not read.
L8 = Layout(
    files={
        "mise.toml": _monorepo(
            '["svc", "svc/inner", "apps/*", "tk"]',
            "a",
            aliases='[monorepo.path_aliases]\nin = "svc/inner"\nw = "apps/web"\nt = "tk"\n',
        ),
        "svc/mise.toml": _task("s"),
        "svc/mise.prod.toml": _task("p"),
        "svc/inner/mise.toml": _task("i"),
        "svc/inner/deep/f": "",
        "apps/web/mise.toml": _task("web"),
        "tk/mise-tasks/x": FILE_TASK,
        "tools/f": "",
    },
    tasks="a :a //:a s :s //svc:s svc:s i :i //svc/inner:i svc/inner:i in:i //in:i inner:i web :web //apps/web:web "
    "w:web //w:web apps:web x :x //tk:x tk:x t:x p :p //svc:p svc:p",
    ok={
        ".": "a :a //:a //svc:s svc:s //svc/inner:i in:i //in:i //apps/web:web w:web //w:web //tk:x tk:x t:x",
        "svc": "//:a s :s //svc:s svc:s //svc/inner:i in:i //in:i //apps/web:web //w:web //tk:x",
        "svc/inner": "//:a //svc:s svc:s i :i //svc/inner:i in:i //in:i //apps/web:web //w:web //tk:x",
        "svc/inner/deep": "//:a //svc:s svc:s i :i //svc/inner:i in:i //in:i //apps/web:web //w:web //tk:x",
        "apps": "a :a //:a //svc:s svc:s //svc/inner:i in:i //in:i //apps/web:web w:web //w:web //tk:x tk:x t:x",
        "apps/web": "//:a //svc:s //svc/inner:i //in:i web :web //apps/web:web w:web //w:web //tk:x",
        "tk": "//:a //svc:s //svc/inner:i //in:i //apps/web:web //w:web x :x //tk:x tk:x t:x",
        "tools": "a :a //:a //svc:s svc:s //svc/inner:i in:i //in:i //apps/web:web w:web //w:web //tk:x tk:x t:x",
    },
    # `p` is defined only in svc/mise.prod.toml: wherever the name reaches
    # svc's own files the check cannot tell it from a selected environment.
    unsettled={
        ".": "//svc:p svc:p",
        "svc": "p :p //svc:p svc:p",
        "svc/inner": "//svc:p svc:p",
        "svc/inner/deep": "//svc:p svc:p",
        "apps": "//svc:p svc:p",
        "apps/web": "//svc:p",
        "tk": "//svc:p",
        "tools": "//svc:p svc:p",
    },
)


def _verdicts(tmp_path: Path, files: Mapping[str, str], cases: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    doc = "".join(f"`cd {cwd} && mise run {task}`\n\n" for cwd, task in cases)
    for rel, content in {**files, "README.md": doc}.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    report = build_report(repo_files_on_disk(tmp_path), [], [], [], [], True, doc_config(ALL_DOCS))
    by_line = {r.line: r.status for r in report.docs["README.md"].references if r.kind == "command"}
    return {case: by_line[2 * index + 1] for index, case in enumerate(cases)}


@pytest.mark.parametrize("layout", [L2, L4, L5, L6, L7, L8], ids=["L2", "L4", "L5", "L6", "L7", "L8"])
def test_mise_names_resolve_as_real_mise_runs_them(tmp_path: Path, layout: Layout) -> None:
    cases = [(cwd, task) for cwd in layout.ok for task in layout.tasks.split()]
    expected = {
        (cwd, task): "ok"
        if task in layout.ok[cwd].split()
        else "not_checked"
        if task in layout.unsettled.get(cwd, "").split()
        else "missing"
        for cwd, task in cases
    }
    assert _verdicts(tmp_path, layout.files, cases) == expected
