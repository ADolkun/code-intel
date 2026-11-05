"""Where a module-level container may be changed in place (``py_escapes``), and
how the value evaluator and router graph treat it."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from code_intel.backend_model import Located
from code_intel.py_escapes import collect_escapes, mutable_value
from code_intel.py_sources import PySources
from code_intel.py_values import ESCAPED_REASON, ValueEvaluator
from code_intel.router_graph import build_backend_model

DEPS = "from fastapi import Depends\ndef verify(): ...\ndef audit(): ...\n"


def write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


FASTAPI_HEAD = "from fastapi import APIRouter, FastAPI\napp = FastAPI()\nrouter = APIRouter()\n"


def escaped(source: str) -> list[str]:
    return sorted({name for name, _ in collect_escapes(ast.parse(textwrap.dedent(source))).refs})


@pytest.mark.parametrize(
    "use",
    [
        "router = APIRouter(dependencies=COMMON)",
        "app.include_router(router, dependencies=COMMON)",
        "@router.get('/x', dependencies=COMMON)\ndef x(): ...",
        "router.add_api_route('/x', x, dependencies=COMMON, tags=COMMON)",
        "app.include_router(router, **COMMON)",
        "for item in COMMON:\n    print(item)",
        "count = len(COMMON)",
        "found = 1 in COMMON",
        "more = [*COMMON, 1]",
        "both = COMMON + [1]",
        "text = f'{COMMON}'",
        "first = COMMON[0] == 1",
        "if COMMON:\n    pass",
        "names = [d for d in COMMON]",
        "n = COMMON.count(1)",
    ],
)
def test_reads_leave_the_value_alone(use: str) -> None:
    assert "COMMON" not in escaped(f"{FASTAPI_HEAD}COMMON = [1]\n{use}\n")


@pytest.mark.parametrize(
    "head",
    [
        "import fastapi\nrouter = fastapi.APIRouter()\n",
        "import fastapi.routing\nrouter = fastapi.routing.APIRouter()\n",
        "from fastapi.routing import APIRouter as R\nrouter = R()\n",
        "from fastapi.applications import FastAPI\nrouter = FastAPI()\n",
    ],
)
def test_fastapi_is_recognised_by_import(head: str) -> None:
    assert "COMMON" not in escaped(f"{head}COMMON = [1]\nrouter.include_router(router, dependencies=COMMON)\n")


@pytest.mark.parametrize(
    "use",
    [
        "ALIAS = COMMON",  # a second name
        "A, B = COMMON, 1",  # tuple unpacking
        "def common():\n    return COMMON",  # returned
        "REG = {'c': COMMON}",  # stored in a container
        "(alias := COMMON).append(1)",  # walrus
        "for lst in (COMMON,):\n    lst.append(1)",  # loop variable
        "COMMON.append(1)",  # changed directly
        "COMMON[0] = 2",
        "setup(COMMON)",  # handed to a call
        "setup(*COMMON)",
        "setup(**COMMON)",
        "COPY = {**COMMON}",  # a copy shares the values
        "SAME = COMMON or []",
        "VALUES = COMMON.copy()",
        "def f(x=COMMON): ...",
        "router = APIRouter(other=COMMON)",
        # Calls named like FastAPI's or a reading builtin that are not confirmed to be one.
        "def get(path, dependencies): ...\nget('/x', dependencies=COMMON)",
        "helpers = Helpers()\nhelpers.get('/x', dependencies=COMMON)",
        "def setup(router):\n    router.get('/x', dependencies=COMMON)",
        "from pkg.routes import router as other\nother.include_router(other, dependencies=COMMON)",
        "APIRouter = make\nr = APIRouter(dependencies=COMMON)",
        "def sorted(x): ...\nsorted(COMMON)",
        "from pkg.util import len\nlen(COMMON)",
        "from pkg.util import *\nlen(COMMON)",
        "def f(len):\n    return len(COMMON)",
        "for len in ():\n    pass\nlen(COMMON)",
    ],
)
def test_any_other_use_may_change_the_value(use: str) -> None:
    assert "COMMON" in escaped(f"{FASTAPI_HEAD}COMMON = [1]\n{use}\n")


@pytest.mark.parametrize(
    "use",
    [
        "g = G()",  # an instance shares the class's attributes
        "G().deps.append(1)",
        "class H(G): ...",
        "G.add()",
        "from pkg.other import make\nmake(G)",
    ],
)
def test_a_class_escapes_when_called_or_handed_on(use: str) -> None:
    assert "G" in escaped(f"class G:\n    deps = [1]\n    def add(self):\n        self.deps.append(2)\n{use}\n")


def test_reading_a_class_attribute_or_calling_a_function_does_not() -> None:
    assert escaped("class G:\n    deps = [1]\ndef f(): ...\nn = len(G.deps)\nf()\nprint(G.deps[0])\n") == []


def test_a_function_local_name_hides_the_module_one() -> None:
    source = """
        COMMON = [1]
        def f():
            COMMON = []
            COMMON.append(1)
        def g(COMMON):
            COMMON.append(1)
        def h():
            global COMMON
            COMMON.append(1)
        """
    escapes = collect_escapes(ast.parse(textwrap.dedent(source)))
    assert escapes.anytime == {"COMMON"}
    assert escapes.at_import == {}


@pytest.mark.parametrize(
    ("value", "mutable"),
    [("[1]", True), ("{1: 2}", True), ("list(X)", True), ("make()", True), ("A + B", True)]
    + [("'/x'", False), ("f'{A}'", False), ("(1, 2)", False), ("tuple(X)", False), ("OTHER", False), ("m.X", False)],
)
def test_which_values_may_be_containers(value: str, mutable: bool) -> None:
    assert mutable_value(ast.parse(value, mode="eval").body) is mutable


# ── In the evaluator and the graph ───────────────────────────────────────

ALIAS_FORMS = {
    "two-hop": {
        "pkg/guards.py": "from fastapi import Depends\nfrom pkg.deps import verify\nCOMMON = [Depends(verify)]\n",
        "pkg/alias.py": "from pkg.guards import COMMON as C2\nALIAS = C2\n",
        "pkg/mut.py": "from fastapi import Depends\nfrom pkg.deps import audit\nfrom pkg.alias import ALIAS\n"
        "ALIAS.append(Depends(audit))\n",
        "pkg/m.py": "from pkg import mut\nfrom pkg.guards import COMMON\n",
    },
    "tuple-unpacking": {"pkg/m.py": "COMMON = [1]\nA, B = COMMON, 1\nA.append(2)\n"},
    "returned": {"pkg/m.py": "COMMON = [1]\ndef common():\n    return COMMON\ncommon().append(2)\n"},
    "stored": {"pkg/m.py": "COMMON = [1]\nREG = {'c': COMMON}\nREG['c'].append(2)\n"},
    "walrus": {"pkg/m.py": "COMMON = [1]\n(alias := COMMON).append(2)\n"},
    "loop": {"pkg/m.py": "COMMON = [1]\nfor lst in (COMMON,):\n    lst.append(2)\n"},
}


@pytest.mark.parametrize("form", sorted(ALIAS_FORMS))
def test_a_list_changed_through_another_reference_has_no_value(tmp_path: Path, form: str) -> None:
    write(tmp_path, {"pkg/__init__.py": "", "pkg/deps.py": DEPS, **ALIAS_FORMS[form]})
    sources = PySources(tmp_path, "pkg", tmp_path).load()
    ev = ValueEvaluator(sources)
    value = ev.sequence(Located("pkg.m", ast.Name("COMMON", ast.Load()), None))
    assert getattr(value, "reason", None) == ESCAPED_REASON


def test_a_string_is_never_changed_in_place(tmp_path: Path) -> None:
    write(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": "PREFIX = '/a'\nsetup(PREFIX)\n"})
    ev = ValueEvaluator(PySources(tmp_path, "pkg", tmp_path).load())
    assert ev.string(Located("pkg.m", ast.Name("PREFIX", ast.Load()), None)) == "/a"


def test_a_local_list_the_function_changes_has_no_value(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/deps.py": DEPS,
            "pkg/main.py": """
                from fastapi import APIRouter, Depends, FastAPI
                from pkg.deps import audit, verify
                def setup(app, extra=[Depends(verify)]):
                    deps = [Depends(verify)]
                    deps.append(Depends(audit))
                    extra.append(Depends(audit))
                    router = APIRouter()
                    @router.get("/x")
                    def x(): ...
                    app.include_router(router, prefix="/a", dependencies=deps)
                    app.include_router(router, prefix="/b", dependencies=extra)
                app = FastAPI()
                setup(app)
                """,
        },
    )
    model = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path)
    reasons = [p.message for p in model.limitations if p.kind == "unresolved_dependencies"]
    assert len(reasons) == 2 and all(ESCAPED_REASON in r for r in reasons)


def test_the_real_backends_patterns_stay_exact(tmp_path: Path) -> None:
    """Module-level guard lists and response maps used the ways a production
    backend uses them are only read, so they keep their value."""
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/deps.py": DEPS,
            "pkg/common.py": """
                STANDARD_RESPONSES = {401: {"description": "no"}, 403: {"description": "no"}}
                """,
            "pkg/routes.py": """
                from fastapi import APIRouter, Depends
                from pkg.common import STANDARD_RESPONSES
                from pkg.deps import audit, verify
                GUARDS = [Depends(verify)]
                TAGS = ["items"]
                router = APIRouter(prefix="/items", tags=TAGS, dependencies=GUARDS)
                @router.get("/", responses={**STANDARD_RESPONSES, 409: {"description": "conflict"}})
                def listing(): ...
                @router.get("/{item_id}", dependencies=[*GUARDS, Depends(audit)])
                def detail(item_id: int): ...
                GUARD_COUNT = len(GUARDS)
                for guard in GUARDS:
                    assert guard is not None
                """,
            "pkg/main.py": """
                from fastapi import FastAPI
                from pkg.routes import GUARDS, router
                app = FastAPI()
                app.include_router(router, prefix="/api", dependencies=GUARDS)
                """,
        },
    )
    model = build_backend_model(tmp_path, "pkg", "pkg.main:app", tmp_path)
    assert model.limitations == ()
    # pyrefly: ignore[bad-argument-type]
    layers = {r.path: [len(layer.dependencies) for layer in r.layers] for r in model.routes}
    # app, include, router, route decorator
    assert layers == {"/api/items/": [0, 1, 1, 0], "/api/items/{item_id}": [0, 1, 1, 2]}
