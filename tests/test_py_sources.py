"""Tests for py_sources: loading a backend package with per-step diagnostics,
module naming, the per-file parse cache, import resolution and ModuleScope."""

from __future__ import annotations

import ast
import os
import textwrap
from pathlib import Path

import pytest

from code_intel import code_paths, py_sources
from code_intel.backend_model import PyModule
from code_intel.py_sources import ModuleScope, PySources, module_name_for, resolve_relative, static_names


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


def load(tmp_path: Path, files: dict[str, str], package: str = "pkg", **kwargs) -> PySources:
    write_tree(tmp_path, files)
    return PySources(tmp_path, package, tmp_path, **kwargs).load()


def module_of(sources: PySources, name: str) -> PyModule:
    mod = sources.module(name)
    assert mod is not None, name
    return mod


def assigned_value(sources: PySources, name: str) -> ast.expr:
    """The value of the first statement of ``name``, which must be an assignment."""
    stmt = module_of(sources, name).tree.body[0]
    assert isinstance(stmt, ast.Assign)
    return stmt.value


def constant(node: ast.expr) -> object:
    assert isinstance(node, ast.Constant)
    return node.value


def scope_of(tmp_path: Path, source: str) -> ModuleScope:
    sources = load(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": source})
    return sources.scope("pkg.m")


def stmt_at(scope: ModuleScope, line: int) -> tuple[int, ast.AST]:
    """(top-level index, a Name node on ``line``) for a use written on that line."""
    for node in ast.walk(scope.module.tree):
        if isinstance(node, ast.Name) and node.lineno == line and isinstance(node.ctx, ast.Load):
            index = scope.index_of(node)
            assert index is not None
            return index, node
    raise AssertionError(f"no Name load on line {line}")


def values(bindings) -> list[str]:
    return [ast.unparse(b.value) if b.value is not None else b.kind for b in bindings]


# ── Naming and relative imports ──────────────────────────────────────────


def test_module_names_follow_the_import_system(tmp_path: Path) -> None:
    assert module_name_for(tmp_path / "pkg" / "__init__.py", tmp_path) == "pkg"
    assert module_name_for(tmp_path / "pkg" / "api" / "routes.py", tmp_path) == "pkg.api.routes"
    assert module_name_for(tmp_path / "pkg" / "bad-name.py", tmp_path) is None
    assert module_name_for(Path("/elsewhere/x.py"), tmp_path) is None


@pytest.mark.parametrize(
    "module,is_package,level,target,expected",
    [
        ("pkg.api.routes", False, 0, "fastapi", "fastapi"),
        ("pkg.api.routes", False, 1, None, "pkg.api"),
        ("pkg.api.routes", False, 1, "deps", "pkg.api.deps"),
        ("pkg.api.routes", False, 2, "core", "pkg.core"),
        ("pkg.api", True, 1, "routes", "pkg.api.routes"),
        ("pkg.api", True, 2, None, "pkg"),
        ("pkg.m", False, 3, None, None),
    ],
)
def test_relative_imports_resolve_against_the_package(module, is_package, level, target, expected) -> None:
    assert resolve_relative(module, is_package, level, target) == expected


def test_load_names_every_module_and_counts_files(tmp_path: Path) -> None:
    sources = load(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "app = 1\n",
            "pkg/api/__init__.py": "",
            "pkg/api/routes/a.py": "x = 1\n",
            "pkg/.hidden/skip.py": "x = 1\n",
            "pkg/__pycache__/skip.py": "x = 1\n",
            "other/ignored.py": "x = 1\n",
        },
    )
    assert sorted(sources.modules) == ["pkg", "pkg.api", "pkg.api.routes.a", "pkg.main"]
    assert sources.files_scanned == 4
    assert not any(p.is_diagnostic for p in sources.problems)
    assert module_of(sources, "pkg").is_package and not module_of(sources, "pkg.main").is_package
    assert module_of(sources, "pkg.api.routes.a").rel == "pkg/api/routes/a.py"
    # `pkg.api.routes` has no __init__.py but is still a (namespace) package.
    assert sources.has_module("pkg.api.routes") and not sources.has_module("pkg.nope")


# ── Per-step diagnostics ──────────────────────────────────────────────────


def _kinds(sources: PySources) -> list[tuple[str, str | None, str | None]]:
    return [(p.kind, p.step, p.file) for p in sources.problems]


def test_parse_failure_names_the_file_and_step_and_keeps_the_others(tmp_path: Path) -> None:
    sources = load(tmp_path, {"pkg/__init__.py": "", "pkg/bad.py": "def (:\n", "pkg/good.py": "x = 1\n"})
    assert _kinds(sources) == [("parse_failed", "parse file", "pkg/bad.py")]
    assert "pkg/bad.py: could not parse file (SyntaxError" in sources.problems[0].message
    assert sources.problems[0].line == 1
    assert "pkg.bad" not in sources.modules and "pkg.good" in sources.modules
    assert any(p.is_diagnostic for p in sources.problems)


def test_decode_failure_is_its_own_step(tmp_path: Path) -> None:
    write_tree(tmp_path, {"pkg/__init__.py": ""})
    (tmp_path / "pkg" / "latin.py").write_bytes(b"# -*- coding: no-such-codec -*-\nx = 1\n")
    (tmp_path / "pkg" / "utf8.py").write_bytes(b"x = '\xff'\n")
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    assert sorted(_kinds(sources)) == [
        ("decode_failed", "decode file", "pkg/latin.py"),
        ("decode_failed", "decode file", "pkg/utf8.py"),
    ]


def test_source_encoding_follows_pep_263_and_the_bom(tmp_path: Path) -> None:
    write_tree(tmp_path, {"pkg/__init__.py": ""})
    (tmp_path / "pkg" / "latin.py").write_bytes(b"# -*- coding: latin-1 -*-\nNAME = '\xe9'\n")
    (tmp_path / "pkg" / "bom.py").write_bytes(b"\xef\xbb\xbfNAME = 'b'\n")
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    assert not any(p.is_diagnostic for p in sources.problems)
    assert constant(assigned_value(sources, "pkg.latin")) == "\u00e9"
    assert "pkg.bom" in sources.modules


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX permissions")
def test_read_and_list_failures_are_named(tmp_path: Path) -> None:
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/secret.py": "x = 1\n", "pkg/locked/a.py": "x = 1\n"})
    (tmp_path / "pkg" / "secret.py").chmod(0)
    (tmp_path / "pkg" / "locked").chmod(0)
    try:
        sources = PySources(tmp_path, "pkg", tmp_path).load()
    finally:
        (tmp_path / "pkg" / "secret.py").chmod(0o644)
        (tmp_path / "pkg" / "locked").chmod(0o755)
    assert sorted(_kinds(sources)) == [
        ("list_failed", "list directory", "pkg/locked"),
        ("read_failed", "read file", "pkg/secret.py"),
    ]
    assert any(p.is_diagnostic for p in sources.problems)


def test_missing_package_root_is_a_list_failure(tmp_path: Path) -> None:
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    assert _kinds(sources) == [("list_failed", "list directory", "pkg")]
    assert sources.modules == {}


def test_any_other_per_file_error_is_analysis_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    real_parse = ast.parse

    def flaky_parse(source, filename="<unknown>", *args, **kwargs):
        if str(filename).endswith("a.py"):
            raise RuntimeError("boom")
        return real_parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(py_sources.ast, "parse", flaky_parse)
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    assert _kinds(sources) == [("analysis_failed", "analyze module", "pkg/a.py")]
    assert "pkg/a.py: could not analyze module (RuntimeError: boom)" == sources.problems[0].message
    assert "pkg.b" in sources.modules


def test_deep_nesting_is_a_parse_failure_not_a_crash(tmp_path: Path) -> None:
    deep = "x = " + "(" * 5000 + "1" + ")" * 5000 + "\n"
    sources = load(tmp_path, {"pkg/__init__.py": "", "pkg/deep.py": deep})
    assert [p.kind for p in sources.problems] == ["parse_failed"]
    assert sources.problems[0].step == "parse file"


# ── Parse cache ──────────────────────────────────────────────────────────


def test_reload_reuses_unchanged_modules_and_reparses_edits(tmp_path: Path) -> None:
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n", "pkg/b.py": "y = 1\n"})
    sources = PySources(tmp_path, "pkg", tmp_path, clock=lambda: 10**20).load()
    first_a, first_b = sources.module("pkg.a"), sources.module("pkg.b")
    assert sources.changed == {"pkg", "pkg.a", "pkg.b"}
    sources.load()
    assert sources.changed == frozenset()
    assert sources.module("pkg.a") is first_a
    (tmp_path / "pkg" / "a.py").write_text("x = 22\n", encoding="utf-8")
    sources.load()
    assert sources.changed == {"pkg.a"}
    assert sources.module("pkg.a") is not first_a and sources.module("pkg.b") is first_b
    (tmp_path / "pkg" / "b.py").unlink()
    sources.load()
    assert "pkg.b" not in sources.modules


@pytest.mark.usefixtures("ctime_follows_mtime")
def test_racy_stamp_is_checked_by_content(tmp_path: Path) -> None:
    """A same-stamp edit inside the window is caught by content. ctime follows
    mtime here, as for two writes in one ctime tick: with the kernel's ctime
    the rewrite would change the stamp and never reach the content check."""
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    path = tmp_path / "pkg" / "a.py"
    st = path.stat()
    # The clock says the file was written just now, so its stamp alone is not trusted.
    sources = PySources(tmp_path, "pkg", tmp_path, clock=lambda: st.st_mtime_ns).load()
    first = sources.module("pkg.a")
    path.write_text("x = 2\n", encoding="utf-8")  # same size
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size
    sources.load()
    assert sources.module("pkg.a") is not first
    assert constant(assigned_value(sources, "pkg.a")) == 2


def _edit_in_place(path: Path, text: str) -> None:
    """Rewrite ``path`` with same-size content, keeping its whole stamp when
    ``ctime_follows_mtime`` is active."""
    st = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size


def _edit_after_first_read(monkeypatch: pytest.MonkeyPatch, path: Path, text: str) -> None:
    """Make the first read of ``path`` return its old bytes and then change the
    file, as an edit landing between the read and the cache insertion would."""
    real = code_paths.read_file_bytes
    pending = [True]

    def read(target: Path) -> bytes:
        data = real(target)
        if pending and target == path:
            pending.clear()
            _edit_in_place(path, text)
        return data

    monkeypatch.setattr(code_paths, "read_file_bytes", read)


@pytest.mark.usefixtures("ctime_follows_mtime")
def test_an_edit_between_the_read_and_the_cache_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The digest kept for a racy file is of the bytes parsed, so an edit right
    after the read makes the next load parse the file again. ctime follows
    mtime, so the edit keeps the stamp and only the digest can show it."""
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    path = tmp_path / "pkg" / "a.py"
    st = path.stat()
    _edit_after_first_read(monkeypatch, path, "x = 2\n")
    sources = PySources(tmp_path, "pkg", tmp_path, clock=lambda: st.st_mtime_ns).load()
    assert constant(assigned_value(sources, "pkg.a")) == 1
    sources.load()
    assert constant(assigned_value(sources, "pkg.a")) == 2


def test_a_same_size_rewrite_with_its_mtime_restored_after_the_window_is_reparsed(
    tmp_path: Path, rewrite_keeping_mtime
) -> None:
    """A module loaded once it is past the racy window is trusted by stamp
    alone; the stamp's ctime still catches a rewrite that sets mtime back
    with the same size, as a backend route edited in place would be."""
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/routes.py": '@router.get("/old")\ndef f(): pass\n'})
    path = tmp_path / "pkg" / "routes.py"
    clock = {"now": 0}

    def settle() -> None:
        st = path.stat()
        clock["now"] = max(st.st_mtime_ns, st.st_ctime_ns) + 3 * code_paths.RACY_WINDOW_NS

    settle()
    sources = PySources(tmp_path, "pkg", tmp_path, clock=lambda: clock["now"]).load()
    sources.load()
    assert sources.changed == frozenset()
    rewrite_keeping_mtime(path, '@router.get("/new")\ndef f(): pass\n')
    settle()
    sources.load()
    assert sources.changed == {"pkg.routes"}
    handler = module_of(sources, "pkg.routes").tree.body[0]
    assert isinstance(handler, ast.FunctionDef)
    decorator = handler.decorator_list[0]
    assert isinstance(decorator, ast.Call)
    assert constant(decorator.args[0]) == "/new"


def test_scope_is_rebuilt_only_for_a_reparsed_module(tmp_path: Path) -> None:
    write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    sources = PySources(tmp_path, "pkg", tmp_path, clock=lambda: 10**20).load()
    scope = sources.scope("pkg.a")
    sources.load()
    assert sources.scope("pkg.a") is scope
    (tmp_path / "pkg" / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    sources.load()
    assert sources.scope("pkg.a") is not scope


# ── ModuleScope ──────────────────────────────────────────────────────────


def test_a_use_sees_only_the_bindings_before_it(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        P = "/a"
        use1 = P
        P = "/b"
        use2 = P
        """,
    )
    at, node = stmt_at(scope, 2)
    assert values(scope.reaching("P", at, node)) == ["'/a'"]
    at, node = stmt_at(scope, 4)
    assert values(scope.reaching("P", at, node)) == ["'/b'"]
    assert values(scope.reaching("P", None)) == ["'/b'"]


def test_a_binding_in_the_same_statement_is_not_yet_visible(tmp_path: Path) -> None:
    scope = scope_of(tmp_path, "P = '/a'\nP = P + '/b'\n")
    at, node = stmt_at(scope, 2)
    assert values(scope.reaching("P", at, node)) == ["'/a'"]


def test_branch_bindings_may_or_may_not_replace_earlier_ones(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        import os
        P = "/base"
        if os.environ.get("X"):
            P = "/x"
            inside = P
        else:
            P = "/y"
        after = P
        """,
    )
    at, node = stmt_at(scope, 5)
    assert values(scope.reaching("P", at, node)) == ["'/x'"]
    at, node = stmt_at(scope, 8)
    assert values(scope.reaching("P", at, node)) == ["'/y'", "'/x'", "'/base'"]
    assert all(b.conditional for b in scope.reaching("P", at, node)[:2])


def test_try_body_and_with_body_run_while_handlers_may(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        try:
            from fastapi import APIRouter
        except ImportError:
            APIRouter = None
        with open("x") as fh:
            NAME = "n"
        use = APIRouter, NAME
        """,
    )
    at, node = stmt_at(scope, 7)
    assert [b.kind for b in scope.reaching("APIRouter", at, node)] == ["assign", "from_import"]
    assert [b.kind for b in scope.reaching("NAME", at, node)] == ["assign"]
    assert [b.kind for b in scope.reaching("fh", at, node)] == ["with"]


def test_function_and_class_bodies_bind_no_module_names(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        def f():
            router = 1
        class C:
            router = 2
        """,
    )
    assert "router" not in scope.bindings
    assert [b.kind for b in scope.bindings["f"]] == ["def"]
    assert [b.kind for b in scope.bindings["C"]] == ["class"]


def test_loop_targets_walrus_match_and_except_names(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        for item in [1]:
            pass
        if (w := "/w"):
            pass
        match w:
            case str() as captured:
                pass
        try:
            pass
        except Exception as err:
            pass
        lazy = lambda: (z := 1)
        """,
    )
    assert scope.bindings["item"][0].kind == "for" and scope.bindings["item"][0].conditional
    assert scope.bindings["w"][0].kind == "walrus" and values(scope.bindings["w"][:1]) == ["'/w'"]
    assert scope.bindings["captured"][0].kind == "match"
    assert scope.bindings["err"][0].kind == "except"
    assert "z" not in scope.bindings


def test_imports_record_the_absolute_source(tmp_path: Path) -> None:
    sources = load(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/api/__init__.py": "",
            "pkg/api/m.py": """
                import os.path
                import pkg.api as api_pkg
                from . import sibling
                from ..core import settings as cfg
                from .barrel import *
                """,
        },
    )
    scope = sources.scope("pkg.api.m")
    assert scope.bindings["os"][0].imported_from == "os"
    assert scope.bindings["api_pkg"][0].imported_from == "pkg.api"
    sibling = scope.bindings["sibling"][0]
    assert (sibling.kind, sibling.imported_from, sibling.imported_name) == ("from_import", "pkg.api", "sibling")
    assert (scope.bindings["cfg"][0].imported_from, scope.bindings["cfg"][0].imported_name) == ("pkg.core", "settings")
    assert [(b.kind, b.imported_from) for b in scope.star_imports] == [("star_import", "pkg.api.barrel")]


def test_star_imports_after_the_last_certain_binding_reach(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        X = 1
        from os import *
        use = X
        X = 2
        from sys import *
        """,
    )
    at, node = stmt_at(scope, 3)
    assert [b.kind for b in scope.reaching("X", at, node)] == ["star_import", "assign"]
    assert [(b.kind, b.imported_from) for b in scope.reaching("X", None)] == [("star_import", "sys"), ("assign", None)]


def test_exports_use_a_literal_all_or_public_names(tmp_path: Path) -> None:
    sources = load(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "__all__ = ['router']\nrouter = 1\nother = 2\n",
            "pkg/b.py": "router = 1\n_private = 2\nfrom os import path\n",
            "pkg/c.py": "__all__ = NAMES\n",
            "pkg/d.py": "from pkg.a import *\nown = 1\n",
        },
    )
    assert sources.scope("pkg.a").exports() == ("router",)
    assert sources.scope("pkg.b").exports() == ("path", "router")
    assert sources.scope("pkg.c").exports() is None
    # A star import's names are re-exported too, so they cannot be listed here.
    assert sources.scope("pkg.d").exports() is None


@pytest.mark.parametrize(
    "source",
    [
        "import os\nif os.environ.get('X'):\n    __all__ = ['router']\nelse:\n    __all__ = []\n",
        "__all__ = ['router']\n__all__ = []\n",
        "__all__ = []\n__all__ += ['router']\n",
        "__all__ = []\n__all__.append('router')\n",
        "__all__ = []\n__all__.extend(['router'])\n",
        "__all__ = []\ndef add():\n    __all__.append('router')\n",
        "try:\n    __all__ = []\nexcept ImportError:\n    __all__ = ['router']\n",
    ],
)
def test_exports_are_unknown_unless_all_is_one_unconditional_literal(tmp_path: Path, source: str) -> None:
    sources = load(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "router = 1\n" + source})
    assert sources.scope("pkg.a").exports() is None


def test_exports_accept_an_annotated_literal_all(tmp_path: Path) -> None:
    sources = load(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "__all__: list[str] = ['router']\nrouter = 1\n"})
    assert sources.scope("pkg.a").exports() == ("router",)


def test_index_of_finds_the_top_level_statement(tmp_path: Path) -> None:
    scope = scope_of(tmp_path, "a = 1\n\nif a:\n    b = (\n        a\n    )\n")
    branch = scope.module.tree.body[1]
    assert isinstance(branch, ast.If)
    assignment = branch.body[0]
    assert isinstance(assignment, ast.Assign)
    inner = assignment.value
    assert scope.index_of(inner) == 1
    assert scope.index_of(scope.module.tree.body[0]) == 0


def test_a_decorator_belongs_to_its_decorated_statement(tmp_path: Path) -> None:
    """A decorated ``def`` starts at its first decorator, so a use inside the
    decorator sits in that statement and sees only earlier bindings."""
    scope = scope_of(
        tmp_path,
        """
        P = "/a"
        @route(P)
        def handler(): ...
        P = "/b"
        """,
    )
    at, node = stmt_at(scope, 2)
    assert at == 1
    assert values(scope.reaching("P", at, node)) == ["'/a'"]


# ── Code that never runs on import ───────────────────────────────────────


def test_type_checking_and_main_blocks_bind_nothing_at_import(tmp_path: Path) -> None:
    """``if TYPE_CHECKING:`` and ``if __name__ == "__main__":`` bodies never run
    when the module is imported; their ``else`` always does."""
    scope = scope_of(
        tmp_path,
        """
        from typing import TYPE_CHECKING
        import typing as t
        if TYPE_CHECKING:
            from pkg.a import Alias
        else:
            Alias = 1
        if not t.TYPE_CHECKING:
            Other = 2
        if __name__ == "__main__":
            Main = 3
        use = Alias, Other
        """,
    )
    index, node = stmt_at(scope, 11)
    assert [(b.kind, b.conditional) for b in scope.reaching("Alias", index, node)] == [("assign", False)]
    assert [(b.kind, b.conditional) for b in scope.reaching("Other", index, node)] == [("assign", False)]
    assert scope.reaching("Main", None) == []
    assert all(target != "pkg.a" for _, targets, _, _ in scope.import_targets() for target in targets)


_GUARDED = """
from typing import TYPE_CHECKING
{other}
if TYPE_CHECKING:
    from pkg.a import Alias
else:
    Alias = 1
"""


@pytest.mark.parametrize(
    "other",
    [
        "def f(TYPE_CHECKING): ...",
        "def f():\n    TYPE_CHECKING = True",
        "def f():\n    def g():\n        from x import TYPE_CHECKING",
        "class C:\n    TYPE_CHECKING = True",
        "hook = lambda TYPE_CHECKING: 1",
        "names = [TYPE_CHECKING for TYPE_CHECKING in ()]",
        "def f():\n    global TYPE_CHECKING\n    return TYPE_CHECKING",
    ],
)
def test_a_name_bound_only_in_another_scope_does_not_rebind_the_flag(tmp_path: Path, other: str) -> None:
    scope = scope_of(tmp_path, _GUARDED.format(other=other))
    assert [b.kind for b in scope.reaching("Alias", None)] == ["assign"]


@pytest.mark.parametrize(
    "other",
    [
        "TYPE_CHECKING = True",
        "for TYPE_CHECKING in (): pass",
        "flags = [(TYPE_CHECKING := 1) for _ in ()]",
        "def f():\n    global TYPE_CHECKING\n    TYPE_CHECKING = True",
        "class C:\n    def f(self):\n        global TYPE_CHECKING\n        TYPE_CHECKING = True",
        "def f():\n    global TYPE_CHECKING\n    def g():\n        pass\n    def TYPE_CHECKING(): ...",
        "@decorate(TYPE_CHECKING := True)\ndef f(): ...",
    ],
)
def test_a_module_rebinding_of_the_flag_makes_the_block_run(tmp_path: Path, other: str) -> None:
    scope = scope_of(tmp_path, _GUARDED.format(other=other))
    assert sorted(b.kind for b in scope.reaching("Alias", None)) == ["assign", "from_import"]


def test_inside_a_function_a_local_flag_name_is_not_the_flag() -> None:
    tree = ast.parse("from typing import TYPE_CHECKING\ndef f(TYPE_CHECKING): ...\n")
    test = ast.Name("TYPE_CHECKING", ast.Load())
    assert static_names(tree).truth(test) is False
    assert static_names(tree, nested=True).truth(test) is None


def test_import_targets_list_every_module_a_statement_runs(tmp_path: Path) -> None:
    sources = load(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/m.py": """
                import os.path
                from . import sibling, other as o
                from ..api.routes import router
                from ....too_far import x
                def later():
                    import pkg.never
                if os.environ.get("X"):
                    import json
                """,
        },
    )
    assert sources.scope("pkg.sub.m").import_targets() == [
        (0, ("os", "os.path"), False, 1),
        (1, ("pkg", "pkg.sub", "pkg.sub.sibling", "pkg.sub.other"), False, 2),
        (2, ("pkg", "pkg.api", "pkg.api.routes", "pkg.api.routes.router"), False, 3),
        (5, ("json",), True, 8),
    ]


# ── Changes in place ─────────────────────────────────────────────────────


def test_in_place_changes_are_found_by_position_and_scope(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        A = [1]
        use_a_before = A
        A.append(2)
        B = {"k": 1}
        B["k"] = 2
        C = object()
        C.attr = 1
        D = [1]
        def grow():
            D.extend([2])
        E = [1]
        def shadow(E):
            E.append(1)
        def rebind():
            global F
            F = 2
        F = 1
        use = A, B, C, D, E, F
        """,
    )
    before, node_before = stmt_at(scope, 2)
    assert not scope.mutated("A", before, node_before)
    after, node_after = stmt_at(scope, 18)
    assert [name for name in "ABCDEF" if scope.mutated(name, after, node_after)] == ["A", "B", "C", "D", "F"]
    assert ("C", ("attr",)) in scope.mutation_receivers()


def test_a_change_through_a_second_name_or_setattr_changes_the_value(tmp_path: Path) -> None:
    scope = scope_of(
        tmp_path,
        """
        import config
        COMMON = [1]
        ALIAS = COMMON
        ALIAS.append(2)
        PATHS = config.PATHS
        PATHS.append("/x")
        OTHER = [1]
        setattr(config, "PREFIX", "/new")
        use = COMMON, ALIAS, OTHER
        """,
    )
    index, node = stmt_at(scope, 8)
    assert [name for name in ("COMMON", "ALIAS", "OTHER") if scope.mutated(name, index, node)] == ["COMMON", "ALIAS"]
    receivers = scope.mutation_receivers()
    assert ("config", ("PATHS",)) in receivers
    assert ("config", ("PREFIX",)) in receivers


def test_a_change_through_another_modules_second_name_is_found(tmp_path: Path) -> None:
    sources = load(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/guards.py": "COMMON = [1]\nEXTRA = COMMON\nOTHER = [1]\n",
            "pkg/main.py": "from pkg.guards import EXTRA\nEXTRA.extend([2])\n",
        },
    )
    assert sources.changed_elsewhere("pkg.guards", "COMMON")
    assert not sources.changed_elsewhere("pkg.guards", "OTHER")


def test_a_change_made_through_another_modules_import_is_found(tmp_path: Path) -> None:
    sources = load(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/guards.py": "COMMON = []\nOTHER = []\n",
            "pkg/a.py": "from pkg.guards import COMMON\nCOMMON.append(1)\n",
            "pkg/b.py": "import pkg.guards\npkg.guards.OTHER[0:0] = [1]\n",
            "pkg/c.py": "import pkg.guards as g\ng.SETTING = 1\n",
        },
    )
    assert sources.changed_elsewhere("pkg.guards", "COMMON")
    assert sources.changed_elsewhere("pkg.guards", "OTHER")
    assert sources.changed_elsewhere("pkg.guards", "SETTING")
    assert not sources.changed_elsewhere("pkg.guards", "UNTOUCHED")


def test_finding_the_block_of_a_use_stays_linear_in_one_wrapped_module() -> None:
    """A module wrapped in one ``try:`` put every statement under one top-level
    index, and each use scanned all of them (2,000 uses took seconds)."""
    import scaling_check

    def make(n: int) -> tuple[ModuleScope, list[tuple[str, ast.AST]]]:
        source = "try:\n" + "".join(f"    x{i} = {i}\n    y{i} = x{i}\n" for i in range(n)) + "except E:\n    pass\n"
        tree = ast.parse(source)
        uses: list[tuple[str, ast.AST]] = [
            (n.id, n) for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        ]
        mod = PyModule("pkg.m", Path("m.py"), "m.py", code_paths.FileStamp(0, 0, 0, 0), tree, source, False)
        return ModuleScope(mod, lambda level, target: target), uses

    def run(made: tuple[ModuleScope, list[tuple[str, ast.AST]]]) -> None:
        scope, uses = made
        for name, node in uses:
            scope.reaching(name, 0, node)

    scaling_check.assert_scales_linearly(make, run, 2000)
