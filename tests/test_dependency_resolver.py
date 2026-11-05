"""``dependency_resolver``: FastAPI's ``analyze_param`` rules and canonical identity.

Each test runs twice: against a small ``NameResolver`` double (module-level
bindings, last one wins) and against the shared ``py_values.ValueEvaluator``
when it is present, so the resolver depends only on the protocol.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_intel.backend_model import (
    Def,
    DepLayer,
    DepUse,
    External,
    LiveRoute,
    Located,
    ModuleRef,
    PyModule,
    Target,
    Unknown,
    Value,
)
from code_intel.code_paths import FileStamp
from code_intel.dependency_resolver import BOTH_DECLARED_MESSAGE, DependencyResolver, ModuleSource

PACKAGE = "pkg"

DEPS = """
from fastapi import Depends, Security

def get_db():
    yield "db"

def get_user(db=Depends(get_db)):
    return "u"

def get_admin(user=Depends(get_user)):
    return user

def verify_key():
    return True

def audit():
    return True
"""


def _module_name(rel: str) -> str:
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ── NameResolver double ──────────────────────────────────────────────────


class FakeSources:
    """Parsed modules keyed by dotted name; ``rel`` is ``backend/<path>``."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.modules: dict[str, PyModule] = {}
        for rel, source in files.items():
            name = _module_name(rel)
            text = textwrap.dedent(source).lstrip("\n")
            self.modules[name] = PyModule(
                name=name,
                path=Path("/fake") / rel,
                rel=f"backend/{rel}",
                stamp=FileStamp(0, 0, 0, 0),
                tree=ast.parse(text),
                text=text,
                is_package=rel.endswith("__init__.py"),
            )

    def module(self, name: str) -> PyModule | None:
        return self.modules.get(name)


class FakeResolver:
    """Module-level bindings in statement order, the last one wins; ``at`` is ignored."""

    def __init__(self, sources: FakeSources) -> None:
        self.sources = sources

    def _absolute(self, module: str, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        mod = self.sources.module(module)
        parts = module.split(".")
        base = parts if mod is not None and mod.is_package else parts[:-1]
        base = base[: len(base) - (node.level - 1)]
        return ".".join([*base, node.module] if node.module else base)

    def _member(self, source: str, name: str) -> list[Target]:
        if self.sources.module(source) is not None:
            found = self.resolve_name(source, name, None)
            if not (len(found) == 1 and isinstance(found[0], Unknown)):
                return found
        if self.sources.module(f"{source}.{name}") is not None:
            return [ModuleRef(f"{source}.{name}")]
        return [External(f"{source}.{name}")]

    def resolve_name(self, module: str, name: str, at: int | None) -> list[Target]:
        mod = self.sources.module(module)
        if mod is None:
            return [Unknown("missing module", f"{module}.{name}")]
        found: list[Target] = []
        for index, stmt in enumerate(mod.tree.body):
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                found = self._imported(module, stmt, name) or found
            else:
                found = self._bound(module, index, stmt, name) or found
        if not found:
            if name in ("staticmethod", "classmethod", "object"):
                return [External(f"builtins.{name}")]
            if self.sources.module(f"{module}.{name}") is not None:
                return [ModuleRef(f"{module}.{name}")]
            return [Unknown("unbound", name)]
        return found

    @staticmethod
    def _bound(module: str, index: int, stmt: ast.stmt, name: str) -> list[Target]:
        """What a statement other than an import binds to ``name``, if it binds it."""
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == name:
            return [Def(module, name, stmt)]
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
            return [Value(Located(module, stmt.value, index))]
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == name:
            return [Value(Located(module, stmt.value, index))] if stmt.value is not None else []
        if isinstance(stmt, ast.TypeAlias) and stmt.name.id == name:
            return [Value(Located(module, stmt.value, index))]
        return []

    def _imported(self, module: str, stmt: ast.Import | ast.ImportFrom, name: str) -> list[Target]:
        """What an import statement binds to ``name``, if it binds it."""
        found: list[Target] = []
        for alias in stmt.names:
            if isinstance(stmt, ast.ImportFrom):
                if (alias.asname or alias.name) == name:
                    found = self._member(self._absolute(module, stmt), alias.name)
            elif alias.asname == name:
                found = [self._module(alias.name)]
            elif alias.asname is None and alias.name.split(".")[0] == name:
                found = [self._module(name)]
        return found

    def _module(self, dotted: str) -> Target:
        return ModuleRef(dotted) if self.sources.module(dotted) is not None else External(dotted)

    def resolve_expr(self, loc: Located) -> list[Target]:
        node = loc.node
        if isinstance(node, ast.Name):
            out: list[Target] = []
            for target in self.resolve_name(loc.module, node.id, loc.at):
                if isinstance(target, Value) and isinstance(target.loc.node, (ast.Name, ast.Attribute)):
                    out.extend(self.resolve_expr(target.loc))
                else:
                    out.append(target)
            return out
        if isinstance(node, ast.Attribute):
            out = []
            for base in self.resolve_expr(Located(loc.module, node.value, loc.at)):
                if isinstance(base, ModuleRef):
                    out.extend(self._member(base.name, node.attr))
                elif isinstance(base, External):
                    out.append(External(f"{base.dotted}.{node.attr}"))
                else:
                    out.append(Unknown("attribute", node.attr))
            return out
        if isinstance(node, ast.Lambda):
            return [Def(loc.module, f"<lambda>@L{node.lineno}", node)]
        return [Value(loc)]

    def string(self, loc: Located) -> str | Unknown:
        return Unknown("not modelled", "string")

    def sequence(self, loc: Located) -> tuple[Located, ...] | Unknown:
        return Unknown("not modelled", "sequence")


# ── Fixtures ─────────────────────────────────────────────────────────────


@dataclass
class Analysis:
    resolver: DependencyResolver
    sources: ModuleSource

    def module(self, name: str) -> PyModule:
        mod = self.sources.module(name)
        assert mod is not None, name
        return mod

    def function(self, module: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
        for node in ast.walk(self.module(module).tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f"{module}: no def {name}")

    def params(self, module: str, handler: str) -> list[DepUse]:
        return self.resolver.endpoint_deps(Located(module, self.function(module, handler)))

    def elements(self, module: str, name: str) -> tuple[Located, ...]:
        for stmt in self.module(module).tree.body:
            if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
                assert isinstance(stmt.value, (ast.List, ast.Tuple))
                return tuple(Located(module, e) for e in stmt.value.elts)
        raise AssertionError(f"{module}: no list {name}")

    def problems(self) -> list[tuple[str, str | None, int | None, str]]:
        return [(p.kind, p.file, p.line, p.message) for p in self.resolver.problems]


@pytest.fixture(params=["double", "evaluator"])
def analyze(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[Mapping[str, str]], Analysis]:
    """``analyze({path under backend/: source})`` -> ``Analysis``; ``pkg/__init__.py`` and ``pkg/deps.py`` are added."""

    def build(files: Mapping[str, str]) -> Analysis:
        all_files = {"pkg/__init__.py": "", "pkg/deps.py": DEPS, **files}
        if request.param == "double":
            sources = FakeSources(all_files)
            return Analysis(DependencyResolver(FakeResolver(sources), sources), sources)
        from code_intel import py_sources, py_values

        for rel, source in all_files.items():
            path = tmp_path / "backend" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        real = py_sources.PySources(tmp_path / "backend", PACKAGE, tmp_path).load()
        assert not [p for p in real.problems if p.is_diagnostic], real.problems
        return Analysis(DependencyResolver(py_values.ValueEvaluator(real), real), real)

    return build


def ids(uses: list[DepUse]) -> list[str]:
    return [u.target.id for u in uses]


def summary(uses: list[DepUse]) -> list[tuple]:
    return [(u.param, u.target.id, u.target.kind, u.display, u.alias, u.marker, u.via) for u in uses]


# ── Markers ──────────────────────────────────────────────────────────────


def test_security_and_depends_markers_in_every_position(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            import fastapi
            from fastapi import Depends, Security
            from fastapi import params
            from pkg.deps import get_user, get_admin, get_db

            def sec_default(u=Security(get_user, scopes=["read"])): ...
            def sec_annotated(u: Annotated[str, Security(get_admin)]): ...
            def attr_marker(db=fastapi.Depends(get_db)): ...
            def params_class(db=params.Depends(get_db)): ...
            """
        }
    )
    assert summary(a.params("pkg.a", "sec_default")) == [
        ("u", "pkg.deps:get_user", "function", "get_user", None, "Security", "direct")
    ]
    assert summary(a.params("pkg.a", "sec_annotated")) == [
        ("u", "pkg.deps:get_admin", "function", "get_admin", None, "Security", "direct")
    ]
    assert ids(a.params("pkg.a", "attr_marker")) == ["pkg.deps:get_db"]
    assert ids(a.params("pkg.a", "params_class")) == ["pkg.deps:get_db"]


def test_a_local_function_named_depends_is_not_a_marker(analyze) -> None:
    """A marker is identified by what it resolves to, never by its spelling."""
    a = analyze(
        {
            "pkg/a.py": """
            from pkg.deps import get_db

            def Depends(x):
                return x

            def handler(db=Depends(get_db)): ...
            """
        }
    )
    assert a.params("pkg.a", "handler") == []
    assert a.problems() == []


def test_dependency_keyword_and_bare_depends_use_the_annotation(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends
            from pkg.deps import get_db

            class Pager:
                def __init__(self, db=Depends(get_db), limit: int = 10): ...

            def keyword(db=Depends(dependency=get_db)): ...
            def explicit_none(p: Pager = Depends(None)): ...
            def bare(p: Pager = Depends()): ...
            def annotated_bare(p: Annotated[Pager, Depends()]): ...
            def quoted_bare(p: "Pager" = Depends()): ...
            def no_type(p=Depends()): ...
            """
        }
    )
    assert ids(a.params("pkg.a", "keyword")) == ["pkg.deps:get_db"]
    for handler in ("explicit_none", "bare", "annotated_bare", "quoted_bare"):
        uses = a.params("pkg.a", handler)
        assert summary(uses) == [("p", "pkg.a:Pager", "class", "Pager", None, "Depends", "direct")], handler
        assert ids(a.resolver.sub_deps(uses[0].target)) == ["pkg.deps:get_db"], "class deps come from __init__"
    (no_type,) = a.params("pkg.a", "no_type")
    assert no_type.target.kind == "unresolved"
    assert [p[0] for p in a.problems()] == ["unresolved_dependency"]


# ── Annotated: last marker wins (oracle case c17) ────────────────────────


def test_only_the_last_fastapi_marker_in_annotated_counts(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends, Query, Body
            from pkg.deps import get_db, get_user, verify_key

            CurrentUser = Annotated[str, Depends(get_user)]
            Keyed = Annotated[CurrentUser, Depends(verify_key)]
            QueryDb = Annotated[CurrentUser, Query()]

            def two(x: Annotated[str, Depends(get_db), Depends(get_user)]): ...
            def nested(x: Keyed): ...
            def query_last(x: Annotated[str, Depends(get_db), Query()] = "q"): ...
            def body_last(x: Annotated[dict, Depends(get_db), Body()]): ...
            def alias_then_query(x: QueryDb): ...
            def depends_then_query_default(x: Annotated[str, Depends(get_db)] = Query("q")): ...
            """
        }
    )
    assert ids(a.params("pkg.a", "two")) == ["pkg.deps:get_user"]
    assert ids(a.params("pkg.a", "nested")) == ["pkg.deps:verify_key"], "nested Annotated flattens"
    assert a.params("pkg.a", "query_last") == []
    assert a.params("pkg.a", "body_last") == []
    assert a.params("pkg.a", "alias_then_query") == [], "an inner alias's Depends loses to a later Query()"
    # analyze_param keeps an Annotated Depends when the default is a param marker.
    assert ids(a.params("pkg.a", "depends_then_query_default")) == ["pkg.deps:get_db"]
    assert a.problems() == []


def test_a_dependency_in_both_annotated_and_the_default_is_rejected(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends, Query
            from pkg.deps import get_db, get_user

            DbSession = Annotated[str, Depends(get_db)]

            def both(db: DbSession = Depends(get_user)): ...
            def query_and_default(x: Annotated[str, Query()] = Depends(get_user)): ...
            """
        }
    )
    assert a.params("pkg.a", "both") == []
    assert a.params("pkg.a", "query_and_default") == []
    found = a.problems()
    assert [(kind, file, line) for kind, file, line, _ in found] == [
        ("unresolved_dependency", "backend/pkg/a.py", 7),
        ("unresolved_dependency", "backend/pkg/a.py", 8),
    ]
    assert all(BOTH_DECLARED_MESSAGE in message for *_, message in found)
    assert "parameter 'db'" in found[0][3]


def test_annotation_aliases_resolve_across_modules_and_spellings(analyze) -> None:
    a = analyze(
        {
            "pkg/core/__init__.py": """
            from .database import DbSession as ReexportedDb
            """,
            "pkg/core/database.py": """
            from typing import Annotated, TypeAlias
            from fastapi import Depends
            from pkg.deps import get_db

            DbSession = Annotated[str, Depends(get_db)]
            DbTyped: TypeAlias = Annotated[str, Depends(get_db)]
            type DbType = Annotated[str, Depends(get_db)]
            """,
            "pkg/b.py": """
            from pkg.core.database import DbType

            type ImportedTwice = DbType
            """,
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends
            import pkg.core.database as database
            from pkg.core import ReexportedDb
            from pkg.core.database import DbSession, DbTyped, DbType
            from pkg.core.database import DbSession as RenamedDb
            from pkg.b import ImportedTwice
            ChainedDb = DbSession
            type Twice = DbType

            def imported(db: DbSession): ...
            def renamed(db: RenamedDb): ...
            def reexported(db: ReexportedDb): ...
            def typed(db: DbTyped): ...
            def chained(db: ChainedDb): ...
            def attribute(db: database.DbSession): ...
            def quoted(db: "DbSession"): ...
            def pep695(db: DbType): ...
            def pep695_two_levels(db: Twice): ...
            def pep695_two_levels_imported(db: ImportedTwice): ...
            def pep695_inside_annotated(db: Annotated[DbType, "meta"]): ...
            """,
        }
    )
    # The use points at the Depends(...) it came from: DbSession is line 5, DbTyped 6, DbType 7.
    marker_lines = {"typed": 6, "pep695": 7}
    for handler in ("imported", "renamed", "reexported", "typed", "chained", "attribute", "quoted", "pep695"):
        uses = a.params("pkg.a", handler)
        assert ids(uses) == ["pkg.deps:get_db"], handler
        assert (uses[0].file, uses[0].line) == ("backend/pkg/core/database.py", marker_lines.get(handler, 5)), handler
    # FastAPI unpacks a PEP 695 alias once; an alias of an alias, or one inside Annotated, is a plain type.
    assert a.params("pkg.a", "pep695_two_levels") == []
    assert a.params("pkg.a", "pep695_two_levels_imported") == []
    assert a.params("pkg.a", "pep695_inside_annotated") == []


def test_empty_annotated_is_a_limitation_not_a_crash(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends
            from pkg.deps import get_db

            def empty(x: Annotated[()], db=Depends(get_db)): ...
            """
        }
    )
    assert ids(a.params("pkg.a", "empty")) == ["pkg.deps:get_db"]
    ((kind, file, line, message),) = a.problems()
    assert (kind, file, line) == ("unresolved_dependency", "backend/pkg/a.py", 5)
    assert "Annotated[()] has no type argument" in message


def test_positional_only_defaults_are_right_aligned(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user, verify_key

            def f(a, b=1, /, c=Depends(get_db), d=Depends(get_user), *args, e=Depends(verify_key), **kw): ...
            """
        }
    )
    assert [(u.param, u.target.name) for u in a.params("pkg.a", "f")] == [
        ("c", "get_db"),
        ("d", "get_user"),
        ("e", "verify_key"),
    ]


def test_a_name_bound_to_a_marker_counts_as_the_marker(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_user

            CurrentUser = Depends(get_user)

            def handler(u=CurrentUser): ...
            """
        }
    )
    assert ids(a.params("pkg.a", "handler")) == ["pkg.deps:get_user"]


# ── Callable kinds and identity ──────────────────────────────────────────


def test_import_aliases_and_module_attributes_reach_the_definition(analyze) -> None:
    """`get_user as current_user` and `deps.get_user` are the same target (c02)."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg import deps
            from pkg.deps import get_user as current_user

            def renamed(u=Depends(current_user)): ...
            def attribute(u=Depends(deps.get_user)): ...
            """
        }
    )
    for handler in ("renamed", "attribute"):
        (use,) = a.params("pkg.a", handler)
        assert (use.target.id, use.display, use.alias) == ("pkg.deps:get_user", "get_user", None), handler
        assert [t.id for t in a.resolver.closure([use])] == ["pkg.deps:get_user", "pkg.deps:get_db"]


def test_same_named_dependencies_in_two_modules_stay_distinct(analyze) -> None:
    """c14: two `get_ctx` and a method `UserRepo.get_user` never merge."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db

            def get_ctx(db=Depends(get_db)):
                return db

            def a(c=Depends(get_ctx)): ...
            """,
            "pkg/b.py": """
            from fastapi import Depends
            from pkg.deps import verify_key

            def get_ctx(k=Depends(verify_key)):
                return k

            def b(c=Depends(get_ctx)): ...
            """,
            "pkg/users.py": """
            class UserRepo:
                def get_user(self, user_id: int):
                    return user_id
            """,
            "pkg/u.py": """
            from fastapi import Depends
            from pkg.deps import get_user

            def u(user=Depends(get_user)): ...
            """,
        }
    )
    a_use, b_use = a.params("pkg.a", "a")[0], a.params("pkg.b", "b")[0]
    assert (a_use.target.id, b_use.target.id) == ("pkg.a:get_ctx", "pkg.b:get_ctx")
    assert [t.id for t in a.resolver.closure([a_use])] == ["pkg.a:get_ctx", "pkg.deps:get_db"]
    assert [t.id for t in a.resolver.closure([b_use])] == ["pkg.b:get_ctx", "pkg.deps:verify_key"]
    u_use = a.params("pkg.u", "u")[0]
    assert [t.id for t in a.resolver.closure([u_use])] == ["pkg.deps:get_user", "pkg.deps:get_db"]


def test_class_and_instance_dependencies(analyze) -> None:
    """c03: a class reads `__init__`; an instance reads `__call__` (also through an alias and a base)."""
    a = analyze(
        {
            "pkg/deps_cls.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user

            class Pager:
                def __init__(self, db=Depends(get_db), limit: int = 10): ...

            class BaseChecker:
                def __call__(self, user=Depends(get_user)):
                    return user

            class RoleChecker(BaseChecker):
                def __init__(self, role: str):
                    self.role = role

            admin_only = RoleChecker("admin")
            """,
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps_cls import Pager, RoleChecker, admin_only

            def cls(p: Pager = Depends(Pager)): ...
            def instance(u=Depends(RoleChecker("admin"))): ...
            def instance_alias(u=Depends(admin_only)): ...
            """,
        }
    )
    (cls,) = a.params("pkg.a", "cls")
    assert (cls.target.id, cls.target.kind, cls.display) == ("pkg.deps_cls:Pager", "class", "Pager")
    assert ids(a.resolver.sub_deps(cls.target)) == ["pkg.deps:get_db"]
    (inst,) = a.params("pkg.a", "instance")
    assert (inst.target.id, inst.target.kind, inst.display, inst.alias) == (
        "pkg.deps_cls:RoleChecker()",
        "instance",
        "RoleChecker(admin)",
        None,
    )
    assert ids(a.resolver.sub_deps(inst.target)) == ["pkg.deps:get_user"]
    (alias,) = a.params("pkg.a", "instance_alias")
    assert (alias.target.id, alias.display, alias.alias) == (
        "pkg.deps_cls:RoleChecker()",
        "RoleChecker(admin)",
        "admin_only",
    )


def test_methods_lambdas_and_generators(analyze) -> None:
    """c04: bound, static and class methods skip exactly the bound parameter; a
    method bound to an instance is ``C().m``."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user

            class Svc:
                def get_items(self, db=Depends(get_db)):
                    return []

                @staticmethod
                def static_dep(user=Depends(get_user)):
                    return user

                @classmethod
                def cls_dep(cls, db=Depends(get_db)):
                    return db

            svc = Svc()

            async def agen(db=Depends(get_db)):
                yield 1

            def bound(x=Depends(svc.get_items)): ...
            def static(x=Depends(Svc.static_dep)): ...
            def classm(x=Depends(Svc.cls_dep)): ...
            def lam(x=Depends(lambda: 1)): ...
            def gen(x=Depends(agen)): ...
            """
        }
    )
    expected = {
        "bound": ("pkg.a:Svc().get_items", "method", "get_items", ["pkg.deps:get_db"]),
        "static": ("pkg.a:Svc.static_dep", "method", "static_dep", ["pkg.deps:get_user"]),
        "classm": ("pkg.a:Svc.cls_dep", "method", "cls_dep", ["pkg.deps:get_db"]),
        "lam": ("pkg.a:<lambda>@L24", "lambda", "<lambda>", []),
        "gen": ("pkg.a:agen", "function", "agen", ["pkg.deps:get_db"]),
    }
    for handler, (target_id, kind, display, subs) in expected.items():
        (use,) = a.params("pkg.a", handler)
        assert (use.target.id, use.target.kind, use.display) == (target_id, kind, display), handler
        assert ids(a.resolver.sub_deps(use.target)) == subs, handler


def test_a_class_its_instances_and_its_bound_methods_have_their_own_ids(analyze) -> None:
    """``Depends(C)`` reads ``__init__`` and ``Depends(C())`` reads ``__call__``;
    ``Depends(C.m)`` reads every parameter of ``m`` and ``Depends(C().m)`` skips
    the first. One id each, so neither form takes the other's sub-dependencies."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user, verify_key
            class C:
                def __init__(self, db=Depends(get_db)): ...
                def __call__(self, user=Depends(get_user)): ...
                def m(first=Depends(get_user), key=Depends(verify_key)): ...
            def h(i=Depends(C()), c=Depends(C), f=Depends(C.m), b=Depends(C().m)): ...
            """
        }
    )
    uses = a.params("pkg.a", "h")
    assert [(u.target.id, u.target.kind, ids(a.resolver.sub_deps(u.target))) for u in uses] == [
        ("pkg.a:C()", "instance", ["pkg.deps:get_user"]),
        ("pkg.a:C", "class", ["pkg.deps:get_db"]),
        ("pkg.a:C.m", "method", ["pkg.deps:get_user", "pkg.deps:verify_key"]),
        ("pkg.a:C().m", "method", ["pkg.deps:verify_key"]),
    ]
    assert [u.display for u in uses] == ["C()", "C", "m", "m"]
    assert a.problems() == []


def test_a_factory_returning_forms_with_other_parameters_under_one_id_is_reported(analyze) -> None:
    """Which form ``make(flag)`` returns depends on ``flag`` at runtime: both
    share the id ``pkg.a:make``, so every call gets the sub-dependencies of
    both, and the factory is reported."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user
            def make(flag):
                class Local:
                    def m(first=Depends(get_user), second=Depends(get_db)): ...
                if flag:
                    return Local().m
                return Local.m
            def h(x=Depends(make(False))): ...
            """
        }
    )
    (use,) = a.params("pkg.a", "h")
    assert sorted(set(ids(a.resolver.sub_deps(use.target)))) == ["pkg.deps:get_db", "pkg.deps:get_user"]
    ((kind, _, _, message),) = a.problems()
    assert kind == "shared_dependency_id"
    assert "dependency factory make returns callables FastAPI reads with different parameters" in message
    assert "(pkg.a:make)" in message


def test_factory_results_take_their_deps_from_the_returned_callable_only(analyze) -> None:
    """c16: the id is the factory's; neither its own parameters nor unused nested defs count."""
    a = analyze(
        {
            "pkg/factories.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user, get_admin

            def require_scope(scope: str):
                def unused(admin=Depends(get_admin)):
                    return admin

                async def _inner(db=Depends(get_db)):
                    return scope

                return _inner

            def require_role(role: str, user=Depends(get_user)):
                def checker(k=Depends(get_db)):
                    return role
                return checker

            def require_lambda(x):
                return lambda user=Depends(get_user): user

            def opaque(x):
                return x
            """,
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.factories import require_scope, require_role as rr, require_lambda, opaque

            _require_list_access = rr("admin", extra=1)

            def scope(x=Depends(require_scope("s"))): ...
            def role(x=Depends(rr("admin"))): ...
            def guarded(x=Depends(_require_list_access)): ...
            def lam(x=Depends(require_lambda(1))): ...
            def unknown(x=Depends(opaque(3))): ...
            """,
        }
    )
    (scope,) = a.params("pkg.a", "scope")
    assert (scope.target.id, scope.target.kind, scope.display) == (
        "pkg.factories:require_scope",
        "factory_result",
        "require_scope(s)",
    )
    assert [t.id for t in a.resolver.closure([scope])] == ["pkg.factories:require_scope", "pkg.deps:get_db"]
    (role,) = a.params("pkg.a", "role")
    assert (role.target.id, role.display) == ("pkg.factories:require_role", "require_role(admin)")
    assert ids(a.resolver.sub_deps(role.target)) == ["pkg.deps:get_db"]
    (guarded,) = a.params("pkg.a", "guarded")
    assert (guarded.target.id, guarded.display, guarded.alias) == (
        "pkg.factories:require_role",
        "require_role(admin, extra=1)",
        "_require_list_access",
    )
    (lam,) = a.params("pkg.a", "lam")
    assert ids(a.resolver.sub_deps(lam.target)) == ["pkg.deps:get_user"]
    (unknown,) = a.params("pkg.a", "unknown")
    assert (unknown.target.id, unknown.target.kind) == ("pkg.factories:opaque", "factory_result")
    assert a.resolver.sub_deps(unknown.target) == []
    ((kind, file, line, message),) = a.problems()
    assert (kind, file, line) == ("unresolved_dependency", "backend/pkg/factories.py", 22)
    assert "opaque" in message and "returned callable" in message


def test_names_resolve_in_their_own_scope_and_position(analyze) -> None:
    """c25: a later local def shadows an import; a nested def does not touch module scope."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user

            def get_db():
                yield "local"

            def outer():
                def get_user():
                    return 1
                return get_user

            def shadow(db=Depends(get_db)): ...
            def u(x=Depends(get_user)): ...
            """
        }
    )
    assert ids(a.params("pkg.a", "shadow")) == ["pkg.a:get_db"]
    assert ids(a.params("pkg.a", "u")) == ["pkg.deps:get_user"]


def test_unresolvable_targets_get_an_unresolved_id_and_a_limitation(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from fastapi.security import HTTPBearer

            def missing(x=Depends(nowhere)): ...
            def external(x=Depends(HTTPBearer())): ...
            def from_param(dep, x=Depends(int)): ...
            """
        }
    )
    (missing,) = a.params("pkg.a", "missing")
    assert (missing.target.id, missing.target.kind, missing.display) == (
        "unresolved:pkg.a:nowhere",
        "unresolved",
        "nowhere",
    )
    (external,) = a.params("pkg.a", "external")
    assert external.target.id == "unresolved:pkg.a:HTTPBearer()"
    (builtin,) = a.params("pkg.a", "from_param")
    assert builtin.target.kind == "unresolved"
    found = a.problems()
    assert [(k, line) for k, _f, line, _m in found] == [
        ("unresolved_dependency", 4),
        ("unresolved_dependency", 5),
        ("unresolved_dependency", 6),
    ]
    assert "Depends(nowhere)" in found[0][3]
    assert "fastapi.security.HTTPBearer" in found[1][3]


# ── Route layers, order and closures ─────────────────────────────────────


def _route(a: Analysis, module: str, handler: str, layers: tuple[DepLayer, ...]) -> LiveRoute:
    fn = a.function(module, handler)
    return LiveRoute(
        method="GET",
        path=f"/{handler}",
        kind="http",
        handler=handler,
        handler_id=f"{module}:{handler}",
        file=a.module(module).rel,
        line=fn.lineno,
        registered_file=a.module(module).rel,
        registered_line=fn.lineno,
        chain=("pkg.main:app",),
        mount="static",
        layers=layers,
        order=0,
        endpoint=Located(module, fn),
    )


def test_route_deps_follow_fastapi_order_without_dedupe(analyze) -> None:
    """App, include and router layers, then the decorator, then parameters (c12 keeps duplicates)."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends, Security
            from pkg.deps import get_db, get_user, verify_key, audit

            APP = [Depends(audit)]
            INCLUDE = [Security(verify_key)]
            OWN = [Depends(verify_key)]
            DECORATOR = [Depends(get_user), "not a marker"]

            def handler(db=Depends(get_db)): ...
            """
        }
    )
    route = _route(
        a,
        "pkg.a",
        "handler",
        (
            DepLayer("app", "pkg.main:app", a.elements("pkg.a", "APP")),
            DepLayer("router", "pkg.main:app->pkg.a:router", a.elements("pkg.a", "INCLUDE")),
            DepLayer("router", "pkg.a:router", a.elements("pkg.a", "OWN")),
            DepLayer("router", "pkg.a:other", Unknown("unresolved", "COMMON")),
            DepLayer("route decorator", "pkg.a:handler", a.elements("pkg.a", "DECORATOR")),
        ),
    )
    uses = a.resolver.route_deps(route)
    assert [(u.via, u.target.name, u.marker, u.param) for u in uses] == [
        ("app", "audit", "Depends", None),
        ("router", "verify_key", "Security", None),
        ("router", "verify_key", "Depends", None),
        ("route decorator", "get_user", "Depends", None),
        ("direct", "get_db", "Depends", "db"),
    ]
    assert [t.name for t in a.resolver.closure(uses)] == ["audit", "verify_key", "get_user", "get_db"]
    ((kind, file, line, message),) = a.problems()
    assert (kind, file, line) == ("unresolved_dependencies", "backend/pkg/a.py", 7)
    assert "'not a marker'" in message


def test_closure_is_cycle_safe(analyze) -> None:
    """Lazily evaluated annotations let two dependencies name each other."""
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends

            def first(x=Depends(lambda: 0), y: Annotated[int, Depends(second)] = 0): ...
            def second(z: Annotated[int, Depends(first)]): ...
            def handler(f=Depends(first)): ...
            """
        }
    )
    uses = a.params("pkg.a", "handler")
    assert [t.id for t in a.resolver.closure(uses)] == ["pkg.a:first", "pkg.a:<lambda>@L4", "pkg.a:second"]


def test_alias_cycles_and_rebinding_follow_module_execution(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from typing import Annotated
            from fastapi import Depends
            from pkg.deps import get_db, get_user

            A = B
            B = A
            Shadowed = Annotated[str, Depends(get_db)]
            def Shadowed(): ...
            Rebound = Annotated[str, Depends(get_db)]
            Rebound = Annotated[str, Depends(get_user)]

            def cycle(x: A): ...
            def shadowed(x: Shadowed): ...
            def rebound(x: Rebound): ...
            """
        }
    )
    assert a.params("pkg.a", "cycle") == []
    assert a.params("pkg.a", "shadowed") == [], "a later def rebinds the module name"
    assert ids(a.params("pkg.a", "rebound")) == ["pkg.deps:get_user"], "the last binding wins"


def test_wrapper_aliases_are_identified_by_their_binding_instead_of_making_the_wrapper_a_dependency(analyze) -> None:
    """`get_settings = lru_cache(_settings)` must not register `lru_cache` as a dependency."""
    a = analyze(
        {
            "pkg/a.py": """
            from functools import lru_cache
            from fastapi import Depends

            def _settings(): ...

            get_settings = lru_cache(_settings)

            def cached(s=Depends(get_settings)): ...
            """
        }
    )
    (use,) = a.params("pkg.a", "cached")
    assert (use.target.id, use.target.kind) == ("pkg.a:get_settings", "external")
    ((kind, _file, line, message),) = a.problems()
    assert (kind, line) == ("unresolved_dependency", 8)
    assert "functools.lru_cache" in message


def test_an_external_object_has_one_id_however_it_is_imported(analyze) -> None:
    """`guard = make_guard(...)` is built by a library the analysis cannot see into; every
    module that imports it, under any name, depends on the one object bound in `pkg.auth`."""
    a = analyze(
        {
            "pkg/auth.py": """
            from lib import make_guard

            guard = make_guard(active=True)
            """,
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.auth import guard

            def first(user=Depends(guard)): ...
            """,
            "pkg/b.py": """
            from fastapi import Depends
            from pkg.auth import guard as current

            def second(user=Depends(current)): ...
            """,
            "pkg/c.py": """
            from fastapi import Depends
            from pkg import auth
            from pkg.b import current as who

            def third(user=Depends(who), other=Depends(auth.guard)): ...
            """,
        }
    )
    uses = [
        *a.params("pkg.a", "first"),
        *a.params("pkg.b", "second"),
        *a.params("pkg.c", "third"),
    ]
    assert ids(uses) == ["pkg.auth:guard"] * 4
    assert [u.display for u in uses] == ["guard", "current", "who", "auth.guard"]
    target = uses[0].target
    assert (target.kind, target.module, target.name, target.file, target.line) == (
        "external",
        "pkg.auth",
        "guard",
        "backend/pkg/auth.py",
        3,
    )
    assert target.bound_to == "make_guard(active=True)"
    assert [(kind, file) for kind, file, _line, _message in a.problems()] == [
        ("unresolved_dependency", "backend/pkg/a.py"),
        ("unresolved_dependency", "backend/pkg/b.py"),
        ("unresolved_dependency", "backend/pkg/c.py"),
        ("unresolved_dependency", "backend/pkg/c.py"),
    ]


# ── What FastAPI calls: factories, wrappers, partials, dataclasses ───────


def test_a_factory_result_is_identified_as_what_it_returns(analyze) -> None:
    """A closure keeps the factory's id, a module function its own, an
    instance its class's with ``()``; a lambda made in a function is that function."""
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, get_user
            class Checker:
                def __call__(self, user=Depends(get_user)): ...
            def closure(role):
                def check(db=Depends(get_db)): ...
                return check
            def module_function(role):
                return get_db
            def instance(role):
                return Checker()
            def lam(role):
                return lambda user=Depends(get_user): user
            def h(a=Depends(closure("x")), b=Depends(module_function("x")), c=Depends(instance("x")), d=Depends(lam("x"))): ...
            """
        }
    )
    uses = a.params("pkg.a", "h")
    assert [(u.target.id, u.target.kind, u.display) for u in uses] == [
        ("pkg.a:closure", "factory_result", "closure(x)"),
        ("pkg.deps:get_db", "factory_result", "module_function(x)"),
        ("pkg.a:Checker()", "factory_result", "instance(x)"),
        ("pkg.a:lam", "factory_result", "lam(x)"),
    ]
    assert [t.id for t in a.resolver.closure(uses)] == [
        "pkg.a:closure",
        "pkg.deps:get_db",
        "pkg.a:Checker()",
        "pkg.deps:get_user",
        "pkg.a:lam",
    ]


def test_a_wraps_factory_takes_the_wrapped_functions_parameters(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            import functools
            from fastapi import Depends
            from pkg.deps import get_db, verify_key
            def with_retry(fn):
                @functools.wraps(fn)
                def inner(*args, **kwargs):
                    return fn(*args, **kwargs)
                return inner
            def get_repo(db=Depends(get_db)): ...
            def get_key(ok=Depends(verify_key)): ...
            def h(r=Depends(with_retry(get_repo)), k=Depends(with_retry(get_key))): ...
            """
        }
    )
    uses = a.params("pkg.a", "h")
    assert ids(uses) == ["pkg.a:get_repo", "pkg.a:get_key"]
    assert [t.id for t in a.resolver.closure(uses)] == [
        "pkg.a:get_repo",
        "pkg.deps:get_db",
        "pkg.a:get_key",
        "pkg.deps:verify_key",
    ]
    assert a.problems() == []


def test_a_partial_is_its_function_without_the_parameters_it_binds(analyze) -> None:
    """FastAPI reads a partial's signature: leading positional parameters it
    fills are gone, and keyword values it binds become defaults. Other uses of
    the function keep its own parameters."""
    a = analyze(
        {
            "pkg/a.py": """
            import functools
            from typing import Annotated
            from fastapi import Depends
            from pkg.deps import audit, get_db, verify_key
            def paged(limit: int, db=Depends(get_db), key=Depends(verify_key)): ...
            def ann(limit: int, db: Annotated[str, Depends(get_db)]): ...
            def h(
                keeps=Depends(functools.partial(paged, limit=10)),
                kw_none=Depends(functools.partial(paged, db=None)),
                kw_other=Depends(functools.partial(paged, db=Depends(audit))),
                positional=Depends(functools.partial(paged, 10, None)),
                nested=Depends(functools.partial(functools.partial(paged, 10), key=None)),
                annotated=Depends(functools.partial(ann, db=None)),
                plain=Depends(paged),
            ): ...
            """
        }
    )
    uses = a.params("pkg.a", "h")
    assert ids(uses) == ["pkg.a:paged"] * 5 + ["pkg.a:ann", "pkg.a:paged"]
    subs = {use.param: ids(a.resolver.sub_deps(use.target)) for use in uses}
    assert subs == {
        "keeps": ["pkg.deps:get_db", "pkg.deps:verify_key"],
        "kw_none": ["pkg.deps:verify_key"],
        "kw_other": ["pkg.deps:audit", "pkg.deps:verify_key"],
        "positional": ["pkg.deps:verify_key"],
        "nested": ["pkg.deps:get_db"],
        # A dependency in Annotated stays: the bound value is only a default.
        "annotated": ["pkg.deps:get_db"],
        "plain": ["pkg.deps:get_db", "pkg.deps:verify_key"],
    }
    assert a.problems() == []


def test_a_partial_binding_through_star_arguments_is_reported(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            import functools
            from fastapi import Depends
            from pkg.deps import get_db
            def paged(limit: int, db=Depends(get_db)): ...
            KW = {"db": None}
            def h(p=Depends(functools.partial(paged, **KW))): ...
            """
        }
    )
    assert [u.target.kind for u in a.params("pkg.a", "h")] == ["unresolved"]
    assert [p[0] for p in a.problems()] == ["unresolved_dependency"]


def test_dataclass_fields_are_the_parameters_and_other_class_decorators_are_reported(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            import dataclasses
            from dataclasses import dataclass, field
            from typing import ClassVar
            from fastapi import Depends
            from pkg.deps import get_db, get_user, verify_key
            def register(cls):
                return cls
            @dataclass
            class Base:
                user: str = Depends(get_user)
            @dataclass
            class Params(Base):
                q: str = ""
                db: str = Depends(get_db)
                skipped: str = field(default=Depends(verify_key), init=False)
                shared: ClassVar[str] = Depends(verify_key)
                made: list = field(default_factory=list)
            @register
            class Unknown:
                q: str = ""
            def h(p: Params = Depends(), u: Unknown = Depends()): ...
            """
        }
    )
    uses = a.params("pkg.a", "h")
    assert ids(uses) == ["pkg.a:Params", "pkg.a:Unknown"]
    assert [t.id for t in a.resolver.closure(uses[:1])] == ["pkg.a:Params", "pkg.deps:get_user", "pkg.deps:get_db"]
    (problem,) = a.problems()
    assert "dependency class Unknown: class decorator @register may change its constructor" in problem[3]


def test_a_dataclass_constructor_comes_before_an_inherited_init(analyze) -> None:
    # @dataclass generates __init__ unless the class body defines one or init=False; a base's __init__ does not stop it.
    a = analyze(
        {
            "pkg/a.py": """
            from dataclasses import dataclass
            from fastapi import Depends
            from pkg.deps import audit, get_db, verify_key
            FLAG = False
            class Base:
                def __init__(self, ok=Depends(audit)): ...
            @dataclass
            class Child(Base):
                key: str = Depends(verify_key)
            class Plain(Child):
                pass
            @dataclass(init=False)
            class NoInit(Base):
                db: str = Depends(get_db)
            @dataclass
            class Own(Base):
                key: str = Depends(verify_key)
                def __init__(self, db=Depends(get_db)): ...
            @dataclass(init=FLAG)
            class Maybe(Base):
                key: str = Depends(verify_key)
            def h(c: Child = Depends(), p: Plain = Depends(), n: NoInit = Depends(), o: Own = Depends()): ...
            def m(x: Maybe = Depends()): ...
            """
        }
    )
    uses = a.params("pkg.a", "h")
    closures = [[t.id for t in a.resolver.closure([use])] for use in uses]
    assert closures == [
        ["pkg.a:Child", "pkg.deps:verify_key"],
        ["pkg.a:Plain", "pkg.deps:verify_key"],
        ["pkg.a:NoInit", "pkg.deps:audit"],
        ["pkg.a:Own", "pkg.deps:get_db"],
    ]
    assert a.problems() == []
    assert [t.id for t in a.resolver.closure(a.params("pkg.a", "m"))] == ["pkg.a:Maybe"]
    (problem,) = a.problems()
    assert problem[:3] == ("unresolved_dependency", "backend/pkg/a.py", 20)
    assert "dependency class Maybe: @dataclass(init=FLAG) may or may not generate __init__" in problem[3]


def test_a_decorated_dependency_without_wraps_is_reported(analyze) -> None:
    a = analyze(
        {
            "pkg/a.py": """
            from fastapi import Depends
            from pkg.deps import get_db, verify_key
            def needs_verify(fn):
                def wrapper(ok=Depends(verify_key)):
                    return fn()
                return wrapper
            @needs_verify
            def current(db=Depends(get_db)): ...
            def h(c=Depends(current)): ...
            """
        }
    )
    assert ids(a.params("pkg.a", "h")) == ["pkg.a:current"]
    (problem,) = a.problems()
    assert problem[:3] == ("opaque_decorator", "backend/pkg/a.py", 7)
    assert "needs_verify() returns wrapper without functools.wraps" in problem[3]


# ── Callables of the package named by their runtime id (the library capture's leaves) ──

THINGS = """
from fastapi import Depends

from pkg.deps import get_db


class Plain:
    def __init__(self, db=Depends(get_db)):
        self.db = db


class Guard:
    def __call__(self, db=Depends(get_db)):
        return db

    def check(self, db=Depends(get_db)):
        return db


def make_dep(role):
    def dependency(db=Depends(get_db)):
        return role

    return dependency


def make_lambda():
    return lambda db=Depends(get_db): db
"""


def test_a_callable_of_the_package_is_found_by_the_id_the_runtime_reports(analyze) -> None:
    a = analyze({"pkg/things.py": THINGS})
    found = {
        leaf_id: a.resolver.leaf(leaf_id)
        for leaf_id in (
            "pkg.deps:get_user",
            "pkg.things:Plain",
            "pkg.things:Guard()",
            "pkg.things:Guard().check",
            "pkg.things:Guard.check",
        )
    }
    assert {i: (t.kind if t else None) for i, t in found.items()} == {
        "pkg.deps:get_user": "function",
        "pkg.things:Plain": "class",
        "pkg.things:Guard()": "instance",
        "pkg.things:Guard().check": "method",
        "pkg.things:Guard.check": "method",
    }
    assert ids(a.resolver.sub_deps(found["pkg.deps:get_user"])) == ["pkg.deps:get_db"]
    assert ids(a.resolver.sub_deps(found["pkg.things:Guard()"])) == ["pkg.deps:get_db"]


@pytest.mark.parametrize(
    "leaf_id",
    [
        "pkg.deps:nope",
        "pkg.nowhere:thing",
        "pkg.deps",
        ":x",
        "pkg.things:Plain()",
        "pkg.things:Plain.nope",
        "pkg.things:make_dep",
        "pkg.things:make_lambda",
        "pkg.things:<lambda>@L1",
    ],
)
def test_an_id_the_package_cannot_be_matched_to_is_not_found(analyze, leaf_id: str) -> None:
    """A name the package lacks, an instance of a class without ``__call__``, and a
    factory whose closure FastAPI identifies by the factory (so the closure's parameters
    are unknown) all stay unresolved."""
    a = analyze({"pkg/things.py": THINGS})
    assert a.resolver.leaf(leaf_id) is None
