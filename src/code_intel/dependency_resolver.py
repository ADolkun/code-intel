"""FastAPI dependency identity and closures, read from source with FastAPI's rules.

Every ``Depends``/``Security`` target is resolved at its use site to a canonical
``module:qualname`` id, so two functions that share a name stay distinct and an
import alias reaches the same definition as the original name.

Parameter semantics follow ``fastapi.dependencies.utils.analyze_param`` (0.135.2):

- ``*args``/``**kwargs`` are skipped; so is the bound first parameter of methods,
  classmethods, ``__init__`` and ``__call__``.
- An annotation is unwrapped through string forward references, assignment
  aliases (across modules and re-exports), one level of PEP 695 ``type X = ...``,
  and nested ``Annotated`` (which Python flattens).
- Inside ``Annotated`` only the last FastAPI marker counts; a parameter marker
  there (``Query()``, ``Body()``, ...) means the parameter is not a dependency.
- A ``Depends``/``Security`` default combined with any FastAPI marker in
  ``Annotated`` is rejected by FastAPI and reported as ``unresolved_dependency``.
- ``Depends()`` without a target depends on the annotation's type.
- A ``@dataclass`` dependency's parameters are its fields.
- A factory call is identified as what it returns: a closure by the factory, a
  class instance by its class, a module function by itself. A wrapper made
  with ``functools.wraps(fn)`` is identified as ``fn`` and takes its parameters.
- ``functools.partial(f, ...)`` is identified as ``f``, with the parameters it
  binds removed or given the bound default.

Deliberately not modelled: security scopes, ``use_cache``, ``scope`` and
``app.dependency_overrides`` (a runtime test hook). A class whose constructor
comes from a base outside the analyzed package contributes no sub-dependencies.
A package decorator that does not apply ``functools.wraps`` is reported
(``opaque_decorator``); decorators from outside the package are assumed to keep
the signature.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from code_intel import problems
from code_intel.backend_model import (
    Def,
    DepKind,
    DepMarker,
    DepTarget,
    DepUse,
    DepVia,
    External,
    LiveRoute,
    Located,
    ModuleRef,
    NameResolver,
    PyModule,
    Target,
    Unknown,
    Value,
)
from code_intel.problems import Problem
from code_intel.py_values import opaque_decorator

STAGE = "resolve dependencies"

FASTAPI_MARKER_MODULES = frozenset({"fastapi", "fastapi.params", "fastapi.param_functions"})
DEPENDENCY_MARKERS = frozenset({"Depends", "Security"})
# fastapi.params.Param and fastapi.params.Body subclasses: FastAPI's "specific"
# annotations, which compete with Depends for the last position in Annotated.
PARAM_MARKERS = frozenset({"Query", "Path", "Body", "Header", "Cookie", "Form", "File"})
ANNOTATED = frozenset({"typing.Annotated", "typing_extensions.Annotated"})
DATACLASS_DECORATORS = frozenset({"dataclasses.dataclass", "pydantic.dataclasses.dataclass"})
# Pydantic's accepts only init=False and still generates its own __init__.
STDLIB_DATACLASS = frozenset({"dataclasses.dataclass"})
DATACLASS_FIELD = frozenset({"dataclasses.field", "pydantic.dataclasses.Field", "pydantic.Field"})
PARTIAL = "functools.partial"
WRAPS = "functools.wraps"
CLASS_VARS = frozenset({"typing.ClassVar", "typing_extensions.ClassVar"})

MAX_DEPTH = 32
TEXT_MAX = 60

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# Statement fields whose bodies run in the enclosing scope (no new scope).
_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
_TYPE_ALIAS_TYPES: tuple[type, ...] = (ast.TypeAlias,)
_PARSE_ERRORS: tuple[type[BaseException], ...] = (SyntaxError, ValueError, RecursionError, MemoryError)

BOTH_DECLARED_MESSAGE = "FastAPI rejects a dependency declared in both Annotated and the default"


class ModuleSource(Protocol):
    """The part of ``py_sources.PySources`` the resolver reads."""

    def module(self, name: str) -> PyModule | None: ...


# ── Internal shapes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Params:
    """A callable whose parameters FastAPI reads."""

    module: str
    fn: ast.AST  # FunctionDef, AsyncFunctionDef or Lambda
    skip_first: bool
    # What ``functools.partial`` binds: the leading positional parameters it
    # fills, which FastAPI no longer sees, and keyword values, which become
    # those parameters' defaults.
    bound_positional: int = 0
    bound_keywords: tuple[_Bound, ...] = ()


@dataclass(frozen=True)
class _Bound:
    """A keyword value a ``functools.partial`` binds, resolved where the partial is."""

    name: str
    module: str
    value: ast.expr
    ctx: ast.AST


@dataclass(frozen=True)
class _Member:
    """A function found as an attribute of a class or of an instance of it."""

    module: str
    qualname: str
    node: ast.AST
    via_instance: bool


@dataclass(frozen=True)
class _Instance:
    """The result of calling a class: FastAPI calls its ``__call__``."""

    cls: Def
    call: Located


_Resolved = Target | _Member | _Instance


@dataclass(frozen=True)
class _Callable:
    target: DepTarget
    params: tuple[_Params, ...]  # empty when FastAPI reads no parameters we can see


@dataclass(frozen=True)
class _Marker:
    kind: str  # "Depends", "Security" or a PARAM_MARKERS name
    loc: Located  # the marker call
    ctx: ast.AST  # in-tree node whose scope names in the call resolve against


@dataclass(frozen=True)
class _Annotation:
    markers: tuple[_Marker, ...]
    type_expr: ast.expr | None
    type_ctx: ast.AST | None
    type_module: str
    problem: str | None = None


@dataclass
class _ModuleIndex:
    module: PyModule
    parents: dict[int, ast.AST]
    top_index: dict[int, int]  # id(top-level statement) -> index


def _text(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except _PARSE_ERRORS:
        text = type(node).__name__
    text = " ".join(text.split())
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _render_arg(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Attribute):
        dotted = _dotted(node)
        if dotted is not None:
            return dotted
    return "…"


def call_display(name: str, call: ast.Call) -> str:
    """``name(args)`` with each argument rendered as the DI output shows it."""
    args = [_render_arg(a) for a in call.args]
    args += [f"{kw.arg}={_render_arg(kw.value)}" if kw.arg else f"**{_render_arg(kw.value)}" for kw in call.keywords]
    return f"{name}({', '.join(args)})"


def _id_qualname(qualname: str) -> str:
    """FastAPI reports a closure under the function that created it."""
    return qualname.split(".<locals>", 1)[0]


def _instance_qualname(qualname: str, *, member: bool) -> str:
    """The id qualname of an instance (``C()``) or of a method bound to one
    (``C().m``). FastAPI reads ``__call__`` of an instance but ``__init__`` of
    its class, and a bound method without its first parameter, so these forms
    need ids of their own."""
    owner, dot, attr = qualname.rpartition(".")
    marked = f"{owner}().{attr}" if member and dot else f"{qualname}()"
    return _id_qualname(marked)


def _decorator_names(fn: ast.AST) -> set[str]:
    return {d.id for d in getattr(fn, "decorator_list", ()) if isinstance(d, ast.Name)}


def _same(a: _Resolved, b: _Resolved) -> bool:
    if isinstance(a, Def) and isinstance(b, Def):
        return (a.module, a.qualname) == (b.module, b.qualname)
    if isinstance(a, Value) and isinstance(b, Value):
        return a.loc.module == b.loc.module and a.loc.node is b.loc.node
    return a == b


def _distinct(targets: Iterable[_Resolved]) -> list[_Resolved]:
    out: list[_Resolved] = []
    for target in targets:
        if not any(_same(target, seen) for seen in out):
            out.append(target)
    return out


class DependencyResolver:
    """Resolve route and dependency parameters to ``DepUse``/``DepTarget``.

    ``resolver`` evaluates module-level names and values; ``sources`` maps a
    module name to its parsed ``PyModule``. Problems found while resolving are
    collected in ``problems`` (limitations only, de-duplicated).
    """

    def __init__(self, resolver: NameResolver, sources: ModuleSource, *, stage: str = STAGE) -> None:
        self._resolver = resolver
        self._sources = sources
        self._stage = stage
        self._index: dict[str, _ModuleIndex | None] = {}
        self._sub_deps: dict[tuple, list[DepUse]] = {}
        self._callables: dict[tuple, _Callable] = {}
        self._factories: dict[tuple, _Callable] = {}
        self._problems: list[Problem] = []
        self._problem_keys: set[tuple[str, str | None, int | None, str]] = set()

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def problems(self) -> tuple[Problem, ...]:
        return tuple(self._problems)

    def route_deps(self, route: LiveRoute) -> list[DepUse]:
        """Route-level layers in FastAPI order (app, routers, decorator), then handler parameters."""
        uses: list[DepUse] = []
        for layer in route.layers:
            if isinstance(layer.dependencies, Unknown):
                # The graph builder reports an unevaluable dependencies= value.
                continue
            for loc in layer.dependencies:
                use = self.marker_use(loc, layer.via)
                if use is not None:
                    uses.append(use)
        uses.extend(self.endpoint_deps(route.endpoint))
        return uses

    def layer_uses(self, dependencies: Sequence[Located], via: DepVia) -> list[DepUse]:
        """Uses for one ``dependencies=[...]`` list (a router's own or an include's)."""
        out = []
        for loc in dependencies:
            use = self.marker_use(loc, via)
            if use is not None:
                out.append(use)
        return out

    def endpoint_deps(self, endpoint: Located) -> list[DepUse]:
        """Dependencies FastAPI reads from a route handler's parameters."""
        node = endpoint.node
        if isinstance(node, (*_FUNCTION_NODES, ast.Lambda)):
            return self._param_uses(_Params(endpoint.module, node, skip_first=False))
        # pyrefly: ignore[bad-argument-type]
        callable_ = self._classify(endpoint.module, node, node, alias=None, depth=0)
        if isinstance(callable_, str):
            self._limit(
                "unresolved_dependency",
                endpoint.module,
                endpoint.line,
                f"route endpoint {_text(node)}: could not resolve the handler to a function ({callable_})",
            )
            return []
        uses: list[DepUse] = []
        for params in callable_[0].params:
            uses.extend(self._param_uses(params))
        return uses

    def sub_deps(self, target: DepTarget) -> list[DepUse]:
        """The direct dependencies of ``target`` (its parameters' ``Depends``/``Security``)."""
        key = _key(target)
        cached = self._sub_deps.get(key)
        if cached is not None:
            return cached
        self._sub_deps[key] = []  # cycle guard: a self-referencing dependency stops here
        callable_ = self._callables.get(key)
        uses: list[DepUse] = []
        if callable_ is not None:
            for params in callable_.params:
                uses.extend(self._param_uses(params))
        self._sub_deps[key] = uses
        return uses

    def closure(self, uses: Iterable[DepUse]) -> list[DepTarget]:
        """Every target reachable from ``uses``, unique by id, in first-seen order."""
        out: dict[str, DepTarget] = {}
        for target in self.forms(uses):
            out.setdefault(target.id, target)
        return list(out.values())

    def forms(self, uses: Iterable[DepUse]) -> list[DepTarget]:
        """Every target reachable from ``uses``, one per callable FastAPI reads
        (so an id shared by a partial and its function appears twice), in first-seen order."""
        out: list[DepTarget] = []
        expanded: set[tuple] = set()

        def visit(target: DepTarget) -> None:
            key = _key(target)
            if key in expanded:
                return
            expanded.add(key)
            out.append(target)
            for sub in self.sub_deps(target):
                visit(sub.target)

        for use in uses:
            visit(use.target)
        return out

    def marker_use(self, loc: Located, via: DepVia, param: str | None = None) -> DepUse | None:
        """The use declared by one ``dependencies=`` element (a ``Depends``/``Security`` call)."""
        ctx = loc.node
        # pyrefly: ignore[bad-argument-type]
        marker = self._marker_of_value(loc.module, loc.node, ctx)
        if marker is None or marker.kind not in DEPENDENCY_MARKERS:
            self._limit(
                "unresolved_dependencies",
                loc.module,
                loc.line,
                f"dependencies= element {_text(loc.node)}: not a Depends(...)/Security(...) call",
            )
            return None
        return self._use_from_marker(marker, via=via, param=param, type_expr=None)

    # ── Parameters ───────────────────────────────────────────────────────

    def _param_uses(self, params: _Params) -> list[DepUse]:
        bound = {b.name: b for b in params.bound_keywords}
        uses: list[DepUse] = []
        for arg, default in _read_parameters(params):
            kw = bound.get(arg.arg)
            if kw is None:
                use = self._param_use(params.module, arg, default)
            else:
                use = self._param_use(params.module, arg, kw.value, default_at=(kw.module, kw.ctx))
            if use is not None:
                uses.append(use)
        return uses

    def _param_use(
        self,
        module: str,
        arg: ast.arg,
        default: ast.expr | None,
        *,
        default_at: tuple[str, ast.AST] | None = None,
    ) -> DepUse | None:
        """The dependency parameter ``arg`` declares, if any. ``default_at`` is
        where a default bound by ``functools.partial`` resolves."""
        annotation = self._annotation(module, arg.annotation, arg, depth=0, top=True)
        if annotation.problem is not None:
            self._limit(
                "unresolved_dependency",
                module,
                arg.lineno,
                f"parameter {arg.arg!r}: {annotation.problem}",
            )
        last = annotation.markers[-1] if annotation.markers else None
        default_module, default_ctx = default_at or (module, default)
        # pyrefly: ignore[bad-argument-type]
        default_marker = self._marker_of_value(default_module, default, default_ctx) if default is not None else None
        depends: _Marker | None = None
        if default_marker is not None and default_marker.kind in DEPENDENCY_MARKERS:
            if last is not None:
                self._limit(
                    "unresolved_dependency",
                    module,
                    arg.lineno,
                    f"parameter {arg.arg!r}: {BOTH_DECLARED_MESSAGE}",
                )
                return None
            depends = default_marker
        elif last is not None and last.kind in DEPENDENCY_MARKERS:
            depends = last
        if depends is None:
            return None
        return self._use_from_marker(
            depends,
            via="direct",
            param=arg.arg,
            type_expr=(annotation.type_module, annotation.type_expr, annotation.type_ctx),
        )

    def _use_from_marker(
        self,
        marker: _Marker,
        *,
        via: DepVia,
        param: str | None,
        type_expr: tuple[str, ast.expr | None, ast.AST | None] | None,
    ) -> DepUse:
        call = marker.loc.node
        assert isinstance(call, ast.Call)
        module = marker.loc.module
        target_expr: ast.expr | None = call.args[0] if call.args else None
        for kw in call.keywords:
            if kw.arg == "dependency":
                target_expr = kw.value
        if isinstance(target_expr, ast.Constant) and target_expr.value is None:
            target_expr = None
        marker_kind: DepMarker = "Security" if marker.kind == "Security" else "Depends"
        line = getattr(call, "lineno", 0)
        file = self._file(module)
        if target_expr is not None:
            resolved = self._classify(module, target_expr, marker.ctx, alias=None, depth=0)
            text = _text(target_expr)
        elif type_expr is not None and type_expr[1] is not None:
            type_module, expr, ctx = type_expr
            # pyrefly: ignore[bad-argument-type]
            resolved = self._classify(type_module, expr, ctx or expr, alias=None, depth=0)
            # pyrefly: ignore[bad-argument-type]
            text = _text(expr)
        else:
            resolved = "FastAPI needs a callable, and there is no target or type annotation"
            text = _text(call)
        if isinstance(resolved, str):
            self._limit(
                "unresolved_dependency",
                module,
                line,
                f"{marker_kind}({text}): could not resolve the dependency to a callable ({resolved})",
            )
            target = DepTarget(
                id=f"unresolved:{module}:{text}",
                module=module,
                qualname=text,
                name=text,
                kind="unresolved",
                file=file,
                line=line,
            )
            return DepUse(
                target=target,
                display=text,
                via=via,
                param=param,
                alias=None,
                marker=marker_kind,
                file=file or "",
                line=line,
            )
        callable_, display, alias = resolved
        return DepUse(
            target=callable_.target,
            display=display,
            via=via,
            param=param,
            alias=alias,
            marker=marker_kind,
            file=file or "",
            line=line,
        )

    # ── Annotations and markers ──────────────────────────────────────────

    def _annotation(self, module: str, expr: ast.expr | None, ctx: ast.AST, *, depth: int, top: bool) -> _Annotation:
        if expr is None:
            return _Annotation((), None, None, module)
        if depth > MAX_DEPTH:
            return _Annotation(
                (), None, None, module, problem=f"annotation {_text(expr)} nests deeper than {MAX_DEPTH}"
            )
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                parsed = ast.parse(expr.value.strip(), mode="eval").body
            except _PARSE_ERRORS:
                return _Annotation((), None, None, module)
            # Names in a forward reference resolve where the string is written.
            return self._annotation(module, parsed, ctx, depth=depth + 1, top=top)
        if isinstance(expr, ast.Subscript) and self._is_annotated(module, expr.value, ctx):
            elts = list(expr.slice.elts) if isinstance(expr.slice, ast.Tuple) else [expr.slice]
            if not elts:
                return _Annotation((), None, None, module, problem="Annotated[()] has no type argument")
            inner = self._annotation(module, elts[0], ctx, depth=depth + 1, top=False)
            own: list[_Marker] = []
            for elt in elts[1:]:
                marker = self._marker_of_value(module, elt, ctx)
                if marker is not None:
                    own.append(marker)
            return _Annotation(
                (*inner.markers, *own), inner.type_expr, inner.type_ctx, inner.type_module, inner.problem
            )
        if isinstance(expr, (ast.Name, ast.Attribute)):
            targets = _distinct(self._targets(module, expr, ctx, depth=depth))
            if len(targets) == 1 and isinstance(targets[0], Value):
                loc = targets[0].loc
                node, is_type_alias = self._alias_value(loc)
                if node is not None and (top or not is_type_alias):
                    alias_ctx = loc.node
                    return self._annotation(loc.module, node, alias_ctx, depth=depth + 1, top=False)
        return _Annotation((), expr, ctx, module)

    def _alias_value(self, loc: Located) -> tuple[ast.expr | None, bool]:
        """``(aliased expression, is a PEP 695 alias)`` for an annotation alias binding."""
        node = loc.node
        if _TYPE_ALIAS_TYPES and isinstance(node, _TYPE_ALIAS_TYPES):
            return node.value, True  # type: ignore[attr-defined]
        index = self._module_index(loc.module)
        parent = index.parents.get(id(node)) if index is not None else None
        if _TYPE_ALIAS_TYPES and isinstance(parent, _TYPE_ALIAS_TYPES):
            return node if isinstance(node, ast.expr) else None, True
        return (node if isinstance(node, ast.expr) else None), False

    def _is_annotated(self, module: str, expr: ast.expr, ctx: ast.AST) -> bool:
        targets = _distinct(self._targets(module, expr, ctx, depth=0))
        return len(targets) == 1 and isinstance(targets[0], External) and targets[0].dotted in ANNOTATED

    def _marker_kind(self, module: str, call: ast.Call, ctx: ast.AST) -> str | None:
        targets = _distinct(self._targets(module, call.func, ctx, depth=0))
        if len(targets) != 1 or not isinstance(targets[0], External):
            return None
        owner, _, name = targets[0].dotted.rpartition(".")
        if owner in FASTAPI_MARKER_MODULES and (name in DEPENDENCY_MARKERS or name in PARAM_MARKERS):
            return name
        return None

    def _marker_of_value(self, module: str, expr: ast.expr | None, ctx: ast.AST, depth: int = 0) -> _Marker | None:
        """The FastAPI marker an expression evaluates to: a marker call, or a name bound to one."""
        if expr is None or depth > MAX_DEPTH:
            return None
        if isinstance(expr, ast.Call):
            kind = self._marker_kind(module, expr, ctx)
            return _Marker(kind, Located(module, expr), ctx) if kind is not None else None
        if isinstance(expr, (ast.Name, ast.Attribute)):
            targets = _distinct(self._targets(module, expr, ctx, depth=depth))
            if len(targets) == 1 and isinstance(targets[0], Value):
                loc = targets[0].loc
                if isinstance(loc.node, ast.expr):
                    return self._marker_of_value(loc.module, loc.node, loc.node, depth + 1)
        return None

    # ── Callables ────────────────────────────────────────────────────────

    def _classify(
        self, module: str, expr: ast.expr, ctx: ast.AST, *, alias: str | None, depth: int
    ) -> tuple[_Callable, str, str | None] | str:
        """``(callable, display, alias)`` for a dependency target, or the reason it is unresolved."""
        if depth > MAX_DEPTH:
            return f"alias chain deeper than {MAX_DEPTH}"
        if isinstance(expr, ast.Lambda):
            return self._lambda(module, expr), "<lambda>", alias
        if isinstance(expr, ast.Call):
            return self._classify_call(module, expr, ctx, alias=alias, depth=depth)
        if not isinstance(expr, (ast.Name, ast.Attribute)):
            return f"{type(expr).__name__} is not a callable reference"
        targets = _distinct(self._targets(module, expr, ctx, depth=depth))
        if not targets:
            return f"no binding for {_text(expr)}"
        if len(targets) > 1:
            return f"{_text(expr)} has {len(targets)} possible bindings"
        target = targets[0]
        name = expr.id if isinstance(expr, ast.Name) else expr.attr
        if isinstance(target, Value):
            node = target.loc.node
            if not isinstance(node, ast.expr):
                return f"{_text(expr)} is not bound to an expression"
            return self._classify(
                target.loc.module,
                node,
                node,
                alias=alias or name,
                depth=depth + 1,
            )
        resolved = self._callable_of(target)
        if isinstance(resolved, str):
            return resolved
        callable_, display = resolved
        if alias == display:
            alias = None
        return callable_, display, alias

    def _classify_call(
        self, module: str, call: ast.Call, ctx: ast.AST, *, alias: str | None, depth: int
    ) -> tuple[_Callable, str, str | None] | str:
        targets = _distinct(self._targets(module, call.func, ctx, depth=depth))
        if not targets:
            return f"no binding for {_text(call.func)}"
        if len(targets) > 1:
            return f"{_text(call.func)} has {len(targets)} possible bindings"
        target = targets[0]
        if isinstance(target, Def) and isinstance(target.node, ast.ClassDef):
            resolved = self._callable_of(_Instance(target, Located(module, call)))
        elif isinstance(target, Def) and isinstance(target.node, _FUNCTION_NODES):
            resolved = self._factory(target, call, module, depth)
        elif isinstance(target, External) and target.dotted == PARTIAL:
            return self._partial(module, call, ctx, alias=alias, depth=depth)
        elif isinstance(target, External):
            return f"{_text(call.func)} is {target.dotted}, outside the analyzed package"
        else:
            return f"{_text(call.func)} is not a class or function of the analyzed package"
        if isinstance(resolved, str):
            return resolved
        callable_, display = resolved
        return callable_, display, alias

    def _partial(
        self, module: str, call: ast.Call, ctx: ast.AST, *, alias: str | None, depth: int
    ) -> tuple[_Callable, str, str | None] | str:
        """``functools.partial(f, ...)``: the runtime identifies it as ``f``, and
        FastAPI reads ``f``'s signature without the leading positional parameters
        it binds, with the keyword values it binds as defaults. Other uses of
        ``f`` keep ``f``'s own parameters."""
        if not call.args or isinstance(call.args[0], ast.Starred):
            return f"{_text(call)} has no callable argument"
        if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
            return f"{_text(call)} binds arguments through * or **, so which parameters FastAPI reads is unknown"
        resolved = self._classify(module, call.args[0], ctx, alias=alias, depth=depth + 1)
        if isinstance(resolved, str):
            return resolved
        callable_, display, alias = resolved
        keywords = tuple(_Bound(kw.arg, module, kw.value, ctx) for kw in call.keywords if kw.arg)
        params = tuple(
            replace(
                p,
                bound_positional=p.bound_positional + len(call.args) - 1,
                bound_keywords=(*(b for b in p.bound_keywords if b.name not in {k.name for k in keywords}), *keywords),
            )
            for p in callable_.params
        )
        site = f"{module}:{getattr(call, 'lineno', 0)}:{getattr(call, 'col_offset', 0)}"
        return self._register(replace(callable_.target, variant=site), params), display, alias

    def _check_decorators(self, target: Def) -> None:
        node = target.node
        at = self._at(target.module, node)
        for dec in getattr(node, "decorator_list", ()):
            reason = opaque_decorator(self._resolver, target.module, dec, at)
            if reason is not None:
                self._limit(
                    "opaque_decorator",
                    target.module,
                    getattr(dec, "lineno", None),
                    f"dependency {target.qualname}: @{_text(dec)}: {reason}, so FastAPI reads that wrapper's "
                    f"parameters, not {getattr(node, 'name', target.qualname)}()'s; its dependencies are reported "
                    "from the function",
                )

    def _callable_of(self, target: _Resolved) -> tuple[_Callable, str] | str:
        if isinstance(target, Def):
            return self._callable_def(target)
        if isinstance(target, _Member):
            decorators = _decorator_names(target.node)
            # Static and class methods are the same callable through the class
            # or an instance; only a plain method is bound to the instance.
            bound = False
            if "staticmethod" in decorators:
                skip = False
            elif "classmethod" in decorators:
                skip = True
            else:
                skip = bound = target.via_instance
            dep = self._target(target.module, target.qualname, "method", target.node, bound=bound)
            return self._register(dep, (_Params(target.module, target.node, skip_first=skip),)), dep.name
        if isinstance(target, _Instance):
            call_member = self._class_member(target.cls, "__call__")
            if call_member is None or not isinstance(call_member.node, _FUNCTION_NODES):
                return f"instance of {target.cls.qualname} has no __call__ in the analyzed package"
            dep = self._target(target.cls.module, target.cls.qualname, "instance", target.cls.node, bound=True)
            params = (_Params(call_member.module, call_member.node, skip_first=True),)
            node = target.call.node
            display = call_display(dep.name, node) if isinstance(node, ast.Call) else dep.name
            return self._register(dep, params), display
        if isinstance(target, External):
            return f"{target.dotted} is outside the analyzed package"
        if isinstance(target, ModuleRef):
            return f"{target.name} is a module"
        if isinstance(target, Unknown):
            return f"{target.reason}: {target.text}"
        return "unsupported target"

    def _callable_def(self, target: Def) -> tuple[_Callable, str] | str:
        """A lambda, class or function used as a dependency."""
        node = target.node
        if isinstance(node, ast.Lambda):
            return self._lambda(target.module, node), "<lambda>"
        if isinstance(node, ast.ClassDef):
            dep = self._target(target.module, target.qualname, "class", node)
            return self._register(dep, self._class_params(target)), dep.name
        if isinstance(node, _FUNCTION_NODES):
            self._check_decorators(target)
            dep = self._target(target.module, target.qualname, "function", node)
            return self._register(dep, (_Params(target.module, node, skip_first=False),)), dep.name
        return f"{target.qualname} is not a function or class"

    def _class_params(self, cls: Def) -> tuple[_Params, ...]:
        """What FastAPI reads from a class: its ``__init__``, or the one a
        ``@dataclass`` decorator generates."""
        params = self._constructor(cls, set())
        if isinstance(params, str):
            self._limit(
                "unresolved_dependency",
                cls.module,
                getattr(cls.node, "lineno", None),
                f"dependency class {cls.qualname}: {params}, so its parameters are not known",
            )
            return ()
        return params if params is not None else ()

    def _constructor(self, cls: Def, seen: set[tuple[str, str]]) -> tuple[_Params, ...] | str | None:
        """The ``__init__`` of ``cls`` or its nearest in-package base (depth-first,
        left to right), where a class's own ``__init__`` comes before the one its
        ``@dataclass`` generates, which comes before a base's; None when none is
        in the package, the reason when the analysis cannot tell."""
        key = (cls.module, cls.qualname)
        if key in seen or not isinstance(cls.node, ast.ClassDef):
            return None
        seen.add(key)
        for stmt in cls.node.body:
            if isinstance(stmt, _FUNCTION_NODES) and stmt.name == "__init__":
                return (_Params(cls.module, stmt, skip_first=True),)
        fields = self._dataclass_params(cls)
        if isinstance(fields, str):
            return fields
        if fields is not None:
            generated = self._generates_init(cls)
            if generated is not False:
                return fields if generated is True else generated
        for base in cls.node.bases:
            for target in _distinct(self._targets(cls.module, base, base, depth=0)):
                if isinstance(target, Def) and isinstance(target.node, ast.ClassDef):
                    found = self._constructor(target, seen)
                    if isinstance(found, str):
                        return f"base class {target.qualname}: {found}"
                    if found is not None:
                        return found
        return None

    def _generates_init(self, cls: Def) -> bool | str:
        """Whether the dataclass decorator on ``cls`` generates ``__init__``;
        pydantic's always does, the standard library's unless ``init=False``."""
        assert isinstance(cls.node, ast.ClassDef)
        for dec in cls.node.decorator_list:
            if not (isinstance(dec, ast.Call) and self._is_external(cls.module, dec.func, dec, STDLIB_DATACLASS)):
                continue
            init = next((k.value for k in dec.keywords if k.arg == "init"), None)
            if init is None:
                continue
            if not isinstance(init, ast.Constant):
                return f"@{_text(dec)} may or may not generate __init__"
            if not init.value:
                return False
        return True

    def _factory(self, factory: Def, call: ast.Call, call_module: str, depth: int) -> tuple[_Callable, str] | str:
        """A factory call. FastAPI identifies the result by what it is: a closure
        by the factory, an instance by its class, a module function by itself,
        and a wrapper made with ``functools.wraps(fn)`` by the ``fn`` the call
        passes (whose parameters it then reads); otherwise the parameters are
        the returned callable's."""
        own = self._target(factory.module, factory.qualname, "factory_result", factory.node)
        display = call_display(own.name, call)
        wrapped = self._wrapped_argument(factory, call, call_module, depth)
        key = (factory.module, factory.qualname, wrapped[0].target.id if wrapped is not None else None)
        known = self._factories.get(key)
        if known is not None:
            return known, display
        returned: list[_Params] = []
        ids: list[DepTarget] = []
        params_by_id: dict[str, set[tuple[_Params, ...]]] = {}
        values = self._returned_values(factory.node)
        for value in values:
            resolved = self._classify(factory.module, value, value, alias=None, depth=depth + 1)
            if isinstance(resolved, str):
                self._limit(
                    "unresolved_dependency",
                    factory.module,
                    getattr(value, "lineno", None),
                    f"dependency factory {factory.qualname}: could not resolve the returned callable "
                    f"{_text(value)} ({resolved})",
                )
                continue
            is_wrapper = wrapped is not None and wrapped[1] is value
            # functools.wraps copies the wrapped function's name, so FastAPI identifies the wrapper by it.
            ids.append(wrapped[0].target if is_wrapper else resolved[0].target)  # type: ignore[index]
            params_of = wrapped[0].params if is_wrapper else resolved[0].params  # type: ignore[index]
            params_by_id.setdefault(ids[-1].id, set()).add(params_of)
            for params in params_of:
                if params not in returned:
                    returned.append(params)
        if not values:
            self._limit(
                "unresolved_dependency",
                factory.module,
                getattr(factory.node, "lineno", None),
                f"dependency factory {factory.qualname}: returns no callable the analyzer can read",
            )
        for shared_id, forms in params_by_id.items():
            if len(forms) > 1:
                # Which value a call returns depends on its arguments at runtime.
                self._limit(
                    "shared_dependency_id",
                    factory.module,
                    getattr(factory.node, "lineno", None),
                    f"dependency factory {factory.qualname} returns callables FastAPI reads with different "
                    f"parameters under one id ({shared_id}); every call of it is given the sub-dependencies "
                    "of all of them",
                )
        dep = own
        distinct = {t.id for t in ids}
        if len(distinct) == 1 and own.id not in distinct:
            first = ids[0]
            dep = DepTarget(
                first.id, first.module, first.qualname, first.name, "factory_result", first.file, first.line
            )
        elif len(distinct) > 1:
            self._limit(
                "unresolved_dependency",
                factory.module,
                getattr(factory.node, "lineno", None),
                f"dependency factory {factory.qualname} returns different callables ({', '.join(sorted(distinct))}), "
                f"which FastAPI identifies differently; the result is identified as {own.id}",
            )
        callable_ = self._register_factory(dep, tuple(returned), factory)
        self._factories[key] = callable_
        return callable_, display

    def _wrapped_argument(
        self, factory: Def, call: ast.Call, call_module: str, depth: int
    ) -> tuple[_Callable, ast.expr] | None:
        """``(callable passed as fn, returned value)`` when the factory returns a
        nested function decorated with ``functools.wraps(fn)`` for its parameter ``fn``."""
        fn = factory.node
        if not isinstance(fn, _FUNCTION_NODES):
            return None
        params = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
        for value in self._returned_values(fn):
            if not isinstance(value, ast.Name):
                continue
            nested = [s for s in fn.body if isinstance(s, _FUNCTION_NODES) and s.name == value.id]
            name = self._wraps_parameter(factory, nested[0]) if len(nested) == 1 else None
            if name is None:
                continue
            passed: ast.expr | None = None
            if name in params and params.index(name) < len(call.args):
                passed = call.args[params.index(name)]
            for kw in call.keywords:
                if kw.arg == name:
                    passed = kw.value
            if passed is None or isinstance(passed, ast.Starred):
                return None
            resolved = self._classify(call_module, passed, passed, alias=None, depth=depth + 1)
            if isinstance(resolved, str):
                return None
            return resolved[0], value
        return None

    def _wraps_parameter(self, factory: Def, nested: ast.AST) -> str | None:
        """The name ``fn`` in the first ``@functools.wraps(fn)`` decorating ``nested``."""
        for dec in getattr(nested, "decorator_list", ()):
            if not (isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Name)):
                continue
            targets = _distinct(self._targets(factory.module, dec.func, dec, depth=0))
            if len(targets) == 1 and isinstance(targets[0], External) and targets[0].dotted == WRAPS:
                return dec.args[0].id
        return None

    def _register_factory(self, dep: DepTarget, params: tuple[_Params, ...], factory: Def) -> _Callable:
        """Register a factory result; one id reached with different parameters
        (a wrapper factory applied to different functions) keeps all of them."""
        key = _key(dep)
        known = self._callables.get(key)
        if known is None:
            return self._register(dep, params)
        merged = known.params + tuple(p for p in params if p not in known.params)
        if merged == known.params:
            return known
        self._limit(
            "unresolved_dependency",
            factory.module,
            getattr(factory.node, "lineno", None),
            f"dependency factory {factory.qualname} returns callables with different parameters under one id "
            f"({dep.id}); their sub-dependencies are merged",
        )
        callable_ = _Callable(dep, merged)
        self._callables[key] = callable_
        self._sub_deps.pop(key, None)
        return callable_

    def _dataclass_params(self, cls: Def) -> tuple[_Params, ...] | str | None:
        """The ``__init__`` parameters a ``@dataclass`` decorator generates (its
        fields, base classes' first), one ``_Params`` per run of fields written
        in the same module, as names in a field resolve where it is written;
        None for an undecorated class, the reason for a class decorator the
        analysis cannot read."""
        node = cls.node
        assert isinstance(node, ast.ClassDef)
        if not node.decorator_list:
            return None
        for dec in node.decorator_list:
            expr = dec.func if isinstance(dec, ast.Call) else dec
            if not self._is_external(cls.module, expr, dec, DATACLASS_DECORATORS):
                return f"class decorator @{_text(dec)} may change its constructor"
        fields = self._inherited_fields(node, cls.module)
        if isinstance(fields, str):
            return fields
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            kind, default = self._field(cls.module, stmt)
            if kind == "not_init":
                fields.pop(stmt.target.id, None)
            elif kind == "field":
                fields[stmt.target.id] = (stmt, default, cls.module)
        return self._field_params(cls.module, fields)

    def _is_external(self, module: str, expr: ast.expr, ctx: ast.AST, names: frozenset[str]) -> bool:
        """Whether ``expr`` resolves to exactly one outside name among ``names``."""
        targets = _distinct(self._targets(module, expr, ctx, depth=0))
        return len(targets) == 1 and isinstance(targets[0], External) and targets[0].dotted in names

    def _inherited_fields(
        self, node: ast.ClassDef, module: str
    ) -> dict[str, tuple[ast.AnnAssign, ast.expr | None, str]] | str:
        """The fields of a dataclass's dataclass bases, or why one cannot be read."""
        fields: dict[str, tuple[ast.AnnAssign, ast.expr | None, str]] = {}
        for base in reversed(node.bases):
            for target in _distinct(self._targets(module, base, base, depth=0)):
                if isinstance(target, Def) and isinstance(target.node, ast.ClassDef):
                    inherited = self._dataclass_params(target)
                    if isinstance(inherited, str):
                        return inherited
                    if inherited is not None:
                        fields.update(self._synthetic_fields(inherited))
        return fields

    def _field(self, module: str, stmt: ast.AnnAssign) -> tuple[str, ast.expr | None]:
        """``("classvar", None)`` for a ``ClassVar``, ``("not_init", None)`` for
        ``field(init=False)``, else ``("field", default)`` with the default
        ``__init__`` gets (a ``default_factory`` counts as some default)."""
        annotation = stmt.annotation
        annotated = annotation.value if isinstance(annotation, ast.Subscript) else annotation
        if self._is_external(module, annotated, stmt, CLASS_VARS):
            return "classvar", None
        default: ast.expr | None = stmt.value
        if not (isinstance(default, ast.Call) and self._is_external(module, default.func, default, DATACLASS_FIELD)):
            return "field", default
        init = next((k.value for k in default.keywords if k.arg == "init"), None)
        if isinstance(init, ast.Constant) and init.value is False:
            return "not_init", None
        given = next((k.value for k in default.keywords if k.arg == "default"), None)
        factory = any(k.arg == "default_factory" for k in default.keywords)
        return "field", given if given is not None else (ast.Constant(None) if factory else None)

    def _field_params(
        self, module: str, fields: dict[str, tuple[ast.AnnAssign, ast.expr | None, str]]
    ) -> tuple[_Params, ...]:
        """Keyword-only parameters for ``fields``, one ``_Params`` per run of
        fields written in the same module."""
        index = self._module_index(module)
        runs: list[tuple[str, list[ast.arg], list[ast.expr | None]]] = []
        for name, (stmt, default, written_in) in fields.items():
            if not runs or runs[-1][0] != written_in:
                runs.append((written_in, [], []))
            arg = ast.copy_location(ast.arg(arg=name, annotation=stmt.annotation), stmt)
            module_index = index if written_in == module else self._module_index(written_in)
            if module_index is not None:
                # Names in the annotation resolve in the class body, where the field is written.
                module_index.parents[id(arg)] = stmt
            runs[-1][1].append(arg)
            runs[-1][2].append(default)
        return tuple(
            _Params(
                written_in,
                ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[],
                        args=[],
                        vararg=None,
                        kwonlyargs=kwonly,
                        kw_defaults=defaults,
                        kwarg=None,
                        defaults=[],
                    ),
                    body=ast.Constant(None),
                ),
                skip_first=False,
            )
            for written_in, kwonly, defaults in runs
        )

    def _synthetic_fields(self, params: tuple[_Params, ...]) -> dict[str, tuple[ast.AnnAssign, ast.expr | None, str]]:
        out: dict[str, tuple[ast.AnnAssign, ast.expr | None, str]] = {}
        for group in params:
            args = group.fn.args  # type: ignore[attr-defined]
            index = self._module_index(group.module)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
                stmt = index.parents.get(id(arg)) if index is not None else None
                if isinstance(stmt, ast.AnnAssign):
                    out[arg.arg] = (stmt, default, group.module)
        return out

    @staticmethod
    def _returned_values(fn: ast.AST) -> list[ast.expr]:
        """``return <expr>`` values of ``fn``'s own body (not of nested scopes)."""
        out: list[ast.expr] = []
        stack = list(getattr(fn, "body", []))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Return):
                if node.value is not None:
                    out.append(node.value)
                continue
            if isinstance(node, _SCOPE_NODES):
                continue
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt) or isinstance(child, (ast.excepthandler, ast.match_case)):
                    stack.append(child)
        out.sort(key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))
        return out

    def _lambda(self, module: str, node: ast.Lambda) -> _Callable:
        """A lambda is ``<lambda>@L<line>``, or, inside a function, that function
        (FastAPI identifies a closure by the function that made it)."""
        index = self._module_index(module)
        functions = [s for s in self._scopes(index, node) if isinstance(s, _FUNCTION_NODES)] if index else []
        if functions:
            qualname = f"{self._qualname(index, functions[0])}.<locals>.<lambda>"  # type: ignore[arg-type]
        else:
            qualname = f"<lambda>@L{node.lineno}"
        dep = self._target(module, qualname, "lambda", node)
        if "<locals>" not in qualname:
            dep = DepTarget(dep.id, dep.module, dep.qualname, "<lambda>", "lambda", dep.file, dep.line)
        return self._register(dep, (_Params(module, node, skip_first=False),))

    def _target(self, module: str, qualname: str, kind: DepKind, node: ast.AST, *, bound: bool = False) -> DepTarget:
        """``bound``: the callable is an instance of the class ``qualname``, or,
        for a method, that method bound to an instance."""
        qual = _id_qualname(qualname)
        id_qual = _instance_qualname(qualname, member=kind == "method") if bound else qual
        owner, dot, attr = qualname.rpartition(".")
        form = (f"{owner}().{attr}" if kind == "method" and dot else f"{qualname}()") if bound else qualname
        return DepTarget(
            id=f"{module}:{id_qual}",
            module=module,
            qualname=qual,
            name=qual.rsplit(".", 1)[-1],
            kind=kind,
            file=self._file(module),
            line=getattr(node, "lineno", None),
            form=form,
        )

    def _register(self, dep: DepTarget, params: tuple[_Params, ...]) -> _Callable:
        key = _key(dep)
        known = self._callables.get(key)
        if known is not None:
            if known.params != params:
                self._limit(
                    "shared_dependency_id",
                    dep.module,
                    dep.line,
                    f"dependency {dep.id}: two {dep.kind} callables FastAPI reads with different parameters "
                    f"share this id; both are resolved with the parameters of the first one found",
                )
            return known
        callable_ = _Callable(dep, params)
        self._callables[key] = callable_
        return callable_

    def _class_member(self, cls: Def, name: str, seen: set[tuple[str, str]] | None = None) -> _Member | None:
        """``name`` defined in ``cls`` or its nearest in-package base (depth-first, left to right)."""
        seen = set() if seen is None else seen
        key = (cls.module, cls.qualname)
        if key in seen or not isinstance(cls.node, ast.ClassDef):
            return None
        seen.add(key)
        for stmt in cls.node.body:
            if isinstance(stmt, _FUNCTION_NODES) and stmt.name == name:
                return _Member(cls.module, f"{cls.qualname}.{name}", stmt, via_instance=False)
        for base in cls.node.bases:
            for target in _distinct(self._targets(cls.module, base, base, depth=0)):
                if isinstance(target, Def) and isinstance(target.node, ast.ClassDef):
                    found = self._class_member(target, name, seen)
                    if found is not None:
                        return found
        return None

    # ── Name resolution with function and class scopes ───────────────────

    def _targets(self, module: str, expr: ast.expr, ctx: ast.AST, *, depth: int) -> list[_Resolved]:
        if depth > MAX_DEPTH:
            return [Unknown("depth", _text(expr))]
        if isinstance(expr, ast.Name):
            local = self._local_lookup(module, expr.id, ctx)
            if local is not None:
                return local
            return list(self._resolver.resolve_name(module, expr.id, self._at(module, ctx)))
        if isinstance(expr, ast.Attribute):
            bases = _distinct(self._targets(module, expr.value, ctx, depth=depth + 1))
            if len(bases) == 1:
                member = self._attribute(bases[0], expr.attr, depth=depth)
                if member is not None:
                    return member
        return list(self._resolver.resolve_expr(Located(module, expr, self._at(module, ctx))))

    def _attribute(self, base: _Resolved, attr: str, *, depth: int) -> list[_Resolved] | None:
        """Members of classes and instances; module attributes are left to the evaluator."""
        if isinstance(base, External):
            return [External(f"{base.dotted}.{attr}")]
        if isinstance(base, Def) and isinstance(base.node, ast.ClassDef):
            member = self._class_member(base, attr)
            return [member] if member is not None else None
        if isinstance(base, Value) and isinstance(base.loc.node, (ast.Name, ast.Attribute)) and depth <= MAX_DEPTH:
            # `S = Svc` then `S.static`: follow the name to what it binds.
            inner = _distinct(self._targets(base.loc.module, base.loc.node, base.loc.node, depth=depth + 1))
            return self._attribute(inner[0], attr, depth=depth + 1) if len(inner) == 1 else None
        if isinstance(base, Value) and isinstance(base.loc.node, ast.Call) and depth <= MAX_DEPTH:
            call = base.loc.node
            callees = _distinct(self._targets(base.loc.module, call.func, call, depth=depth + 1))
            if len(callees) == 1 and isinstance(callees[0], Def) and isinstance(callees[0].node, ast.ClassDef):
                member = self._class_member(callees[0], attr)
                if member is not None:
                    return [_Member(member.module, member.qualname, member.node, via_instance=True)]
        return None

    def _local_lookup(self, module: str, name: str, ctx: ast.AST) -> list[_Resolved] | None:
        """Bindings of ``name`` in the function or class scopes around ``ctx``; None means module scope."""
        index = self._module_index(module)
        if index is None:
            return None
        crossed_function = False
        for scope in self._scopes(index, ctx):
            if isinstance(scope, ast.ClassDef):
                if crossed_function:
                    continue
                found = self._block_bindings(index, scope.body, name, scope)
                if found:
                    return found
                continue
            crossed_function = True
            if name in self._param_names(scope):
                return [Unknown("parameter", name)]
            if isinstance(scope, ast.Lambda):
                continue
            if self._declares(scope, name, ast.Global):
                return None
            found = self._block_bindings(index, scope.body, name, scope)  # type: ignore[attr-defined]
            if found:
                return found
        return None

    @staticmethod
    def _param_names(scope: ast.AST) -> set[str]:
        args = scope.args  # type: ignore[attr-defined]
        names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        if args.vararg is not None:
            names.add(args.vararg.arg)
        if args.kwarg is not None:
            names.add(args.kwarg.arg)
        return names

    @staticmethod
    def _declares(scope: ast.AST, name: str, kind: type) -> bool:
        stack = list(getattr(scope, "body", []))
        while stack:
            node = stack.pop()
            if isinstance(node, kind) and name in node.names:  # type: ignore[attr-defined]
                return True
            if isinstance(node, _SCOPE_NODES):
                continue
            stack.extend(ast.iter_child_nodes(node))
        return False

    def _block_bindings(self, index: _ModuleIndex, body: list[ast.stmt], name: str, scope: ast.AST) -> list[_Resolved]:
        found: list[_Resolved] = []
        module = index.module.name
        for stmt in self._block_statements(body):
            if isinstance(stmt, (*_FUNCTION_NODES, ast.ClassDef)) and stmt.name == name:
                found.append(Def(module, self._qualname(index, stmt), stmt))
            elif isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
                found.append(Value(Located(module, stmt.value)))
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == name
                and stmt.value is not None
            ):
                found.append(Value(Located(module, stmt.value)))
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.split(".")[0]) == name for alias in stmt.names
            ):
                found.append(Unknown("function-local import", name))
        return _distinct(found)

    @staticmethod
    def _block_statements(body: list[ast.stmt]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        stack = list(reversed(body))
        while stack:
            stmt = stack.pop()
            out.append(stmt)
            if isinstance(stmt, _SCOPE_NODES):
                continue
            nested: list[ast.stmt] = []
            for field in _BLOCK_FIELDS:
                for child in getattr(stmt, field, None) or ():
                    if isinstance(child, ast.stmt):
                        nested.append(child)
                    elif isinstance(child, (ast.excepthandler, ast.match_case)):
                        # pyrefly: ignore[missing-attribute]
                        nested.extend(child.body)
            stack.extend(reversed(nested))
        return out

    def _scopes(self, index: _ModuleIndex, ctx: ast.AST) -> list[ast.AST]:
        """Enclosing scopes of ``ctx``, innermost first.

        A function's own defaults, annotations and decorators run in the scope
        around it, so a function is a scope only for nodes inside its body.
        """
        scopes: list[ast.AST] = []
        child = ctx
        parent = index.parents.get(id(child))
        while parent is not None:
            if isinstance(parent, (*_FUNCTION_NODES, ast.ClassDef)):
                if any(child is stmt for stmt in parent.body):
                    scopes.append(parent)
            elif isinstance(parent, ast.Lambda) and child is parent.body:
                scopes.append(parent)
            child = parent
            parent = index.parents.get(id(child))
        return scopes

    def _at(self, module: str, ctx: ast.AST) -> int | None:
        """Top-level statement index of ``ctx`` when it runs at import, else None (module end).

        Parameter annotations count as module end: the backend runs on Python
        3.14, where annotations are evaluated lazily, when FastAPI inspects the
        signature rather than when the ``def`` runs.
        """
        index = self._module_index(module)
        if index is None:
            return None
        node = ctx
        while True:
            if isinstance(node, ast.arg):
                return None
            parent = index.parents.get(id(node))
            if parent is None:
                return index.top_index.get(id(node))
            if isinstance(parent, (*_FUNCTION_NODES, ast.Lambda)) and any(node is n for n in self._body_of(parent)):
                return None
            node = parent

    @staticmethod
    def _body_of(scope: ast.AST) -> list[ast.AST]:
        body = getattr(scope, "body", [])
        return [body] if isinstance(body, ast.AST) else list(body)

    def _qualname(self, index: _ModuleIndex, node: ast.AST) -> str:
        parts = [getattr(node, "name", "<lambda>")]
        child = node
        parent = index.parents.get(id(child))
        while parent is not None:
            if isinstance(parent, _FUNCTION_NODES):
                parts.append("<locals>")
                parts.append(parent.name)
            elif isinstance(parent, ast.ClassDef):
                parts.append(parent.name)
            child = parent
            parent = index.parents.get(id(child))
        return ".".join(reversed(parts))

    def _module_index(self, module: str) -> _ModuleIndex | None:
        if module in self._index:
            return self._index[module]
        mod = self._sources.module(module)
        index: _ModuleIndex | None = None
        if mod is not None:
            parents: dict[int, ast.AST] = {}
            for node in ast.walk(mod.tree):
                for child in ast.iter_child_nodes(node):
                    parents[id(child)] = node
            top_index = {id(stmt): i for i, stmt in enumerate(mod.tree.body)}
            for stmt in mod.tree.body:
                parents.pop(id(stmt), None)
            index = _ModuleIndex(mod, parents, top_index)
        self._index[module] = index
        return index

    # ── Problems ─────────────────────────────────────────────────────────

    def _file(self, module: str) -> str | None:
        mod = self._sources.module(module)
        return mod.rel if mod is not None else None

    def _limit(self, kind: str, module: str, line: int | None, message: str) -> None:
        file = self._file(module)
        key = (kind, file, line, message)
        if key in self._problem_keys:
            return
        self._problem_keys.add(key)
        where = f"{file}:{line}" if file and line else (file or module)
        self._problems.append(
            problems.limitation(kind, stage=self._stage, file=file, line=line, message=f"{where}: {message}")
        )


def _key(target: DepTarget) -> tuple[str, str, str, str | None, int | None, str]:
    """What FastAPI reads parameters from: one id can name several such callables
    (a redefined function, a local class and its instance, partials)."""
    return (target.id, target.kind, target.variant, target.file, target.line, target.form)


def _read_parameters(params: _Params) -> list[tuple[ast.arg, ast.expr | None]]:
    """``(parameter, default)`` for each parameter FastAPI reads: without a bound
    ``self``/``cls`` or the leading positional ones a partial fills. A parameter
    a partial binds by keyword becomes keyword-only, so positional binding skips it."""
    args = params.fn.args  # type: ignore[attr-defined]
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    pairs = list(zip(positional, defaults, strict=False))[1 if params.skip_first else 0 :]
    if params.bound_positional:
        by_keyword = {b.name for b in params.bound_keywords}
        free = [arg.arg for arg, _ in pairs if arg.arg not in by_keyword]
        filled = set(free[: params.bound_positional])
        pairs = [(arg, default) for arg, default in pairs if arg.arg not in filled]
    return pairs + list(zip(args.kwonlyargs, args.kw_defaults, strict=False))
