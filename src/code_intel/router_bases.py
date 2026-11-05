"""Per-module router facts: which expressions are FastAPI routers, and what each
module does with them, in execution order.

``RouterBases`` replays one module statement by statement and gives every name a
verdict as execution reaches it (function and class scopes, ``global`` and
``nonlocal``, walrus, ``TYPE_CHECKING``, branches, ``try``, loops and ``match``).
While replaying it records, with a sequence number per module:

- ``RouteReg``: ``@<base>.get/post/.../api_route/websocket(...)`` decorators,
  ``<base>.add_api_route(...)`` and ``<base>.add_api_websocket_route(...)``;
- ``IncludeCall``: ``<parent>.include_router(<child>, ...)``;
- ``MountCall``: ``<parent>.mount(<path>, <app>)``;
- ``RouterEscape``: a router handed to code the analyzer does not model (an
  argument to an unknown call, a Starlette-level registration, a mutation of
  ``.dependencies``, ``.prefix`` or ``.routes``, or a route method such as
  ``router.get`` used without being called, e.g. bound to a name).

It also records every call whose callee is a name or an attribute
(``CallSite``), so the graph can run the body of a package function called at
import with the arguments of that call.

Each event carries an ``ExecContext``: the function whose body holds it, whether
a branch, loop or handler may skip it, and whether it sits in lazily evaluated
code (a lambda body, a generator expression, an annotation).

Verdicts (``backend_model.RouterRef``) are module-local: an imported name stays
``("import", module, name)`` and a call result ``("call", Located)``, for the
router graph to resolve across modules.
"""

from __future__ import annotations

import ast
import bisect
import sys
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field

from code_intel.backend_model import (
    CallSite,
    Event,
    ExecContext,
    IncludeCall,
    Located,
    ModuleRouterFacts,
    MountCall,
    PyModule,
    RouterCtor,
    RouteReg,
    RouterEscape,
    RouterKind,
    Unknown,
)
from code_intel.problems import Problem, diagnostic
from code_intel.py_sources import StaticNames, bound_name, resolve_relative, source_text, static_names

STAGE = "analyze routers"

HTTP_METHOD_DECORATORS: dict[str, str] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "options": "OPTIONS",
    "head": "HEAD",
    "trace": "TRACE",
}
API_ROUTE = "api_route"
WEBSOCKET = "websocket"
ROUTE_DECORATORS = frozenset({*HTTP_METHOD_DECORATORS, API_ROUTE, WEBSOCKET})
# FastAPI's APIRoute uses ["GET"] when `methods` is omitted.
DEFAULT_METHODS: tuple[str, ...] = ("GET",)
REGISTRATION_CALLS = frozenset({"add_api_route", "add_api_websocket_route"})
INCLUDE_ROUTER = "include_router"
MOUNT = "mount"
STARLETTE_REGISTRATIONS = frozenset({"route", "add_route", "websocket_route", "add_websocket_route", "host"})
ROUTER_CLASSES: dict[str, RouterKind] = {"APIRouter": "APIRouter", "FastAPI": "FastAPI"}
FASTAPI_MODULES = frozenset({"fastapi", "fastapi.routing", "fastapi.applications"})
# Calls that take a router as an argument without changing its routes.
SAFE_ARGUMENT_CALLS = frozenset({"cast", "isinstance", "getattr", "hasattr", "id", "repr", "str", "type"})
# Router attributes whose mutation changes routes, prefixes or guards.
MUTATED_ATTRIBUTES = frozenset({"dependencies", "prefix", "routes"})
# Router attributes holding a list: a name bound to one, or code handed one, can change the router.
ALIASED_ATTRIBUTES = frozenset({"dependencies", "routes"})
# Builtins that only read (or copy) a list handed to them.
LIST_READING_CALLS = frozenset(
    {"len", "list", "tuple", "set", "frozenset", "sorted", "reversed", "iter", "enumerate", "any", "all", "print"}
)
# Router methods that change routes; one used without being called (bound to a
# name, passed on) changes routes the replay cannot see.
METHOD_REFERENCES = frozenset({*ROUTE_DECORATORS, *REGISTRATION_CALLS, INCLUDE_ROUTER, MOUNT, *STARLETTE_REGISTRATIONS})
# What makes a module worth replaying (the prefilter): calls of these methods,
# and decorators (or hand-applied decorator calls) of these.
_TOUCH_CALLS = frozenset({INCLUDE_ROUTER, MOUNT, *REGISTRATION_CALLS, "add_route", "add_websocket_route"})
_TOUCH_DECORATORS = frozenset({*ROUTE_DECORATORS, "route", "websocket_route"})

_TRY_TYPES: tuple[type, ...] = (ast.Try, ast.TryStar)
_TYPE_ALIAS_TYPES: tuple[type, ...] = (ast.TypeAlias,)
_TYPE_PARAM_TYPES: tuple[type, ...] = (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)
_COMPOUND_FIELDS = frozenset({"body", "orelse", "finalbody", "handlers", "cases"})

_Verdict = tuple
_UNRESOLVED: _Verdict = ("unresolved", None)
_OTHER: _Verdict = ("other", None)
# A router (`router`), or one read at import time whose name a function may
# later rebind with `global` (`router_rebound`).
_ROUTER_KINDS = frozenset({"router", "router_rebound"})
# Verdicts that may denote a router: a local one, or one the graph resolves.
ROUTERISH_KINDS = frozenset({"router", "router_rebound", "import", "call", "unresolved"})
# Values that are provably not a router instance.
_NOT_ROUTER_VALUES = (
    ast.Constant,
    ast.JoinedStr,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Lambda,
    ast.Compare,
)


# ── Small AST helpers ────────────────────────────────────────────────────


def is_route_decorator(node: ast.expr) -> bool:
    """``<base>.get(...)``/``.post(...)``/.../``.api_route(...)``/``.websocket(...)``, whatever the base."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ROUTE_DECORATORS


def method_names(values: Iterable[str]) -> tuple[str, ...]:
    """HTTP methods as FastAPI keeps them: upper-cased, each once, sorted."""
    return tuple(sorted({value.upper() for value in values}))


def dotted_label(expr: ast.expr) -> str:
    """``a.b.c`` for a base expression, shortened when long, built without recursion."""
    parts: list[str] = []
    node = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    parts.append(node.id if isinstance(node, ast.Name) else "<expr>")
    parts.reverse()
    if len(parts) > 6:
        parts = [*parts[:3], "…", *parts[-2:]]
    return ".".join(parts)


def _is_super_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"


def call_argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    """Positional argument ``position``, else the ``keyword=`` argument."""
    if len(call.args) > position and not any(isinstance(a, ast.Starred) for a in call.args[: position + 1]):
        return call.args[position]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def qualnames(tree: ast.Module) -> dict[int, str]:
    """``id(def node) -> __qualname__`` for every function and class in a module."""
    out: dict[int, str] = {}
    todo: list[tuple[ast.AST, str]] = [(tree, "")]
    while todo:
        node, prefix = todo.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}{child.name}"
                out[id(child)] = qualname
                inner = f"{qualname}.<locals>." if not isinstance(child, ast.ClassDef) else f"{qualname}."
                todo.append((child, inner))
            else:
                todo.append((child, prefix))
    return out


def router_config(call: ast.Call | None) -> tuple:
    """What decides a router's routes besides its identity: ``prefix=``,
    ``dependencies=`` and ``**options``, compared by their syntax."""
    if call is None:
        return ()
    return tuple((kw.arg, ast.dump(kw.value)) for kw in call.keywords if kw.arg in (None, "prefix", "dependencies"))


def _bound_by(results: list[dict[str, _Verdict | None]]) -> tuple[dict[str, _Verdict | None], dict[str, int]]:
    """For alternative branches: each name's join over the branches that bound it, and how many did."""
    bound: dict[str, _Verdict | None] = {}
    counts: dict[str, int] = {}
    for result in results:
        for name, value in result.items():
            bound[name] = _join_known(bound.get(name), value)
            counts[name] = counts.get(name, 0) + 1
    return bound, counts


def _join_known(a: _Verdict | None, b: _Verdict | None) -> _Verdict | None:
    """``_join``, where None (unbound on that path) adds nothing."""
    if a is None:
        return b
    return a if b is None else _join(a, b)


def _join(a: _Verdict | None, b: _Verdict | None) -> _Verdict:
    """The verdict for a name that may hold either value."""
    if a is None:
        return b if b is not None else _UNRESOLVED
    if b is None or a == b:
        return a
    if a[0] in _ROUTER_KINDS and b[0] in _ROUTER_KINDS:
        if a[1] is b[1]:
            return a if a[0] == "router_rebound" else b
        # Two different router calls: one verdict only when FastAPI would give
        # their routes the same prefix and guards either way.
        if router_config(a[1]) != router_config(b[1]):
            return _UNRESOLVED
        kept, dropped = (a, b) if a[0] == "router_rebound" else (b, a)
        merges = _MERGES.get()
        if merges is not None:
            if id(a[1]) in merges.blocked or id(b[1]) in merges.blocked:
                return _UNRESOLVED
            merges.dropped[id(dropped[1])] = dropped[1]
        return kept
    return _UNRESOLVED


@dataclass
class _Merges:
    """Router merges during one replay. ``_join`` keeps one of two equally
    configured routers, which is sound only while the dropped one has no use
    of its own (a route, include or alias made on its branch): its routes would
    be lost from every include of the merged name. ``blocked`` holds routers
    found used after an earlier replay, which no longer merge."""

    blocked: frozenset[int]
    dropped: dict[int, ast.Call] = field(default_factory=dict)


_MERGES: ContextVar[_Merges | None] = ContextVar("router_merges", default=None)


class _Scope:
    """One Python scope while ``RouterBases`` replays a module: its local names,
    the verdict of each as execution reaches it (``state``) and over the whole
    scope (``joined``, for code that runs later, such as a nested function)."""

    def __init__(
        self,
        kind: str,
        parent: _Scope | None,
        body: list[ast.stmt],
        node: ast.AST,
        qualname: str | None,
    ) -> None:
        self.kind = kind
        self.parent = parent
        self.node = node
        self.node_id = id(node)
        self.qualname = qualname  # None for the module
        self.state: dict[str, _Verdict] = {}
        self.joined: dict[str, _Verdict] = {}
        # One journal per branch being replayed: each name's value before the
        # branch first set it, so the branch can be undone without copying.
        self.journals: list[dict[str, _Verdict | None]] = []
        # One per `try` body being replayed: the join of every value each name
        # takes there, which is what an `except` handler may start from.
        self.traces: list[dict[str, _Verdict]] = []
        # Every binding of each name made while this scope runs, in order; a
        # nested function sees the one live at its `def` and the later ones.
        self.history: dict[str, tuple[list[int], list[_Verdict]]] = {}
        # The join of what nested functions write here through `global` or
        # `nonlocal`: they may run at any time.
        self.written: dict[str, _Verdict] = {}
        # For a function: per enclosing scope executing at its `def` (by id),
        # that `def`'s sequence number and the scope's verdicts for the names
        # the function reads.
        self.marks: dict[int, tuple[int, dict[str, _Verdict | None]]] = {}
        self.locals, self.globals, self.nonlocals, _, _ = scope_names(body)

    def function(self) -> _Scope | None:
        """The innermost function scope around (or at) this one."""
        s: _Scope | None = self
        while s is not None and s.kind != "function":
            s = s.parent
        return s


def scope_names(body: list[ast.stmt]) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    """(bound, global, nonlocal, assigned global, assigned nonlocal) names of a
    scope whose statements are ``body``. Nested function and class bodies,
    lambdas and comprehension targets are their own scopes."""
    bound: set[str] = set()
    declared_global: set[str] = set()
    declared_nonlocal: set[str] = set()
    todo: list[ast.AST] = list(body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            todo.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                todo.extend(node.bases)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            declared_nonlocal.update(node.names)
        elif _TYPE_ALIAS_TYPES and isinstance(node, _TYPE_ALIAS_TYPES):
            name = node.name  # type: ignore[attr-defined]
            if isinstance(name, ast.Name):
                bound.add(name.id)
            continue
        else:
            found = bound_name(node)
            if found:
                bound.add(found)
        if isinstance(node, ast.comprehension):
            todo.append(node.iter)
            todo.extend(node.ifs)
            continue
        todo.extend(ast.iter_child_nodes(node))
    assigned_global = bound & declared_global
    assigned_nonlocal = bound & declared_nonlocal
    bound -= declared_global | declared_nonlocal
    return bound, declared_global, declared_nonlocal, assigned_global, assigned_nonlocal


def _enclosing_function_scope(scope: _Scope) -> _Scope | None:
    """The function whose body runs a class body (none at module level)."""
    f = scope.parent
    while f is not None and f.kind == "class":
        f = f.parent
    return f if f is not None and f.kind == "function" else None


@dataclass(frozen=True)
class _ExprCtx:
    """Where an expression runs inside its statement: maybe skipped (``conditional``),
    maybe never or later (``lazy``), and the names bound by enclosing lambdas and
    comprehensions there (``shadow``), which hide the scope's own bindings."""

    conditional: bool = False
    lazy: bool = False
    shadow: frozenset[str] = frozenset()


def _target_names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _lambda_params(node: ast.Lambda) -> set[str]:
    args = node.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _children(node: ast.AST, ctx: _ExprCtx) -> list[tuple[ast.AST, _ExprCtx]]:
    """The evaluated sub-expressions of ``node``, each with its context."""
    if isinstance(node, ast.Lambda):
        defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
        body_ctx = _ExprCtx(ctx.conditional, True, ctx.shadow | _lambda_params(node))
        return [*((d, ctx) for d in defaults), (node.body, body_ctx)]
    if isinstance(node, ast.IfExp):
        branch = _ExprCtx(True, ctx.lazy, ctx.shadow)
        return [(node.test, ctx), (node.body, branch), (node.orelse, branch)]
    if isinstance(node, ast.BoolOp):
        later = _ExprCtx(True, ctx.lazy, ctx.shadow)
        return [(node.values[0], ctx), *((v, later) for v in node.values[1:])]
    if isinstance(node, ast.Compare):
        later = _ExprCtx(True, ctx.lazy, ctx.shadow)
        first = node.comparators[:1]
        return [(node.left, ctx), *((c, ctx) for c in first), *((c, later) for c in node.comparators[1:])]
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        deferred = isinstance(node, ast.GeneratorExp)
        out: list[tuple[ast.AST, _ExprCtx]] = []
        shadow = set(ctx.shadow)
        for k, gen in enumerate(node.generators):
            iter_ctx = (
                ctx if k == 0 else _ExprCtx(not deferred or ctx.conditional, deferred or ctx.lazy, frozenset(shadow))
            )
            out.append((gen.iter, iter_ctx))
            shadow |= _target_names(gen.target)
            inner = _ExprCtx(not deferred or ctx.conditional, deferred or ctx.lazy, frozenset(shadow))
            out.extend((cond, inner) for cond in gen.ifs)
        inner = _ExprCtx(not deferred or ctx.conditional, deferred or ctx.lazy, frozenset(shadow))
        if isinstance(node, ast.DictComp):
            out += [(node.key, inner), (node.value, inner)]
        else:
            out.append((node.elt, inner))
        return out
    return [(child, ctx) for child in ast.iter_child_nodes(node) if not isinstance(child, (ast.stmt, ast.arg))]


def _calls_in(roots: list[ast.AST], ctx: _ExprCtx) -> Iterator[tuple[ast.Call | ast.Attribute, _ExprCtx]]:
    """Calls in ``roots`` in evaluation order (arguments before the call),
    iteratively; also router method references (``METHOD_REFERENCES``) that
    are not called where they are written."""
    called: set[int] = set()
    stack: list[tuple[ast.AST, _ExprCtx, bool]] = [(root, ctx, False) for root in reversed(roots)]
    while stack:
        node, node_ctx, expanded = stack.pop()
        if expanded:
            if isinstance(node, ast.Call):
                yield node, node_ctx
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in METHOD_REFERENCES
                and isinstance(node.ctx, ast.Load)
                and id(node) not in called
            ):
                yield node, node_ctx
            continue
        if isinstance(node, ast.Call):
            called.add(id(node.func))
        stack.append((node, node_ctx, True))
        for child, child_ctx in reversed(_children(node, node_ctx)):
            stack.append((child, child_ctx, False))


def _lazy_roots(stmt: ast.stmt) -> list[ast.AST]:
    """Annotations, type parameter bounds and defaults, and type alias values:
    evaluated only when something asks for them (PEP 649, PEP 695), so a call
    there may run later or never."""
    roots: list[ast.AST] = []
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = stmt.args
        every = (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
        roots += [a.annotation for a in every if a is not None and a.annotation is not None]
        if stmt.returns is not None:
            roots.append(stmt.returns)
    elif isinstance(stmt, ast.AnnAssign):
        roots.append(stmt.annotation)
    elif _TYPE_ALIAS_TYPES and isinstance(stmt, _TYPE_ALIAS_TYPES):
        roots.append(stmt.value)  # type: ignore[attr-defined]
    for param in getattr(stmt, "type_params", None) or ():
        roots += [v for v in (getattr(param, "bound", None), getattr(param, "default_value", None)) if v is not None]
    return roots


def _statement_roots(stmt: ast.stmt) -> list[ast.AST]:
    """The expressions a statement evaluates itself (not its nested bodies),
    leaving out the lazily evaluated ones (``_lazy_roots``)."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [*stmt.decorator_list, *stmt.args.defaults, *(d for d in stmt.args.kw_defaults if d is not None)]
    if isinstance(stmt, ast.ClassDef):
        return [*stmt.decorator_list, *stmt.bases, *(k.value for k in stmt.keywords)]
    if isinstance(stmt, ast.AnnAssign):
        return [stmt.value] if stmt.value is not None else []
    if _TYPE_ALIAS_TYPES and isinstance(stmt, _TYPE_ALIAS_TYPES):
        return []
    if isinstance(stmt, ast.If):
        return []  # tests are scanned one by one as the chain runs
    roots: list[ast.AST] = []
    for name, value in ast.iter_fields(stmt):
        if name in _COMPOUND_FIELDS or name in ("targets", "target", "annotation", "type_params"):
            continue
        if isinstance(value, ast.AST):
            roots.append(value)
        elif isinstance(value, list):
            roots.extend(v for v in value if isinstance(v, ast.AST))
    return roots


class RouterBases:
    """Replay of one module that gives every router-relevant expression a verdict
    and records the module's router events in execution order.

    Verdicts, as ``backend_model.RouterRef`` tuples:

    - ``router``: built by ``APIRouter(...)``/``FastAPI(...)`` (also through an
      alias import, ``fastapi.APIRouter``, a subclass, ``cast``, a conditional
      of routers, a walrus or tuple unpacking; ``X.router`` of a FastAPI app is
      the app itself);
    - ``router_rebound``: a router read at import time whose name a function may
      later rebind through ``global`` or ``nonlocal``;
    - ``other``: provably not a router (a literal, a lambda, a ``def``, a
      ``class``, a name bound nowhere);
    - ``import``: ``(module, dotted name)`` bound by an import, or an attribute
      of an imported module;
    - ``module``: a name bound to a module object;
    - ``param``: a function parameter;
    - ``call``: the result of a call the graph resolves through the callee's returns;
    - ``unresolved``: anything else, including two different routers.

    Code that runs later (a nested function's body) sees the binding live at its
    ``def`` and every later binding. A block under ``if TYPE_CHECKING:`` never
    runs, so it records nothing.
    """

    _MAX_DEPTH = 50

    def __init__(self, module: PyModule) -> None:
        self.module = module
        blocked: frozenset[int] = frozenset()
        while True:
            merges = _Merges(blocked)
            token = _MERGES.set(merges)
            try:
                self._replay(module.tree)
            finally:
                _MERGES.reset(token)
            # A dropped router used on its own branch makes the merge lose its
            # routes; replay with it unmergeable, so its uses become unresolved
            # and are reported. Rare, and bounded by the number of routers.
            used = blocked | (merges.dropped.keys() & self._used_routers())
            if used == blocked:
                return
            blocked = frozenset(used)

    def _used_routers(self) -> set[int]:
        """Routers (by ``id`` of their call) that an event, a module-level name
        or a function return refers to directly."""
        refs: list[object] = [*self.module_end.values(), *self.returns.values()]
        for event in self.events:
            if isinstance(event, RouteReg):
                refs.append(event.base)
            elif isinstance(event, IncludeCall):
                refs += (event.parent, event.child)
            elif isinstance(event, MountCall):
                refs += (event.parent, event.app)
            else:
                refs.append(event.router)
        return {id(ref[1]) for ref in refs if isinstance(ref, tuple) and ref and ref[0] in _ROUTER_KINDS}

    def _replay(self, tree: ast.Module) -> None:
        self._ctor_kinds: dict[str, RouterKind] = dict(ROUTER_CLASSES)
        self._star_import = False
        self._seq = 0
        self._event_seq = 0
        self._suffixes: dict[tuple[int, str], list[_Verdict]] = {}
        self._cond = 0  # nesting of blocks that may be skipped
        self._top_index: int | None = None
        self.events: list[Event] = []
        self.ctors: list[RouterCtor] = []
        self.returns: dict[str, _Verdict] = {}
        self.problems: list[Problem] = []
        # seq of an argument escape -> the argument expression that carried the router.
        self.escape_args: dict[int, ast.expr] = {}
        self.calls: list[CallSite] = []
        self.function_scopes: dict[str, FunctionScope] = {}
        self._read_imports(tree)
        self._module = _Scope("module", None, tree.body, tree, None)
        self._scan_writes(tree)
        self._module.locals |= self._rebound | self._class_globals
        self._static: StaticNames = static_names(tree)
        self._static_nested: StaticNames = static_names(tree, nested=True)
        self._pending: list[tuple[_Scope, list[ast.stmt]]] = []
        for index, stmt in enumerate(tree.body):
            self._top_index = index
            self._run(stmt, self._module)
        self._top_index = None
        self.module_end: dict[str, _Verdict] = dict(self._module.state)
        self._run_pending()

    def _read_imports(self, tree: ast.Module) -> None:
        """Note star imports and the names FastAPI's router classes are imported as."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            from_fastapi = node.level == 0 and node.module in FASTAPI_MODULES
            for alias in node.names:
                if alias.name == "*":
                    self._star_import = True
                elif from_fastapi and alias.name in ROUTER_CLASSES:
                    self._ctor_kinds[alias.asname or alias.name] = ROUTER_CLASSES[alias.name]

    def _run_pending(self) -> None:
        """Run the function bodies queued while running the module, and those they queue."""
        done = 0
        while done < len(self._pending):
            scope, body = self._pending[done]
            done += 1
            self._run_block(body, scope)
            if body and not isinstance(body[-1], (ast.Return, ast.Raise)):
                # Falling off the end returns None.
                assert scope.qualname is not None
                self.returns[scope.qualname] = _join_known(self.returns.get(scope.qualname), _OTHER) or _OTHER

    def _scan_writes(self, tree: ast.Module) -> None:
        """Find the names written from a scope other than their own:

        - ``_rebound``: module names a function (or a class inside one) assigns
          under ``global``, so they may change whenever it runs;
        - ``_class_globals``: module names a module-level class body assigns
          under ``global``, which happens in order at import;
        - ``_nonlocal_written``: (function node id, name) a nested function
          assigns under ``nonlocal``.
        """
        self._rebound: set[str] = set()
        self._class_globals: set[str] = set()
        self._nonlocal_written: set[tuple[int, str]] = set()
        todo: list[tuple[ast.AST, tuple[tuple[int, set[str]], ...]]] = [(tree, ())]
        while todo:
            node, chain = todo.pop()
            inner = chain
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = self._scope_writes(node, chain)
            for child in ast.iter_child_nodes(node):
                todo.append((child, inner))

    def _scope_writes(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        chain: tuple[tuple[int, set[str]], ...],
    ) -> tuple[tuple[int, set[str]], ...]:
        """Record what one function or class body writes through ``global`` or
        ``nonlocal``; the chain of function scopes inside it."""
        bound, _, _, assigned_global, assigned_nonlocal = scope_names(node.body)
        is_class = isinstance(node, ast.ClassDef)
        if is_class and not chain:
            self._class_globals |= assigned_global
        else:
            self._rebound |= assigned_global
        for name in assigned_nonlocal:
            for k in range(len(chain) - 1, -1, -1):
                if name in chain[k][1]:
                    # A class body runs inline in its function.
                    if not (is_class and k == len(chain) - 1):
                        self._nonlocal_written.add((chain[k][0], name))
                    break
        if is_class:
            return chain
        args = node.args  # type: ignore[union-attr]
        bound |= {
            a.arg
            for a in (
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                *([args.vararg] if args.vararg else []),
                *([args.kwarg] if args.kwarg else []),
            )
        }
        return (*chain, (id(node), bound))

    # ── Lookup and evaluation ────────────────────────────────────────────

    def _lookup(self, name: str, scope: _Scope) -> _Verdict:
        now = True  # Whether `s` is executing when the lookup happens.
        crossed: _Scope | None = None  # The last function left on the way.
        s: _Scope | None = scope
        while s is not None:
            if s.kind == "function" and name in s.globals:
                crossed, s, now = s, self._module, False
                continue
            if s.kind == "class" and name in s.globals:
                # Inline at import only when no function encloses the class.
                f = _enclosing_function_scope(s)
                if f is not None:
                    crossed, now = f, False
                s = self._module
                continue
            if name in s.locals:
                return self._local_verdict(s, name, now, crossed)
            if s.kind == "function":
                now = False  # Enclosing scopes run before the function does.
                crossed = s
            s = s.parent
            while s is not None and s.kind == "class":
                s = s.parent  # A class body is not visible to nested scopes.
        return _UNRESOLVED if self._star_import else _OTHER

    def _local_verdict(self, s: _Scope, name: str, now: bool, crossed: _Scope | None) -> _Verdict:
        """``name`` bound in scope ``s``: its value now when ``s`` is running,
        else what it can hold when the function ``crossed`` runs."""
        if now:
            verdict = s.state.get(name)
        else:
            mark = crossed.marks.get(id(s)) if crossed else None
            verdict = self._reachable(s, name, *mark) if mark is not None else s.joined.get(name)
        if verdict is None:
            verdict = _UNRESOLVED  # Not bound yet, or only later.
        if (s is self._module and name in self._rebound) or (s.node_id, name) in self._nonlocal_written:
            if now and verdict[0] == "router":
                verdict = ("router_rebound", verdict[1])
            else:
                verdict = _join(verdict, _UNRESOLVED)
        return verdict

    def _reachable(self, scope: _Scope, name: str, seq: int, at_def: dict[str, _Verdict | None]) -> _Verdict | None:
        """The verdicts ``name`` in ``scope`` can have when a function defined at
        ``seq`` runs: the one live at its ``def``, joined with every later
        binding and with what nested functions write there."""
        seqs, verdicts = scope.history.get(name, ([], []))
        start = bisect.bisect_right(seqs, seq)
        key = (id(scope), name)
        suffix = self._suffixes.get(key)
        if suffix is None:
            suffix = [_UNRESOLVED] * len(verdicts)
            acc: _Verdict | None = None
            for i in range(len(verdicts) - 1, -1, -1):
                acc = verdicts[i] if acc is None else _join(verdicts[i], acc)
                suffix[i] = acc
            self._suffixes[key] = suffix
        result = at_def.get(name)
        for more in (suffix[start] if start < len(verdicts) else None, scope.written.get(name)):
            if more is not None:
                result = more if result is None else _join(result, more)
        return result

    def _ctor_kind(self, func: ast.expr) -> RouterKind | None:
        if isinstance(func, ast.Name):
            return self._ctor_kinds.get(func.id)
        if isinstance(func, ast.Attribute) and func.attr in ROUTER_CLASSES:
            return ROUTER_CLASSES[func.attr]
        return None

    def _located(self, node: ast.AST, scope: _Scope) -> Located:
        return Located(self.module.name, node, self._top_index if scope.function() is None else None)

    def _eval(self, value: ast.expr, scope: _Scope, shadow: frozenset[str] = frozenset(), depth: int = 0) -> _Verdict:
        if depth > self._MAX_DEPTH:
            return _UNRESOLVED
        if isinstance(value, ast.Call):
            func = value.func
            is_cast = (isinstance(func, ast.Name) and func.id == "cast") or (
                isinstance(func, ast.Attribute) and func.attr == "cast"
            )
            if is_cast and len(value.args) == 2:
                return self._eval(value.args[1], scope, shadow, depth + 1)
            if self._ctor_kind(func) is not None:
                return ("router", value)
            return ("call", self._located(value, scope))
        if isinstance(value, ast.Name):
            if value.id in shadow:
                return _UNRESOLVED
            return self._lookup(value.id, scope)
        if isinstance(value, ast.Attribute):
            base = self._eval(value.value, scope, shadow, depth + 1)
            if base[0] in _ROUTER_KINDS and value.attr == "router":
                return base  # `app.router` is the app's own router
            if base[0] == "module":
                return ("import", base[1], value.attr)
            if base[0] == "import":
                return ("import", base[1], f"{base[2]}.{value.attr}")
            return _UNRESOLVED
        if isinstance(value, ast.IfExp):
            return _join(
                self._eval(value.body, scope, shadow, depth + 1), self._eval(value.orelse, scope, shadow, depth + 1)
            )
        if isinstance(value, ast.NamedExpr):
            return self._eval(value.value, scope, shadow, depth + 1)
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.Not):
            return _OTHER
        if isinstance(value, _NOT_ROUTER_VALUES):
            return _OTHER
        return _UNRESOLVED

    # ── Binding ──────────────────────────────────────────────────────────

    def _bind(self, name: str, verdict: _Verdict, scope: _Scope) -> None:
        if scope.kind != "module" and (name in scope.globals or name in scope.nonlocals):
            target = self._module if name in scope.globals else scope.parent
            while target is not None and target.kind == "class":
                target = target.parent
            if target is None:
                return
            # A class body runs inline in the scope around it, so its write
            # lands in order; a function's whenever it is called.
            s = scope
            while s is not target and s is not None and s.kind == "class":
                s = s.parent
            if s is target:
                self._set(target, name, verdict)
                target.joined[name] = _join(target.joined.get(name), verdict)
                self._record(target, name, verdict)
            else:
                target.joined[name] = _join(target.joined.get(name), verdict)
                target.written[name] = _join(target.written.get(name), verdict)
            return
        self._set(scope, name, verdict)
        scope.joined[name] = _join(scope.joined.get(name), verdict)
        self._record(scope, name, verdict)

    def _record(self, scope: _Scope, name: str, verdict: _Verdict) -> None:
        self._seq += 1
        seqs, verdicts = scope.history.setdefault(name, ([], []))
        seqs.append(self._seq)
        verdicts.append(verdict)

    def _mark_def(self, inner: _Scope, node: ast.AST, scope: _Scope) -> None:
        """Record, for the function ``inner`` defined by ``node``, the verdicts of
        the names it reads in every enclosing scope executing at its ``def``."""
        reads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        s: _Scope | None = scope
        while s is not None:
            inner.marks[id(s)] = (self._seq, {name: s.state.get(name) for name in reads & s.locals})
            if s.kind != "class":
                break  # A function's or the module's enclosing scopes ran.
            s = s.parent

    @staticmethod
    def _set(scope: _Scope, name: str, verdict: _Verdict | None) -> None:
        if scope.journals and name not in scope.journals[-1]:
            scope.journals[-1][name] = scope.state.get(name)
        if scope.traces and verdict is not None:
            trace = scope.traces[-1]
            trace[name] = _join(trace[name], verdict) if name in trace else verdict
        if verdict is None:
            scope.state.pop(name, None)
        else:
            scope.state[name] = verdict

    def _bind_target(self, target: ast.expr, value: ast.expr | None, scope: _Scope) -> None:
        if isinstance(target, ast.Name):
            verdict = _UNRESOLVED if value is None else self._eval(value, scope)
            self._bind(target.id, verdict, scope)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pairwise = (
                isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == len(target.elts)
                and not any(isinstance(e, ast.Starred) for e in target.elts)
            )
            for k, elt in enumerate(target.elts):
                inner = value.elts[k] if pairwise and isinstance(value, (ast.Tuple, ast.List)) else None
                self._bind_target(elt.value if isinstance(elt, ast.Starred) else elt, inner, scope)

    def _bind_walrus(self, roots: list[ast.AST], scope: _Scope) -> list[str]:
        """Bind the walrus targets in the expressions ``roots``; their names."""
        names: list[str] = []
        todo = list(roots)
        while todo:
            node = todo.pop()
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
                self._bind(node.target.id, self._eval(node.value, scope), scope)
                names.append(node.target.id)
            todo.extend(ast.iter_child_nodes(node))
        return names

    # ── Events ───────────────────────────────────────────────────────────

    def _context(self, scope: _Scope, expr_ctx: _ExprCtx | None = None) -> ExecContext:
        function = scope.function()
        return ExecContext(
            deferred_in=function.qualname if function is not None else None,
            conditional=self._cond > 0 or bool(expr_ctx and expr_ctx.conditional),
            lazy=bool(expr_ctx and expr_ctx.lazy),
        )

    def _next_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    def _methods(self, call: ast.Call, attr: str, scope: _Scope) -> tuple[str, ...] | Located:
        """A literal ``methods=`` list, else the expression for the graph to
        evaluate where the registration runs (a name may be rebound later), or
        the registration call itself when ``methods=`` may come from ``**`` arguments."""
        if attr in HTTP_METHOD_DECORATORS:
            return (HTTP_METHOD_DECORATORS[attr],)
        if attr == WEBSOCKET or attr == "add_api_websocket_route":
            return ("WS",)
        for kw in call.keywords:
            if kw.arg != "methods":
                continue
            value = kw.value
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                return method_names(str(e.value) for e in value.elts)  # type: ignore[attr-defined]
            return self._located(value, scope)
        if any(kw.arg is None for kw in call.keywords):
            return self._located(call, scope)
        return DEFAULT_METHODS

    def _route_reg(
        self,
        base: _Verdict,
        call: ast.Call,
        attr: str,
        path: ast.expr | None,
        endpoint: Located | Unknown,
        decorator: bool,
        scope: _Scope,
        ctx: ExecContext,
    ) -> None:
        if path is None or base[0] in ("other", "unreachable", "module"):
            return
        kind = "websocket" if attr in (WEBSOCKET, "add_api_websocket_route") else "http"
        self.events.append(
            RouteReg(
                base=base,
                call=call,
                decorator=decorator,
                methods=self._methods(call, attr, scope),
                path=path,
                endpoint=endpoint,
                kind=kind,
                context=ctx,
                seq=self._next_seq(),
            )
        )

    def _on_method_reference(self, ref: ast.Attribute, scope: _Scope, expr_ctx: _ExprCtx) -> None:
        """``router.get`` (or another router method) not called where written."""
        base = self._eval(ref.value, scope, expr_ctx.shadow)
        if base[0] in _ROUTER_KINDS or base[0] == "import":
            self.events.append(RouterEscape(base, ref, ref, self._context(scope, expr_ctx), self._next_seq()))

    def _on_call(self, call: ast.Call, scope: _Scope, expr_ctx: _ExprCtx) -> None:
        func = call.func
        ctx = self._context(scope, expr_ctx)
        shadow = expr_ctx.shadow
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name is not None and not (isinstance(func, ast.Name) and func.id in shadow):
            self.calls.append(CallSite(call, name, ctx, self._located(call, scope).at))
        kind = self._ctor_kind(func)
        if kind is not None:
            self.ctors.append(RouterCtor(self.module.name, call, kind, None, self._next_seq()))
            return
        skip: set[int] = set()
        if isinstance(func, ast.Attribute):
            self._on_method_call(call, func, scope, shadow, ctx, skip)
        elif isinstance(func, ast.Call) and is_route_decorator(func):
            # `router.get("/x")(handler)`: the decorator applied by hand.
            assert isinstance(func.func, ast.Attribute)
            base = self._eval(func.func.value, scope, shadow)
            handler = call.args[0] if call.args else None
            endpoint = (
                Located(self.module.name, handler, self._located(call, scope).at)
                if handler is not None
                else Unknown("missing endpoint", source_text(self.module, call, 60))
            )
            self._route_reg(base, func, func.func.attr, call_argument(func, 0, "path"), endpoint, False, scope, ctx)
        if isinstance(func, ast.Name) and func.id in SAFE_ARGUMENT_CALLS:
            return
        if isinstance(func, ast.Attribute) and func.attr == "cast":
            return
        self._argument_escapes(call, scope, shadow, ctx, skip)

    def _on_method_call(
        self,
        call: ast.Call,
        func: ast.Attribute,
        scope: _Scope,
        shadow: frozenset[str],
        ctx: ExecContext,
        skip: set[int],
    ) -> None:
        """``<base>.<method>(...)``: an include, registration, mount, Starlette-level
        registration, or a change to a router's routes or dependencies list.
        Arguments the event already reads are added to ``skip``."""
        attr = func.attr
        # `super().add_api_route(...)` inside a router subclass is FastAPI's own
        # method at work; the subclass is reported as a custom router class.
        base = _OTHER if _is_super_call(func.value) else self._eval(func.value, scope, shadow)
        # A parameter may be a router: the graph resolves it through the call running the function.
        routerish = base[0] in ROUTERISH_KINDS or base[0] == "param"
        if attr == INCLUDE_ROUTER and base[0] not in ("other", "module"):
            self._on_include(call, base, scope, shadow, ctx, skip)
        elif attr in REGISTRATION_CALLS and routerish:
            endpoint_expr = call_argument(call, 1, "endpoint")
            endpoint = (
                Located(self.module.name, endpoint_expr, self._located(call, scope).at)
                if endpoint_expr is not None
                else Unknown("missing endpoint", source_text(self.module, call, 60))
            )
            self._route_reg(base, call, attr, call_argument(call, 0, "path"), endpoint, False, scope, ctx)
        elif attr == MOUNT and routerish:
            self._on_mount(call, base, scope, shadow, ctx, skip)
        elif attr in STARLETTE_REGISTRATIONS and routerish:
            self.events.append(RouterEscape(base, call, func, ctx, self._next_seq()))
        elif isinstance(func.value, ast.Attribute) and func.value.attr in ("routes", "dependencies"):
            owner = self._eval(func.value.value, scope, shadow)
            if owner[0] in ROUTERISH_KINDS and owner[0] != "unresolved":
                self.events.append(RouterEscape(owner, call, func, ctx, self._next_seq()))

    def _on_include(
        self, call: ast.Call, base: _Verdict, scope: _Scope, shadow: frozenset[str], ctx: ExecContext, skip: set[int]
    ) -> None:
        child_expr = call_argument(call, 0, "router")
        child: _Verdict | Unknown = (
            self._eval(child_expr, scope, shadow)
            if child_expr is not None
            else Unknown("missing router argument", source_text(self.module, call, 60))
        )
        if child_expr is not None:
            skip.add(id(child_expr))
        self.events.append(IncludeCall(base, child, call, ctx, self._next_seq()))

    def _on_mount(
        self, call: ast.Call, base: _Verdict, scope: _Scope, shadow: frozenset[str], ctx: ExecContext, skip: set[int]
    ) -> None:
        app_expr = call_argument(call, 1, "app")
        app: _Verdict | Unknown = (
            self._eval(app_expr, scope, shadow)
            if app_expr is not None
            else Unknown("missing app argument", source_text(self.module, call, 60))
        )
        if app_expr is not None:
            skip.add(id(app_expr))
        path = call_argument(call, 0, "path")
        if path is not None:
            self.events.append(MountCall(base, path, app, call, ctx, self._next_seq()))

    def _argument_escapes(
        self, call: ast.Call, scope: _Scope, shadow: frozenset[str], ctx: ExecContext, skip: set[int]
    ) -> None:
        """A router, or its routes or dependencies list, passed to a call."""
        func = call.func
        reads_only = isinstance(func, ast.Name) and func.id in LIST_READING_CALLS and func.id not in shadow
        for arg in (*call.args, *(kw.value for kw in call.keywords)):
            value = arg.value if isinstance(arg, ast.Starred) else arg
            if id(value) in skip or not isinstance(value, (ast.Name, ast.Attribute)):
                continue
            state_owner = self._router_state(value, scope, shadow)
            if state_owner is not None and not reads_only:
                self.events.append(RouterEscape(state_owner, call, value, ctx, self._next_seq()))
                continue
            verdict = self._eval(value, scope, shadow)
            # A parameter may hold a router the caller passed: the graph follows it
            # through each call running the function.
            if verdict[0] in _ROUTER_KINDS or verdict[0] in ("import", "param"):
                seq = self._next_seq()
                self.escape_args[seq] = arg
                self.events.append(RouterEscape(verdict, call, func, ctx, seq))

    def _scan(self, roots: list[ast.AST], scope: _Scope, base_ctx: _ExprCtx | None = None) -> None:
        for node, expr_ctx in _calls_in(roots, base_ctx or _ExprCtx()):
            if isinstance(node, ast.Call):
                self._on_call(node, scope, expr_ctx)
            else:
                self._on_method_reference(node, scope, expr_ctx)

    def _mutations(self, targets: list[ast.expr], stmt: ast.stmt, scope: _Scope) -> None:
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Attribute) and node.attr in MUTATED_ATTRIBUTES:
                    owner = self._eval(node.value, scope)
                    if owner[0] in _ROUTER_KINDS or owner[0] == "import":
                        self.events.append(RouterEscape(owner, stmt, node, self._context(scope), self._next_seq()))

    def _router_state(self, value: ast.expr, scope: _Scope, shadow: frozenset[str] = frozenset()) -> _Verdict | None:
        """The router whose ``.dependencies`` or ``.routes`` list ``value`` is."""
        if isinstance(value, ast.Attribute) and value.attr in ALIASED_ATTRIBUTES:
            owner = self._eval(value.value, scope, shadow)
            if (owner[0] in ROUTERISH_KINDS and owner[0] != "unresolved") or owner[0] == "import":
                return owner
        return None

    def _state_alias(self, value: ast.expr, stmt: ast.stmt, scope: _Scope) -> None:
        """``deps = router.dependencies``: a change through the name changes the router."""
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else [value]
        for item in values:
            owner = self._router_state(item, scope)
            if owner is not None:
                self.events.append(RouterEscape(owner, stmt, item, self._context(scope), self._next_seq()))

    def _decorators(self, stmt: ast.FunctionDef | ast.AsyncFunctionDef, scope: _Scope) -> None:
        """Route decorators register when applied, bottom-up."""
        endpoint = self._located(stmt, scope)
        for dec in reversed(stmt.decorator_list):
            if not is_route_decorator(dec):
                continue
            assert isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
            base = self._eval(dec.func.value, scope)
            self._route_reg(
                base,
                dec,
                dec.func.attr,
                call_argument(dec, 0, "path"),
                endpoint,
                True,
                scope,
                self._context(scope),
            )

    # ── Replay ───────────────────────────────────────────────────────────

    def _run_block(self, body: list[ast.stmt], scope: _Scope) -> None:
        for stmt in body:
            self._run(stmt, scope)

    def _run_conditional(self, body: list[ast.stmt], scope: _Scope) -> None:
        self._cond += 1
        try:
            self._run_block(body, scope)
        finally:
            self._cond -= 1

    def _branch(self, scope: _Scope, run: Callable[[], None]) -> dict[str, _Verdict | None]:
        """Run one alternative, then undo it from its journal; what it bound."""
        journal: dict[str, _Verdict | None] = {}
        scope.journals.append(journal)
        run()
        scope.journals.pop()
        result = {name: scope.state.get(name) for name in journal}
        for name, old in journal.items():
            if old is None:
                scope.state.pop(name, None)
            else:
                scope.state[name] = old
        return result

    def _branches(self, scope: _Scope, blocks: list[Callable[[], None]], *, may_skip: bool) -> None:
        """Run alternative blocks from the same state and merge what they bind."""
        results = [self._branch(scope, run) for run in blocks]
        bound, counts = _bound_by(results)
        for name, value in bound.items():
            if may_skip or counts[name] < len(results):
                value = _join_known(value, scope.state.get(name))
            self._set(scope, name, value)

    def _run_if(self, stmt: ast.If, scope: _Scope) -> None:
        """An ``if``/``elif``/``else`` chain, walked in a loop so a long chain
        doesn't recurse. Each test runs only once the tests before it are false,
        so its walrus binds for its own block and the blocks after it. A block
        whose test is never true on import (``TYPE_CHECKING``,
        ``__name__ == "__main__"``) is no branch, and after an always-true test
        (``not TYPE_CHECKING``) nothing else runs.
        """
        fall: dict[str, _Verdict | None] = {}
        log: dict[str, tuple[list[int], list[_Verdict | None]]] = {}
        scope.journals.append(fall)
        results = self._if_branches(stmt, scope, fall, log)
        scope.journals.pop()
        self._restore(scope, fall, self._if_merged(scope, results, fall, log))

    def _if_branches(
        self,
        stmt: ast.If,
        scope: _Scope,
        fall: dict[str, _Verdict | None],
        log: dict[str, tuple[list[int], list[_Verdict | None]]],
    ) -> list[tuple[int, dict[str, _Verdict | None]]]:
        """Run each block of the chain; ``(tests seen before it, what it bound)``
        per block. Walrus bindings in the tests go to ``log``."""
        results: list[tuple[int, dict[str, _Verdict | None]]] = []
        node, test_no = stmt, 0
        while True:
            parts = self._evaluated_parts(node.test, scope)
            # Tests after the first run only when the earlier ones were false.
            self._scan(parts, scope, _ExprCtx(conditional=test_no > 0))
            for name in self._bind_walrus(parts, scope):
                if name in fall:
                    tests, values = log.setdefault(name, ([], []))
                    tests.append(test_no)
                    values.append(scope.state.get(name))
            truth = self._truth(node.test, scope)
            if truth is not False:
                body = node.body
                results.append((test_no, self._branch(scope, lambda b=body: self._run_conditional(b, scope))))
            if truth is True:
                return results
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                node, test_no = node.orelse[0], test_no + 1
                continue
            orelse = node.orelse
            results.append((test_no, self._branch(scope, lambda b=orelse: self._run_conditional(b, scope))))
            return results

    @staticmethod
    def _if_merged(
        scope: _Scope,
        results: list[tuple[int, dict[str, _Verdict | None]]],
        fall: dict[str, _Verdict | None],
        log: dict[str, tuple[list[int], list[_Verdict | None]]],
    ) -> dict[str, _Verdict | None]:
        """What each name holds after the chain. A branch that leaves a name
        alone keeps the value it started from: the one after the last walrus
        binding before its block."""
        seens = [seen for seen, _ in results]
        bound, _ = _bound_by([result for _, result in results])
        bound_seens: dict[str, list[int]] = {}
        for seen, result in results:
            for name in result:
                bound_seens.setdefault(name, []).append(seen)
        merged: dict[str, _Verdict | None] = {}
        for name in bound.keys() | log.keys():
            value = bound.get(name)
            mine = bound_seens.get(name, [])
            tests, values = log.get(name, ([], []))
            original = fall[name] if name in fall else scope.state.get(name)
            stretches = [
                (0, tests[0] if tests else sys.maxsize, original),
                *((tests[k], tests[k + 1] if k + 1 < len(tests) else sys.maxsize, v) for k, v in enumerate(values)),
            ]
            for lo, hi, start in stretches:
                untouched = (bisect.bisect_left(seens, hi) - bisect.bisect_left(seens, lo)) - (
                    bisect.bisect_left(mine, hi) - bisect.bisect_left(mine, lo)
                )
                if untouched > 0:
                    value = _join_known(value, start)
            merged[name] = value
        return merged

    def _restore(self, scope: _Scope, journal: dict[str, _Verdict | None], merged: dict[str, _Verdict | None]) -> None:
        """Undo the bindings ``journal`` recorded, then bind the merged values."""
        for name, old in journal.items():
            if old is None:
                scope.state.pop(name, None)
            else:
                scope.state[name] = old
        for name, value in merged.items():
            self._set(scope, name, value)

    def _run_try(self, stmt: ast.stmt, scope: _Scope) -> None:
        """The body then ``else``, or a handler. A handler may run after any
        statement of the body, so it starts from each name's value before the
        body joined with every value the body gave it."""
        trace: dict[str, _Verdict] = {}

        def body() -> None:
            scope.traces.append(trace)
            self._run_block(stmt.body, scope)  # type: ignore[attr-defined]
            scope.traces.pop()
            if scope.traces:  # Also values the enclosing `try` body takes.
                outer = scope.traces[-1]
                for name, value in trace.items():
                    outer[name] = _join(outer[name], value) if name in outer else value
            self._run_conditional(stmt.orelse, scope)  # type: ignore[attr-defined]

        done = self._branch(scope, body)
        start: dict[str, _Verdict | None] = {}
        handlers = stmt.handlers  # type: ignore[attr-defined]
        handled = self._run_handlers(handlers, scope, trace, start) if handlers else []
        bound, counts = _bound_by(handled)
        merged: dict[str, _Verdict | None] = {}
        for name in done.keys() | bound.keys() | start.keys():
            original = start[name] if name in start else scope.state.get(name)
            value = done[name] if name in done else original
            if handled:
                value = _join_known(value, bound.get(name))
                if counts.get(name, 0) < len(handled):
                    value = _join_known(value, scope.state.get(name))
            merged[name] = value
        self._restore(scope, start, merged)
        self._run_block(stmt.finalbody, scope)  # type: ignore[attr-defined]

    def _run_handlers(
        self,
        handlers: list[ast.ExceptHandler],
        scope: _Scope,
        trace: dict[str, _Verdict],
        start: dict[str, _Verdict | None],
    ) -> list[dict[str, _Verdict | None]]:
        """Run each handler from the values the ``try`` body may leave
        (journaled in ``start``); what each bound."""

        def handler(h: ast.ExceptHandler) -> Callable[[], None]:
            def run() -> None:
                if h.type is not None:
                    self._scan([h.type], scope, _ExprCtx(conditional=True))
                if h.name:
                    self._bind(h.name, _UNRESOLVED, scope)
                self._run_conditional(h.body, scope)

            return run

        scope.journals.append(start)
        for name, value in trace.items():
            self._set(scope, name, _join_known(scope.state.get(name), value))
        handled = [self._branch(scope, handler(h)) for h in handlers]
        scope.journals.pop()
        return handled

    def _truth(self, test: ast.expr, scope: _Scope) -> bool | None:
        """False when ``test`` is never true when the module is imported
        (``TYPE_CHECKING``, ``__name__ == "__main__"``), True when always, else
        None. In a function or class body, a name it binds locally is not the module's."""
        return (self._static if scope.kind == "module" else self._static_nested).truth(test)

    def _evaluated_parts(self, test: ast.expr, scope: _Scope) -> list[ast.AST]:
        """The parts of ``test`` that run: ``and``/``or`` stop at the first
        operand that decides them."""
        if not isinstance(test, ast.BoolOp):
            return [test]
        stop = isinstance(test.op, ast.Or)
        parts: list[ast.AST] = []
        for value in test.values:
            parts.append(value)
            if self._truth(value, scope) is stop:
                break
        return parts

    def _function_scope(self, stmt: ast.FunctionDef | ast.AsyncFunctionDef, scope: _Scope) -> _Scope:
        prefix = scope.qualname
        if prefix is None:
            qualname = stmt.name
        elif scope.kind == "class":
            qualname = f"{prefix}.{stmt.name}"
        else:
            qualname = f"{prefix}.<locals>.{stmt.name}"
        inner = _Scope("function", scope, stmt.body, stmt, qualname)
        args = stmt.args
        for arg in (
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *([args.vararg] if args.vararg else []),
            *([args.kwarg] if args.kwarg else []),
        ):
            inner.locals.add(arg.arg)
            self._bind(arg.arg, ("param", qualname, arg.arg), inner)
        self._bind_type_params(stmt, inner)
        self.function_scopes.setdefault(
            qualname, FunctionScope(frozenset(inner.locals), frozenset(inner.globals), frozenset(inner.nonlocals))
        )
        return inner

    def _bind_type_params(self, stmt: ast.stmt, inner: _Scope) -> None:
        """PEP 695 type parameters are names of the definition's own scope, bound
        to type variables: they hide the enclosing names and are never routers."""
        for param in getattr(stmt, "type_params", None) or ():
            inner.locals.add(param.name)
            self._bind(param.name, _OTHER, inner)

    def _run(self, stmt: ast.stmt, scope: _Scope) -> None:
        if isinstance(stmt, ast.If):
            self._run_if(stmt, scope)
            return
        roots = _statement_roots(stmt)
        self._scan(roots, scope)
        self._scan(_lazy_roots(stmt), scope, _ExprCtx(lazy=True))
        self._bind_walrus(roots, scope)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            self._run_assignment(stmt, scope)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._run_definition(stmt, scope)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self._run_import(stmt, scope)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match)) or (
            _TRY_TYPES and isinstance(stmt, _TRY_TYPES)
        ):
            self._run_compound(stmt, scope)
        else:
            self._run_simple(stmt, scope)

    def _run_assignment(self, stmt: ast.Assign | ast.AnnAssign | ast.AugAssign, scope: _Scope) -> None:
        if isinstance(stmt, ast.Assign):
            self._mutations(stmt.targets, stmt, scope)
            self._state_alias(stmt.value, stmt, scope)
            for target in stmt.targets:
                self._bind_target(target, stmt.value, scope)
            self._record_ctor_binding(stmt.targets, stmt.value, scope)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self._mutations([stmt.target], stmt, scope)
                self._state_alias(stmt.value, stmt, scope)
                self._bind_target(stmt.target, stmt.value, scope)
                self._record_ctor_binding([stmt.target], stmt.value, scope)
        else:
            self._mutations([stmt.target], stmt, scope)
            self._bind_target(stmt.target, None, scope)

    def _run_definition(self, stmt: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, scope: _Scope) -> None:
        if not isinstance(stmt, ast.ClassDef):
            self._decorators(stmt, scope)
            self._bind(stmt.name, _OTHER, scope)
            inner = self._function_scope(stmt, scope)
            self._mark_def(inner, stmt, scope)
            self._pending.append((inner, stmt.body))
            return
        kinds = {self._ctor_kind(b) for b in stmt.bases} - {None}
        if kinds:
            self._ctor_kinds[stmt.name] = "FastAPI" if "FastAPI" in kinds else "APIRouter"
        prefix = scope.qualname
        qualname = stmt.name if prefix is None else f"{prefix}.{stmt.name}"
        if scope.kind == "function":
            qualname = f"{prefix}.<locals>.{stmt.name}"
        inner = _Scope("class", scope, stmt.body, stmt, qualname)
        self._bind_type_params(stmt, inner)
        self._run_block(stmt.body, inner)
        self._bind(stmt.name, _OTHER, scope)

    def _run_import(self, stmt: ast.Import | ast.ImportFrom, scope: _Scope) -> None:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    self._bind(alias.asname, ("module", alias.name), scope)
                else:
                    top = alias.name.split(".")[0]
                    self._bind(top, ("module", top), scope)
            return
        source = resolve_relative(self.module.name, self.module.is_package, stmt.level or 0, stmt.module)
        for alias in stmt.names:
            if alias.name == "*":
                continue
            verdict = ("import", source, alias.name) if source is not None else _UNRESOLVED
            self._bind(alias.asname or alias.name, verdict, scope)

    def _run_compound(self, stmt: ast.stmt, scope: _Scope) -> None:
        """Loops, ``with``, ``try`` and ``match``."""
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            if not isinstance(stmt, ast.While):
                self._bind_target(stmt.target, None, scope)
            self._branches(scope, [lambda: self._run_conditional(stmt.body, scope)], may_skip=True)
            self._branches(scope, [lambda: self._run_conditional(stmt.orelse, scope)], may_skip=True)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars, None, scope)
            self._run_block(stmt.body, scope)
        elif isinstance(stmt, ast.Match):
            self._branches(scope, [self._case(case, scope) for case in stmt.cases], may_skip=True)
        else:
            self._run_try(stmt, scope)

    def _run_simple(self, stmt: ast.stmt, scope: _Scope) -> None:
        """``del``, a type alias and ``return``."""
        if isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    self._set(scope, target.id, None)
        elif _TYPE_ALIAS_TYPES and isinstance(stmt, _TYPE_ALIAS_TYPES):
            name = stmt.name  # type: ignore[attr-defined]
            if isinstance(name, ast.Name):
                self._bind(name.id, _OTHER, scope)
        elif isinstance(stmt, ast.Return):
            function = scope.function()
            if function is not None and function.qualname is not None:
                verdict = self._eval(stmt.value, scope) if stmt.value is not None else _OTHER
                self.returns[function.qualname] = _join_known(self.returns.get(function.qualname), verdict) or verdict

    def _record_ctor_binding(self, targets: list[ast.expr], value: ast.expr, scope: _Scope) -> None:
        """Name the router a module-level ``name = APIRouter(...)`` builds."""
        if scope is not self._module or not isinstance(value, ast.Call):
            return
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            return
        for k in range(len(self.ctors) - 1, -1, -1):
            ctor = self.ctors[k]
            if ctor.node is value:
                self.ctors[k] = RouterCtor(ctor.module, ctor.node, ctor.kind, names[-1], ctor.seq)
                break

    def _case(self, case: ast.match_case, scope: _Scope) -> Callable[[], None]:
        def run() -> None:
            for node in ast.walk(case.pattern):
                if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                    self._bind(node.name, _UNRESOLVED, scope)
                elif isinstance(node, ast.MatchMapping) and node.rest:
                    self._bind(node.rest, _UNRESOLVED, scope)
            if case.guard is not None:
                self._scan([case.guard], scope, _ExprCtx(conditional=True))
            self._run_conditional(case.body, scope)

        return run

    def facts(self) -> ModuleRouterFacts:
        return ModuleRouterFacts(
            ctors=tuple(self.ctors),
            events=tuple(self.events),
            module_end=dict(self.module_end),
            function_returns=dict(self.returns),
            problems=tuple(self.problems),
            calls=tuple(self.calls),
        )


# ── Module entry points ──────────────────────────────────────────────────


def _decorator_attr(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


@dataclass(frozen=True)
class PrefilterFacts:
    """What the prefilter needs from one module, gathered in one walk.

    ``direct``: the module mentions a router class or a router method;
    ``imported``: ``(module, name)`` pairs it imports with ``from ... import``
    (``name`` is ``"*"`` for a star import); ``modules``: modules it imports whole.
    """

    direct: bool
    imported: frozenset[tuple[str | None, str]]
    modules: frozenset[str]

    def touches(self, router_names: frozenset[tuple[str, str]]) -> bool:
        if self.direct:
            return True
        if not router_names:
            return False
        router_modules = {m for m, _ in router_names}
        for source, name in self.imported:
            if name == "*" or (source, name) in router_names or f"{source}.{name}" in router_modules:
                return True
        return bool(self.modules & router_modules)


def prefilter_facts(mod: PyModule) -> PrefilterFacts:
    direct = False
    imported: set[tuple[str | None, str]] = set()
    modules: set[str] = set()
    for node in ast.walk(mod.tree):
        if direct:
            break
        if isinstance(node, ast.Name):
            direct = node.id in ROUTER_CLASSES
        elif isinstance(node, ast.Attribute):
            direct = node.attr in ROUTER_CLASSES
        elif isinstance(node, ast.Call):
            func = node.func
            direct = (isinstance(func, ast.Attribute) and func.attr in _TOUCH_CALLS) or (
                _decorator_attr(func) in _TOUCH_DECORATORS
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            direct = any(_decorator_attr(d) in _TOUCH_DECORATORS for d in node.decorator_list)
        elif isinstance(node, ast.ImportFrom):
            source = resolve_relative(mod.name, mod.is_package, node.level or 0, node.module)
            for alias in node.names:
                if alias.name in ROUTER_CLASSES:
                    direct = True
                imported.add((source, alias.name))
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return PrefilterFacts(direct, frozenset(imported), frozenset(modules))


def module_may_touch_routers(mod: PyModule, router_names: frozenset[tuple[str, str]] = frozenset()) -> bool:
    """Cheap prefilter: whether replaying ``mod`` can record anything.

    True when the module mentions ``APIRouter`` or ``FastAPI``; decorates with
    ``@<expr>.get/post/.../api_route/websocket/route/websocket_route(...)`` (or
    applies one by hand); calls ``include_router``, ``add_api_route``,
    ``add_api_websocket_route``, ``mount``, ``add_route`` or
    ``add_websocket_route``; or imports a name another module binds to a router
    (``router_names``: ``(module, name)``), or that module, or ``*``.
    """
    return prefilter_facts(mod).touches(router_names)


@dataclass(frozen=True)
class FunctionScope:
    """A function's own names, from the replay: ``local`` (bound in its body,
    parameters included), ``declared_global`` and ``declared_nonlocal``."""

    local: frozenset[str]
    declared_global: frozenset[str]
    declared_nonlocal: frozenset[str]


@dataclass(frozen=True)
class ModuleAnalysis:
    """``ModuleRouterFacts`` plus what the graph needs beyond them: the argument
    expression of each argument escape (by event ``seq``), every function's
    and class's qualname (by node id), each function node by qualname, and each
    replayed function's scope (by qualname)."""

    facts: ModuleRouterFacts
    escape_args: dict[int, ast.expr]
    qualnames: dict[int, str]
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    scopes: dict[str, FunctionScope] = field(default_factory=dict)


def analyze(mod: PyModule) -> ModuleAnalysis:
    """Router facts for one module; an unexpected failure becomes an
    ``analysis_failed`` diagnostic and empty facts, never an exception."""
    names = qualnames(mod.tree)
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(mod.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(names[id(node)], node)
    try:
        bases = RouterBases(mod)
        return ModuleAnalysis(bases.facts(), dict(bases.escape_args), names, functions, dict(bases.function_scopes))
    except Exception as exc:  # noqa: BLE001 - one module never stops the analysis
        problem = diagnostic("analysis_failed", stage=STAGE, step="analyze module", file=mod.rel, exc=exc)
    empty = ModuleRouterFacts(ctors=(), events=(), module_end={}, function_returns={}, problems=(problem,))
    return ModuleAnalysis(empty, {}, names, functions)


__all__ = [
    "API_ROUTE",
    "DEFAULT_METHODS",
    "HTTP_METHOD_DECORATORS",
    "INCLUDE_ROUTER",
    "MOUNT",
    "MUTATED_ATTRIBUTES",
    "REGISTRATION_CALLS",
    "ROUTER_CLASSES",
    "ROUTERISH_KINDS",
    "SAFE_ARGUMENT_CALLS",
    "STARLETTE_REGISTRATIONS",
    "FunctionScope",
    "ModuleAnalysis",
    "RouterBases",
    "analyze",
    "call_argument",
    "dotted_label",
    "is_route_decorator",
    "method_names",
    "METHOD_REFERENCES",
    "PrefilterFacts",
    "module_may_touch_routers",
    "prefilter_facts",
    "qualnames",
    "router_config",
    "scope_names",
]
