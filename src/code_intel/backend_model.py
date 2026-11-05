"""Frozen data shapes for the backend analysis: modules, values, router facts,
the router graph, live routes and dependencies.

No logic lives here. ``py_sources``/``py_values`` produce ``PyModule``,
``Binding`` and ``Target``; ``router_bases`` produces ``ModuleRouterFacts``;
``router_graph`` assembles ``BackendModel``; ``dependency_resolver`` produces
``DepTarget`` and ``DepUse``.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from code_intel.code_paths import FileStamp
from code_intel.problems import Problem

RouterKind = Literal["APIRouter", "FastAPI"]
RouteKind = Literal["http", "websocket"]
# Worst wins when combined along a chain: static < conditional < deferred.
MountState = Literal["static", "conditional", "deferred"]
LayerVia = Literal["app", "router", "route decorator"]
DepVia = Literal["app", "router", "route decorator", "direct"]
DepMarker = Literal["Depends", "Security"]
DepKind = Literal["function", "class", "instance", "method", "lambda", "factory_result", "external", "unresolved"]
BindingKind = Literal[
    "assign",
    "annotated_assign",
    "augmented_assign",
    "def",
    "class",
    "import",
    "from_import",
    "star_import",
    "for",
    "with",
    "walrus",
    "match",
    "except",
    "type_alias",
]


# ── Sources and values ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PyModule:
    name: str  # dotted module name, e.g. "service_backend.api.routes.articles"
    path: Path  # absolute
    rel: str  # repo-relative POSIX
    stamp: FileStamp
    tree: ast.Module
    text: str
    is_package: bool  # an `__init__.py`


@dataclass(frozen=True)
class Binding:
    """One binding of a module-level name by a statement that runs at import."""

    name: str
    index: int  # top-level statement index of the binding statement
    kind: BindingKind
    node: ast.AST  # the binding statement (or the walrus / match capture)
    value: ast.expr | None  # the bound expression, when the statement has one
    conditional: bool  # inside if/try/for/while/with/match at module level
    imported_from: str | None = None  # absolute module for import kinds
    imported_name: str | None = None  # name in that module; None for `import m`


@dataclass(frozen=True)
class Located:
    """An expression together with the module it lives in.

    ``at`` is the top-level statement index of the use (None = module end, for
    code that runs after import, such as function bodies).
    """

    module: str
    node: ast.AST
    at: int | None = None

    @property
    def line(self) -> int | None:
        return getattr(self.node, "lineno", None)


@dataclass(frozen=True)
class Unknown:
    """A value the evaluator could not determine; ``reason`` is e.g. "cycle" or "depth"."""

    reason: str
    text: str  # source text of the expression, for messages


@dataclass(frozen=True)
class Def:
    module: str
    qualname: str
    node: ast.AST  # FunctionDef, AsyncFunctionDef, ClassDef or Lambda


@dataclass(frozen=True)
class Value:
    loc: Located


@dataclass(frozen=True)
class ModuleRef:
    name: str  # dotted module name


@dataclass(frozen=True)
class External:
    dotted: str  # e.g. "fastapi.Depends"


Target = Def | Value | ModuleRef | External | Unknown


class NameResolver(Protocol):
    """What dependency and router analysis need from the value evaluator."""

    def resolve_name(self, module: str, name: str, at: int | None) -> list[Target]: ...

    def resolve_expr(self, loc: Located) -> list[Target]: ...

    def string(self, loc: Located) -> str | Unknown: ...

    def sequence(self, loc: Located) -> tuple[Located, ...] | Unknown: ...


# ── Router facts per module ──────────────────────────────────────────────

# A router verdict keeps the tuple lattice `RouterBases` already joins over:
#   ("router", ast.Call)            built by APIRouter(...) / FastAPI(...)
#   ("router_rebound", ast.Call)    a router whose name a function rebinds via `global`
#   ("other", None)                 provably not a router
#   ("unresolved", None)            may be a router, cannot tell
#   ("unreachable", None)           under `if TYPE_CHECKING:`
#   ("import", module, name)        bound by `from module import name`
#   ("module", module)              bound to a module object (`mod.router` bases)
#   ("param", function_qualname, param_name)   a parameter, for escape analysis
#   ("call", Located)               result of calling a module-level function
RouterRef = (
    tuple[Literal["router", "router_rebound"], ast.Call]
    | tuple[Literal["other", "unresolved", "unreachable"], None]
    | tuple[Literal["import"], str, str]
    | tuple[Literal["module"], str]
    | tuple[Literal["param"], str, str]
    | tuple[Literal["call"], Located]
)


@dataclass(frozen=True)
class ExecContext:
    deferred_in: str | None = None  # qualname of the function whose body holds the event
    conditional: bool = False  # under a branch, loop or try at its level
    lazy: bool = False  # in code that runs later or never (a lambda body, a generator expression, an annotation)


@dataclass(frozen=True)
class RouteReg:
    base: RouterRef
    call: ast.Call
    decorator: bool
    # Literal methods, the `methods=` expression for the graph to evaluate, or
    # the registration call itself when `methods=` may come from `**` arguments.
    methods: tuple[str, ...] | Located
    path: ast.expr
    endpoint: Located | Unknown
    kind: RouteKind
    context: ExecContext
    seq: int


@dataclass(frozen=True)
class IncludeCall:
    parent: RouterRef
    child: RouterRef | Unknown
    call: ast.Call
    context: ExecContext
    seq: int


@dataclass(frozen=True)
class MountCall:
    parent: RouterRef
    path: ast.expr
    app: RouterRef | Unknown
    call: ast.Call
    context: ExecContext
    seq: int


@dataclass(frozen=True)
class RouterEscape:
    """A router passed somewhere the analyzer does not model.

    ``call`` is the ``ast.Call`` for an argument escape, or the assignment
    statement for a ``X.dependencies``/``X.prefix``/``X.routes`` mutation;
    ``callee`` is the called expression, or the mutated attribute.
    """

    router: RouterRef
    call: ast.expr | ast.stmt
    callee: ast.expr
    context: ExecContext
    seq: int


Event = RouteReg | IncludeCall | MountCall | RouterEscape


@dataclass(frozen=True)
class CallSite:
    """A call whose callee is a name or an attribute, so the graph can run the
    body of a package function called at import (``at``: top-level statement
    index, None inside a function)."""

    call: ast.Call
    name: str  # the callee's last name: `f` for `f(...)` and `m.f(...)`
    context: ExecContext
    at: int | None


@dataclass(frozen=True)
class RouterCtor:
    module: str
    node: ast.Call
    kind: RouterKind
    binding: str | None  # module-level name it is bound to, if any
    seq: int


@dataclass(frozen=True)
class ModuleRouterFacts:
    ctors: tuple[RouterCtor, ...]
    events: tuple[Event, ...]  # execution order, increasing seq
    module_end: Mapping[str, RouterRef]  # module-level verdicts after import
    function_returns: Mapping[str, RouterRef]  # function qualname -> joined return verdict
    problems: tuple[Problem, ...]
    calls: tuple[CallSite, ...] = ()


# ── Router graph ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RouterNode:
    id: str  # "{module}:{binding}" or "{module}:L{line}"
    module: str
    file: str  # repo-relative POSIX
    line: int
    kind: RouterKind
    prefix: str | Unknown
    dependencies: tuple[Located, ...] | Unknown
    redirect_slashes: bool  # FastAPI only; True for APIRouter
    events: tuple[Event, ...]


@dataclass(frozen=True)
class IncludeEdge:
    parent: str  # RouterNode.id
    child: str  # RouterNode.id
    prefix: str | Unknown
    dependencies: tuple[Located, ...] | Unknown
    file: str
    line: int
    seq: int
    mount: MountState


@dataclass(frozen=True)
class DepLayer:
    via: LayerVia
    owner: str  # RouterNode.id, or an edge label for include dependencies
    dependencies: tuple[Located, ...] | Unknown


# A path part the analysis could not evaluate shows as `<unresolved: text>`.
# Its real value is unknown, so such a route matches no frontend call.
UNRESOLVED_PATH_PART = "<unresolved: "


def unresolved_path_part(text: str) -> str:
    return f"{UNRESOLVED_PATH_PART}{text}>"


@dataclass(frozen=True)
class LiveRoute:
    method: str  # "GET", ..., "WS" or "ANY"
    path: str
    kind: RouteKind
    handler: str  # short name
    handler_id: str  # "module:qualname"; a lambda is "module:<lambda>@L{line}"
    file: str  # def line's file, repo-relative POSIX
    line: int  # the `def` line, not the decorator line
    registered_file: str
    registered_line: int
    chain: tuple[str, ...]  # RouterNode ids from the app root to the registering node
    mount: MountState
    layers: tuple[DepLayer, ...]  # FastAPI order: app, includes and routers, decorator
    order: int  # global emission index = Starlette dispatch order
    endpoint: Located


IncludeModelKind = Literal["copy", "live", "unknown"]


@dataclass(frozen=True)
class IncludeModel:
    """How ``include_router`` treats routes registered on the child after it
    runs, by the FastAPI version the backend locks (``include_model.py``):
    ``copy`` (up to 0.136: not served through it), ``live`` (0.137 and later:
    served), ``unknown`` (listed as deferred)."""

    kind: IncludeModelKind
    fastapi_version: str | None
    source: str | None  # the lock file the version was read from, repo-relative
    reason: str | None = None  # why the model is unknown


NO_INCLUDE_MODEL = IncludeModel("unknown", None, None, "no FastAPI version was given to the analysis")


@dataclass(frozen=True)
class BackendModel:
    app_id: str
    redirect_slashes: bool
    routes: tuple[LiveRoute, ...]
    unmounted_routes: tuple[LiveRoute, ...]
    routers: tuple[RouterNode, ...]
    edges: tuple[IncludeEdge, ...]
    diagnostics: tuple[Problem, ...]
    limitations: tuple[Problem, ...]
    files_scanned: int
    # `py_sources.PySources`; typed loosely so this module stays free of logic imports.
    sources: Any
    resolver: NameResolver
    include_model: IncludeModel = NO_INCLUDE_MODEL


# ── Dependencies ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DepTarget:
    # "{module}:{qualname}", "{module}:{qualname}()" for an instance (FastAPI reads its
    # __call__), "{module}:{class}().{method}" for a method bound to an instance,
    # "module:<lambda>@L{line}", "unresolved:{module}:{text}", or for an external
    # binding "{module}:{name}" of the module-level name it is bound to
    id: str
    module: str
    qualname: str
    name: str  # last qualname part
    kind: DepKind
    file: str | None
    line: int | None
    # A ``functools.partial`` call site ("module:line:col") when this is the
    # function as that partial binds it: same id, but FastAPI reads fewer
    # parameters or other defaults, so its dependencies differ.
    variant: str = ""
    # The callable's qualname before a closure is folded into its outer function,
    # with "()" for an instance: two forms that share an id after folding (a local
    # class and its instance) keep their own parameters.
    form: str = ""
    # The expression an external binding is assigned (``fastapi_users.current_user(active=True)``).
    bound_to: str = ""


@dataclass(frozen=True)
class DepUse:
    target: DepTarget
    display: str  # short name, or `name(args)` for factory results and instance calls
    via: DepVia
    param: str | None  # handler parameter name, for `direct`
    alias: str | None  # e.g. "_require_list_access" for a guard alias
    marker: DepMarker
    file: str
    line: int
