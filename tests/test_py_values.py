"""Tests for py_values.ValueEvaluator: name resolution across modules, barrels and
star imports, and evaluation of strings, sequences and mappings."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from code_intel.backend_model import Def, External, Located, ModuleRef, Unknown, Value
from code_intel.py_sources import PySources
from code_intel.py_values import ESCAPED_REASON, ValueEvaluator


def evaluator(tmp_path: Path, files: dict[str, str], **kwargs) -> ValueEvaluator:
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    assert not any(p.is_diagnostic for p in sources.problems), sources.problems
    return ValueEvaluator(sources, **kwargs)


def kwarg(ev: ValueEvaluator, module: str, line: int, name: str) -> Located:
    """The ``name=`` keyword value of the call on ``line``, located at its statement."""
    mod = ev.sources.module(module)
    # pyrefly: ignore[missing-attribute]
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.Call) and node.lineno == line:
            for keyword in node.keywords:
                if keyword.arg == name:
                    return Located(module, keyword.value, ev.sources.scope(module).index_of(node))
    raise AssertionError(f"{module}:{line}: no call with {name}=")


def expr(ev: ValueEvaluator, module: str, source: str, at: int | None = None) -> Located:
    """Parse ``source`` as an expression used in ``module`` (at module end by default)."""
    return Located(module, ast.parse(source, mode="eval").body, at)


def texts(locs) -> list[str]:
    return [ast.unparse(loc.node) for loc in locs]


BASE = {
    "pkg/__init__.py": "",
    "pkg/deps.py": """
        def verify_key(): ...
        def audit(): ...
        """,
}


# ── resolve_name / resolve_expr ──────────────────────────────────────────


def test_names_resolve_through_imports_aliases_and_barrels(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/api/__init__.py": "from .routes import router as api_router\n",
            "pkg/api/routes.py": "from fastapi import APIRouter\nrouter = APIRouter()\npublic = router\n",
            "pkg/main.py": """
                import fastapi
                from pkg.api import api_router
                from pkg import api
                import pkg.api.routes as routes_mod
                from pkg.api.routes import public
                """,
        },
    )
    [target] = ev.resolve_name("pkg.main", "api_router", None)
    assert isinstance(target, Value) and target.loc.module == "pkg.api.routes"
    assert ast.unparse(target.loc.node) == "APIRouter()"
    assert ev.resolve_name("pkg.main", "api", None) == [ModuleRef("pkg.api")]
    assert ev.resolve_name("pkg.main", "routes_mod", None) == [ModuleRef("pkg.api.routes")]
    assert ev.resolve_name("pkg.main", "fastapi", None) == [External("fastapi")]
    # `public = router` is an alias: resolve_expr follows it to the call.
    [alias] = ev.resolve_expr(expr(ev, "pkg.main", "public"))
    assert isinstance(alias, Value) and ast.unparse(alias.loc.node) == "APIRouter()"
    [via_module] = ev.resolve_expr(expr(ev, "pkg.main", "routes_mod.router"))
    assert via_module == alias
    [via_package] = ev.resolve_expr(expr(ev, "pkg.main", "api.routes.router"))
    assert via_package == alias
    assert ev.resolve_expr(expr(ev, "pkg.main", "fastapi.Depends")) == [External("fastapi.Depends")]


def test_from_package_import_submodule(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/routes/__init__.py": "from . import one\n",
            "pkg/routes/one.py": "router = 1\n",
            "pkg/routes/two.py": "router = 2\n",
            "pkg/main.py": "from pkg.routes import one, two\n",
        },
    )
    assert ev.resolve_name("pkg.routes", "one", None) == [ModuleRef("pkg.routes.one")]
    assert ev.resolve_name("pkg.main", "two", None) == [ModuleRef("pkg.routes.two")]
    [value] = ev.resolve_expr(expr(ev, "pkg.main", "one.router"))
    # pyrefly: ignore[missing-attribute]
    assert ast.unparse(value.loc.node) == "1"


def test_star_imports_follow_all_or_public_names(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/a.py": "__all__ = ['router']\nrouter = 'a'\nhidden = 'h'\n",
            "pkg/b.py": "other = 'b'\n_private = 'p'\n",
            "pkg/barrel.py": "from .a import *\nfrom .b import *\n",
        },
    )
    assert ev.string(expr(ev, "pkg.barrel", "router")) == "a"
    assert ev.string(expr(ev, "pkg.barrel", "other")) == "b"
    assert ev.resolve_name("pkg.barrel", "hidden", None) == [Unknown("unbound", "hidden")]
    assert ev.resolve_name("pkg.barrel", "_private", None) == [Unknown("unbound", "_private")]


def test_a_star_import_keeps_a_name_another_module_adds_to_all(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/a.py": "__all__ = []\nrouter = 'a'\n",
            "pkg/grow.py": "import pkg.a\npkg.a.__all__.append('router')\n",
            "pkg/barrel.py": "import pkg.grow\nfrom .a import *\n",
        },
    )
    assert ev.string(expr(ev, "pkg.barrel", "router")) == "a"


def test_builtins_unbound_and_missing_names(tmp_path: Path) -> None:
    ev = evaluator(tmp_path, {**BASE, "pkg/m.py": "from pkg.deps import nope\nfrom os.path import join\n"})
    assert ev.resolve_name("pkg.m", "dict", None) == [External("builtins.dict")]
    assert ev.resolve_name("pkg.m", "missing", None) == [Unknown("unbound", "missing")]
    assert ev.resolve_name("pkg.m", "nope", None) == [Unknown("missing name", "pkg.deps.nope")]
    assert ev.resolve_name("pkg.m", "join", None) == [External("os.path.join")]


def test_defs_classes_members_instances_and_lambdas(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/svc.py": """
                class Base:
                    def inherited(self): ...
                class Svc(Base):
                    @staticmethod
                    def static_dep(): ...
                    def get_items(self): ...
                svc = Svc()
                checker = lambda: 1
                """,
        },
    )
    [svc_class] = ev.resolve_name("pkg.svc", "Svc", None)
    assert isinstance(svc_class, Def) and svc_class.qualname == "Svc"
    [static] = ev.resolve_expr(expr(ev, "pkg.svc", "Svc.static_dep"))
    assert isinstance(static, Def) and static.qualname == "Svc.static_dep"
    [bound] = ev.resolve_expr(expr(ev, "pkg.svc", "svc.get_items"))
    assert isinstance(bound, Def) and bound.qualname == "Svc.get_items"
    [inherited] = ev.resolve_expr(expr(ev, "pkg.svc", "Svc.inherited"))
    # pyrefly: ignore[missing-attribute]
    assert inherited.qualname == "Base.inherited"
    [lam] = ev.resolve_expr(expr(ev, "pkg.svc", "checker"))
    assert isinstance(lam, Def) and lam.qualname == "<lambda>@L8"


def test_a_use_resolves_to_the_binding_live_at_its_position(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/m.py": """
                from fastapi import APIRouter
                PREFIX = "/old"
                router = APIRouter(prefix=PREFIX)
                PREFIX = "/new"
                late = APIRouter(prefix=PREFIX)
                """,
        },
    )
    assert ev.string(kwarg(ev, "pkg.m", 3, "prefix")) == "/old"
    assert ev.string(kwarg(ev, "pkg.m", 5, "prefix")) == "/new"
    assert ev.string(expr(ev, "pkg.m", "PREFIX")) == "/new"


def test_conditional_bindings_with_different_values_are_ambiguous(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/m.py": """
                import os
                if os.environ.get("BETA"):
                    P = "/beta"
                else:
                    P = "/stable"
                if os.environ.get("X"):
                    Q = "/same"
                else:
                    Q = "/same"
                """,
        },
    )
    assert len(ev.resolve_name("pkg.m", "P", None)) == 2
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "P")).reason == "ambiguous"
    assert ev.string(expr(ev, "pkg.m", "Q")) == "/same"


def test_cycles_and_depth_give_unknown(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/a.py": "from pkg.b import X\n",
            "pkg/b.py": "from pkg.a import X\n",
            "pkg/chain.py": "A0 = '/end'\n" + "".join(f"A{i} = A{i - 1}\n" for i in range(1, 40)),
        },
        max_depth=8,
    )
    assert ev.resolve_name("pkg.a", "X", None) == [Unknown("cycle", "X")]
    assert ev.string(expr(ev, "pkg.chain", "A3")) == "/end"
    result = ev.string(expr(ev, "pkg.chain", "A39"))
    assert isinstance(result, Unknown) and result.reason == "depth"


def test_a_type_alias_is_its_own_value_locally_and_across_imports(tmp_path: Path) -> None:
    """A PEP 695 alias name holds a TypeAliasType at runtime, so resolution stops
    at the ``type`` statement instead of following the aliased expression; an
    alias of an alias stays two separate aliases even when it is imported."""
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/types.py": """
                PREFIX = "/p"
                type Inner = PREFIX
                type Outer = Inner
                """,
            "pkg/main.py": "from pkg.types import Outer, Inner\nAlias = Outer\n",
        },
    )
    for module, name, alias in (
        ("pkg.types", "Outer", "Outer"),
        ("pkg.main", "Outer", "Outer"),
        ("pkg.main", "Inner", "Inner"),
        ("pkg.main", "Alias", "Outer"),
    ):
        [target] = ev.resolve_expr(expr(ev, module, name))
        assert isinstance(target, Value) and target.loc.module == "pkg.types", (module, name)
        assert isinstance(target.loc.node, ast.TypeAlias) and target.loc.node.name.id == alias, (module, name)
    # The aliased value is never read as the alias's own value.
    assert isinstance(ev.string(expr(ev, "pkg.main", "Inner")), Unknown)


# ── string ───────────────────────────────────────────────────────────────


def test_strings_from_constants_fstrings_and_concatenation(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/paths.py": "API = '/api'\nfrom typing import Final\nVERSION: Final = 'v9'\n",
            "pkg/m.py": """
                from pkg.paths import API, VERSION
                from pkg import paths
                _PREFIX = "/a"
                COUNT = 3
                """,
        },
    )
    assert ev.string(expr(ev, "pkg.m", "_PREFIX")) == "/a"
    assert ev.string(expr(ev, "pkg.m", "API + '/x'")) == "/api/x"
    assert ev.string(expr(ev, "pkg.m", "f'{API}/{VERSION}'")) == "/api/v9"
    assert ev.string(expr(ev, "pkg.m", "paths.API")) == "/api"
    assert ev.string(expr(ev, "pkg.m", "'/x' + '/y'")) == "/x/y"
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "f'{API!r}'")).reason == "formatted value"
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "COUNT")).reason == "not a string"
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "get_path()")).reason == "not a string"
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "UNDEFINED")).reason == "unbound"


# ── sequence / mapping ───────────────────────────────────────────────────


def test_sequences_from_literals_names_concatenation_and_starred(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/m.py": """
                from fastapi import Depends
                from pkg.deps import verify_key, audit
                COMMON = [Depends(verify_key)]
                BASE = (Depends(audit),)
                MORE = COMMON + [Depends(audit)]
                """,
        },
    )
    assert texts(ev.sequence(expr(ev, "pkg.m", "COMMON"))) == ["Depends(verify_key)"]
    assert texts(ev.sequence(expr(ev, "pkg.m", "MORE"))) == ["Depends(verify_key)", "Depends(audit)"]
    assert texts(ev.sequence(expr(ev, "pkg.m", "[*BASE, *COMMON, Depends(x)]"))) == [
        "Depends(audit)",
        "Depends(verify_key)",
        "Depends(x)",
    ]
    # pyrefly: ignore[not-iterable]
    [element] = ev.sequence(expr(ev, "pkg.m", "COMMON"))
    # Elements keep where they were written, so their names resolve there.
    assert element.module == "pkg.m" and element.at == 2
    # pyrefly: ignore[missing-attribute]
    assert ev.sequence(expr(ev, "pkg.m", "make()")).reason == "not a sequence"


def test_mappings_for_kwargs(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/m.py": """
                from fastapi import Depends
                from pkg.deps import verify_key
                MOUNT = dict(prefix="/kw", dependencies=[Depends(verify_key)])
                EXTRA = {**dict(prefix="/kw"), "tags": ["t"]}
                SHARED = dict(prefix="/kw")
                COPY = {**SHARED}
                """,
        },
    )
    mount = ev.mapping(expr(ev, "pkg.m", "MOUNT"))
    # pyrefly: ignore[bad-argument-type]
    assert sorted(mount) == ["dependencies", "prefix"]
    # pyrefly: ignore[bad-index]
    assert ev.string(mount["prefix"]) == "/kw"
    # pyrefly: ignore[bad-index]
    assert texts(ev.sequence(mount["dependencies"])) == ["Depends(verify_key)"]
    # pyrefly: ignore[bad-argument-type]
    assert sorted(ev.mapping(expr(ev, "pkg.m", "EXTRA"))) == ["prefix", "tags"]
    # A copy shares the values, so a change through COPY may change SHARED's.
    # pyrefly: ignore[missing-attribute]
    assert ev.mapping(expr(ev, "pkg.m", "SHARED")).reason == ESCAPED_REASON
    # pyrefly: ignore[missing-attribute]
    assert ev.mapping(expr(ev, "pkg.m", "{KEY: 1}")).reason == "non-string key"
    # pyrefly: ignore[missing-attribute]
    assert ev.mapping(expr(ev, "pkg.m", "build()")).reason == "not a mapping"


@pytest.mark.parametrize("source", ["[1, 2]", "'/x'"])
def test_other_expressions_resolve_to_their_own_value(tmp_path: Path, source: str) -> None:
    ev = evaluator(tmp_path, {**BASE})
    loc = expr(ev, "pkg.deps", source)
    assert ev.resolve_expr(loc) == [Value(loc)]


# ── Values that may have changed ─────────────────────────────────────────


def test_a_value_changed_in_place_has_no_value(tmp_path: Path) -> None:
    """``X.append(...)``, ``X[k] = v`` or ``X.attr = v`` before a use, inside a
    function, or from another module, leaves the evaluator without a value; a
    change after the use does not."""
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/m.py": """
                from fastapi import Depends
                from pkg.deps import verify_key, audit
                COMMON = [Depends(verify_key)]
                COMMON.append(Depends(audit))
                OPTS = {"prefix": "/a"}
                OPTS["prefix"] = "/b"
                LATER = "/later"
                use = LATER
                LATER_LIST = ["/x"]
                def grow():
                    LATER_LIST.append("/y")
                PREFIX = "/p"
                """,
            "pkg/other.py": "from pkg.m import PREFIX\nimport pkg.m\npkg.m.LATER = '/z'\n",
        },
    )
    assert ev.sequence(expr(ev, "pkg.m", "COMMON")) == Unknown("changed in place", "COMMON")
    assert ev.mapping(expr(ev, "pkg.m", "OPTS")) == Unknown("changed in place", "OPTS")
    assert ev.sequence(expr(ev, "pkg.m", "LATER_LIST")) == Unknown("changed in place", "LATER_LIST")
    assert ev.string(expr(ev, "pkg.m", "LATER")) == Unknown("changed in place by another module", "pkg.m.LATER")
    assert ev.string(expr(ev, "pkg.m", "PREFIX")) == "/p"
    # Name resolution itself is unchanged: routers and dependencies still resolve.
    assert [type(t).__name__ for t in ev.resolve_name("pkg.m", "COMMON", None)] == ["Value"]


def test_an_instance_attribute_set_at_runtime_has_no_value(tmp_path: Path) -> None:
    ev = evaluator(
        tmp_path,
        {
            **BASE,
            "pkg/m.py": """
                from dataclasses import dataclass
                from pydantic_settings import BaseSettings
                class Cfg:
                    prefix = "/default"
                    other = "/other"
                    def __init__(self, prefix=None):
                        if prefix:
                            self.prefix = prefix
                @dataclass
                class Data:
                    prefix: str = "/data"
                class Settings(BaseSettings):
                    prefix: str = "/settings"
                class Plain:
                    prefix = "/plain"
                cfg = Cfg("/custom")
                data = Data("/given")
                settings = Settings()
                plain = Plain()
                """,
        },
    )
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "cfg.prefix")).reason == "instance attribute prefix set by Cfg.__init__()"
    assert ev.string(expr(ev, "pkg.m", "cfg.other")) == "/other"
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "data.prefix")).reason == "instance attribute of decorated class Data"
    # pyrefly: ignore[missing-attribute]
    assert "outside the package" in ev.string(expr(ev, "pkg.m", "settings.prefix")).reason
    assert ev.string(expr(ev, "pkg.m", "plain.prefix")) == "/plain"
    assert ev.string(expr(ev, "pkg.m", "Cfg.prefix")) == "/default"


def test_local_names_come_from_the_lookup_never_the_module(tmp_path: Path) -> None:
    """Code in a function body passes a lookup: a local name is what it gives,
    never the module name it hides."""
    ev = evaluator(tmp_path, {**BASE, "pkg/m.py": "PATH = '/module'\nITEMS = ['/m']\n"})
    given = Located("pkg.m", ast.Constant("/local"), None)

    def lookup(name: str):
        if name == "PATH":
            return given, None
        if name == "unknown":
            return Unknown("local variable of make()", name)
        return None

    assert ev.string(expr(ev, "pkg.m", "PATH"), lookup) == "/local"
    assert ev.string(expr(ev, "pkg.m", "PATH + '/x'"), lookup) == "/local/x"
    assert ev.string(expr(ev, "pkg.m", "PATH")) == "/module"
    assert ev.string(expr(ev, "pkg.m", "f'{unknown}/x'"), lookup) == Unknown("local variable of make()", "unknown")
    # pyrefly: ignore[missing-attribute]
    assert ev.string(expr(ev, "pkg.m", "PATH.upper"), lookup).reason == "attribute of local name PATH"
    # pyrefly: ignore[bad-index]
    assert ev.mapping_items(expr(ev, "pkg.m", "{'prefix': PATH}"), lookup)["prefix"][1] is lookup


def test_booleans_and_sequence_builtins(tmp_path: Path) -> None:
    ev = evaluator(tmp_path, {**BASE, "pkg/m.py": "FLAG = False\nGUARDS = ('a', 'b')\nflag_list = [FLAG]\n"})
    assert ev.boolean(expr(ev, "pkg.m", "FLAG")) is False
    assert ev.boolean(expr(ev, "pkg.m", "not FLAG")) is True
    # pyrefly: ignore[missing-attribute]
    assert ev.boolean(expr(ev, "pkg.m", "flag_list[0]")).reason == "not a constant bool"
    assert texts(ev.sequence(expr(ev, "pkg.m", "list(GUARDS)"))) == ["'a'", "'b'"]
    assert texts(ev.sequence(expr(ev, "pkg.m", "tuple(GUARDS) + ('c',)"))) == ["'a'", "'b'", "'c'"]
    # pyrefly: ignore[missing-attribute]
    assert ev.sequence(expr(ev, "pkg.m", "sorted(GUARDS)")).reason == "not a sequence"


DECORATORS = """
    import functools
    from functools import wraps
    import external_lib
    def plain(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            return fn(*a, **k)
        return wrapper
    def bare(fn):
        def wrapper(*a, **k):
            return fn(*a, **k)
        return wrapper
    def register(fn):
        return fn
    def options(fn=None, *, extra=None):
        def decorator(f):
            if extra:
                @wraps(f)
                def a(*args): return f(*args)
                return a
            else:
                @wraps(f)
                def b(*args): return f(*args)
                return b
        if fn is not None:
            return decorator(fn)
        return decorator
    def factory(level):
        def decorator(fn):
            def wrapper(): return fn()
            return wrapper
        return decorator
    class Logged:
        def __init__(self, fn): self.fn = fn
    def updated(fn):
        def wrapper(*a, **k):
            return fn(*a, **k)
        functools.update_wrapper(wrapper, fn)
        return wrapper
    class Updated:
        def __init__(self, fn):
            self.fn = fn
            functools.update_wrapper(self, fn)
    """


@pytest.mark.parametrize(
    "decorator,reason",
    [
        ("plain", None),
        ("register", None),
        ("options", None),
        ("options(extra=1)", None),
        ("external_lib.cached", None),
        ("bare", "bare() returns wrapper without functools.wraps"),
        ("factory(1)", "factory.<locals>.decorator() returns wrapper without functools.wraps"),
        ("Logged", "Logged is a class, whose instance wraps the function"),
        ("updated", None),
        ("Updated", None),
    ],
)
def test_a_decorator_keeps_the_signature_only_through_functools_wraps(
    tmp_path: Path, decorator: str, reason: str | None
) -> None:
    """A package decorator hands FastAPI the function itself, or a wrapper with
    ``functools.wraps`` (whose signature FastAPI follows); anything else is a
    wrapper FastAPI reads instead. Decorators from outside the package are
    assumed to keep the signature."""
    from code_intel.py_values import opaque_decorator

    ev = evaluator(tmp_path, {**BASE, "pkg/d.py": DECORATORS})
    assert opaque_decorator(ev, "pkg.d", ast.parse(decorator, mode="eval").body, None) == reason
