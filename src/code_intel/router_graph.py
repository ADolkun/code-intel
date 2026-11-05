"""The FastAPI router graph: one model of the app's routers, includes, mounts and
live routes, shared by the DI and route analyzers.

``build_backend_model`` loads the package (``py_sources``), replays every module
that may touch a router (``router_bases``), and assembles:

- one ``RouterNode`` per router a ``APIRouter(...)``/``FastAPI(...)`` call builds
  (per call chain for routers made inside a function called at import);
- one ``IncludeEdge`` per ``include_router`` whose parent and child resolve;
- the live routes, enumerated from the app root in Starlette's dispatch order,
  each with its path, handler and the dependency layers in FastAPI's order
  (app, then each include and router on the way, then the decorator).

Execution order is global. Importing the app module runs its imports depth
first (a package's ``__init__`` before its submodules), so every statement that
runs on import gets a position, and a function called at import runs at its
call. FastAPI copies a child's routes when ``include_router`` runs, so an
include sees exactly the registrations on the child that ran before it, from
any module. A module nothing imports on import does not run: a change it makes
to another module's router is not counted, and is reported.

Code in a function body called at import (an app or router factory, a helper
like ``setup()``) runs once per call chain, with that call's arguments; a name
local to the function is never read as a module name.

Whatever cannot be resolved becomes a limitation naming the file, line and
expression; lost files are diagnostics (``complete: false``).
"""

from __future__ import annotations

import ast
import re
import time
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from code_intel import include_model as include_models
from code_intel import py_escapes, router_bases
from code_intel.backend_model import (
    NO_INCLUDE_MODEL,
    BackendModel,
    CallSite,
    Def,
    DepLayer,
    Event,
    ExecContext,
    External,
    IncludeCall,
    IncludeEdge,
    IncludeModel,
    LayerVia,
    LiveRoute,
    Located,
    ModuleRef,
    MountCall,
    MountState,
    PyModule,
    RouterCtor,
    RouteReg,
    RouterEscape,
    RouterKind,
    RouterNode,
    Unknown,
    Value,
    unresolved_path_part,
)
from code_intel.problems import Problem, diagnostic, limitation, partition
from code_intel.py_sources import PySources
from code_intel.py_values import LocalFound, LocalLookup, ValueEvaluator, lambda_qualname, opaque_decorator
from code_intel.router_bases import ModuleAnalysis

STAGE = "build router graph"
INF = float("inf")
_MOUNT_RANK: dict[MountState, int] = {"static": 0, "conditional": 1, "deferred": 2}
_UNRESOLVED_PATH_MAX = 60
# External callees that receive the app without adding routes or dependencies.
# FastAPIInstrumentor.instrument_app only adds tracing middleware.
ROUTER_SAFE_EXTERNAL_CALLS = frozenset({"opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app"})
# External callees that only read a router's routes or dependencies list.
LIST_SAFE_EXTERNAL_CALLS = frozenset({"fastapi.openapi.utils.get_openapi"})
ESCAPE_MAX_DEPTH = 5
# How many function calls deep the graph follows bodies run at import, and how
# many call chains it follows in all: each call chain runs its function's body
# again, so fan-out multiplies the work. A deeper chain, a chain past the
# budget, or a recursive call is reported, not followed.
FRAME_MAX_DEPTH = 16
FRAME_BUDGET = 4096
# How many name lookups may nest (an import, a call's result, a parameter): a
# deeper chain is reported instead of exhausting Python's stack.
NAME_MAX_DEPTH = 100
_FASTAPI_CTORS: dict[str, RouterKind] = {
    "fastapi.APIRouter": "APIRouter",
    "fastapi.routing.APIRouter": "APIRouter",
    "fastapi.FastAPI": "FastAPI",
    "fastapi.applications.FastAPI": "FastAPI",
}
# Methods of a router subclass that change what the analysis reads from its
# constructor call and registrations.
_ROUTER_OVERRIDES = frozenset(
    {
        "__init__",
        "__new__",
        "include_router",
        "mount",
        "add_api_route",
        "add_api_websocket_route",
        "api_route",
        "websocket",
        *router_bases.HTTP_METHOD_DECORATORS,
    }
)
# Constructor arguments that add routes the analysis does not read.
_ROUTE_ARGUMENTS = ("routes",)
# Uses of an escaped router inside a callee that cannot change its routes.
_HARMLESS_PARENTS = (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.If, ast.IfExp, ast.FormattedValue, ast.Assert)
_MODELED_USES = frozenset(
    {*router_bases.ROUTE_DECORATORS, *router_bases.REGISTRATION_CALLS, router_bases.INCLUDE_ROUTER, router_bases.MOUNT}
)


def _worst(a: MountState, b: MountState) -> MountState:
    return a if _MOUNT_RANK[a] >= _MOUNT_RANK[b] else b


def _end_pos(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "end_lineno", None) or getattr(node, "lineno", 0), getattr(node, "end_col_offset", 0) or 0)


def _short(qualname: str) -> str:
    return qualname.replace(".<locals>.", ".")


def _enclosing_function(qualname: str) -> str | None:
    """The function whose scope encloses ``qualname``'s (class bodies are skipped)."""
    parts = qualname.split(".")
    for k in range(len(parts) - 1, 0, -1):
        if parts[k] == "<locals>":
            return ".".join(parts[:k])
    return None


def _single_assignments(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    """Names of ``fn``'s own scope bound exactly once, by a ``name = value``
    statement directly in its body (not under a branch or loop)."""
    counts: dict[str, int] = {}
    todo: list[ast.AST] = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] = counts.get(node.name, 0) + 2
            todo.extend(node.decorator_list)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            counts[node.id] = counts.get(node.id, 0) + 1
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            for alias in getattr(node, "names", ()):
                name = alias if isinstance(alias, str) else (alias.asname or alias.name).split(".")[0]
                counts[name] = counts.get(name, 0) + 2
        todo.extend(ast.iter_child_nodes(node))
    out: dict[str, ast.expr] = {}
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            name, value = stmt.target.id, stmt.value
        else:
            continue
        if counts.get(name) == 1:
            out[name] = value
    return out


def _callers_by_callee(analyses: list[tuple[str, ModuleAnalysis]]) -> dict[str, set[str]]:
    """Last names of the functions calling each called name."""
    callers: dict[str, set[str]] = {}
    for _module, analysis in analyses:
        for site in analysis.facts.calls:
            if site.context.deferred_in is not None:
                callers.setdefault(site.name, set()).add(site.context.deferred_in.rsplit(".", 1)[-1])
    return callers


def _passes_parameter(event: Event) -> bool:
    """A parameter handed to another call: recorded so a router passed through
    several functions can be followed. The caller's own escape check already
    follows the callee's use of it, so it is not checked again, and it does not
    make the function one that changes routes."""
    return isinstance(event, RouterEscape) and event.router[0] == "param"


def _module_chain(dotted: str) -> list[str]:
    """``a``, ``a.b``, ``a.b.c`` for ``a.b.c``: importing a module imports its packages first."""
    parts = dotted.split(".")
    return [".".join(parts[: k + 1]) for k in range(len(parts))]


def _flag_escaped(info: _FnInfo, name: str, found: LocalFound | Unknown) -> LocalFound | Unknown:
    """Mark a local container value the function may change in place."""
    if isinstance(found, tuple) and name in info.escaped_locals and py_escapes.mutable_value(found[0].node):
        return (found[0], found[1], True)
    return found


# ── Resolution results ───────────────────────────────────────────────────

# ("node", key) | ("module", name) | ("param", qualname, name) | ("other",)
# | ("local", qualname) | ("external", dotted) | ("unresolved", reason)
_Res = tuple
# (key, event, frame, executed) on a node, in execution order.
_Entry = tuple


@dataclass(eq=False)
class _Frame:
    """A function body as run by one call chain: ``site`` is the call, run in
    ``parent``'s body (None: at module level)."""

    module: str  # module defining the function
    qualname: str
    site: Located
    parent: _Frame | None
    state: MountState  # whether the call runs for sure, maybe, or later or never
    key: tuple  # (id(site call), *parent.key)

    @property
    def call(self) -> ast.Call:
        assert isinstance(self.site.node, ast.Call)
        return self.site.node

    @property
    def executed(self) -> bool:
        return self.state != "deferred"

    @property
    def depth(self) -> int:
        return len(self.key)


@dataclass
class _FnInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    positional: list[str]
    kwonly: list[str]
    vararg: str | None
    kwarg: str | None
    defaults: dict[str, ast.expr]
    local: set[str]
    declared_global: set[str]
    parent: str | None  # enclosing function qualname
    def_at: int | None  # top-level index of a module-level def
    _once: dict[str, ast.expr] | None = None
    _escaped: frozenset[str] | None = None

    @property
    def escaped_locals(self) -> frozenset[str]:
        """Names used in the body where a container they hold may be changed in
        place or handed on (``py_escapes``)."""
        if self._escaped is None:
            parents = {id(c): p for p in ast.walk(self.node) for c in ast.iter_child_nodes(p)}
            self._escaped = frozenset(
                n.id
                for n in ast.walk(self.node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and not py_escapes.reads_only(n, parents)
            )
        return self._escaped

    @property
    def assigned_once(self) -> dict[str, ast.expr]:
        """Local names bound only by one ``name = value`` at the top of the body."""
        if self._once is None:
            self._once = {k: v for k, v in _single_assignments(self.node).items() if k in self.local}
        return self._once

    @property
    def params(self) -> set[str]:
        out = {*self.positional, *self.kwonly}
        out.update(p for p in (self.vararg, self.kwarg) if p)
        return out


@dataclass
class _Node:
    key: tuple  # (id(ctor call), frame key or None)
    ctor: RouterCtor
    site: _Frame | None
    id: str = ""
    module: str = ""
    file: str = ""
    line: int = 0
    prefix: str | Unknown = ""
    dependencies: tuple[Located, ...] | Unknown = ()
    redirect_slashes: bool = True
    events: list[_Entry] = field(default_factory=list)
    included: bool = False


@dataclass
class _Edge:
    parent: _Node
    child: _Node
    event: IncludeCall
    frame: _Frame | None
    prefix: str | Unknown
    dependencies: tuple[Located, ...] | Unknown
    mount: MountState
    file: str
    line: int
    snapshot: tuple  # the include's execution key: it copies child events before it
    edge: IncludeEdge | None = None


# Calls that reach names they are not given literally. With a constant name,
# the attribute calls only read that name, which is counted as a use of it.
_ATTRIBUTE_CALLS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
# Calls that read a namespace: without an argument their own module's, with one
# whatever they are given.
_NAMESPACE_CALLS = frozenset({"vars", "dir", "globals", "locals"})
# Calls that may reach any module's names: evaluated code, module lookups by
# name, member listings, attribute getters.
_DYNAMIC_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "importlib.import_module",
        "importlib.__import__",
        "importlib.reload",
        "pkgutil.resolve_name",
        "inspect.getmembers",
        "inspect.getmembers_static",
        "operator.attrgetter",
        "operator.methodcaller",
        "runpy.run_module",
        "runpy.run_path",
    }
)
# Of those, the ones whose result is a module or object: read with a constant
# attribute right away (``import_module("m").name``), they only reach that name.
_LOOKUP_CALLS = frozenset({"__import__", "importlib.import_module", "importlib.__import__", "pkgutil.resolve_name"})
# Module lookups by name; entry points name their targets in package metadata,
# outside the source.
_DYNAMIC_PREFIXES = (
    "importlib.util.",
    "importlib.machinery.",
    "importlib.metadata.",
    "importlib_metadata.",
    "pkg_resources.",
)
_NAME_PARTS = re.compile(r"[.:]")
# A string annotation is evaluated (typing.get_type_hints, FastAPI reading a
# signature), so a call written in one runs. Longer strings are prose.
_EVALUATED_STRING_MAX = 300
_STRING_PARSE_ERRORS: tuple[type[BaseException], ...] = (SyntaxError, ValueError, RecursionError, MemoryError)


def _evaluated_names(text: str) -> set[str]:
    """Names and attributes a string would read if evaluated, when it holds a
    call (a string annotation such as ``"later()"``)."""
    if "(" not in text or len(text) > _EVALUATED_STRING_MAX:
        return set()
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except _STRING_PARSE_ERRORS:
        return set()
    return {
        n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(tree) if isinstance(n, (ast.Name, ast.Attribute))
    }


@dataclass
class _NameUses:
    """Every way a module-level function object can be reached in the package,
    to prove a function runs only through the calls the analysis follows: its
    name read or an attribute of that name, a string naming it, its module's
    object used as a value, and lookups that read names dynamically."""

    # name -> (file:line, id(call) when the use is that call's callee, else None),
    # for a name read, an attribute of that name, or a string whose last
    # dotted or ``module:name`` part is the name.
    names: dict[str, list[tuple[str, int | None]]]
    route_decorators: set[int]
    # A use that may reach any module's names (getattr with a computed name,
    # sys.modules, import_module(name), inspect.getmembers(...), ...).
    dynamic: list[str]
    # module -> why its namespace is reachable as a whole (its module object
    # used as a value, or read by vars()/dir()/globals()/locals() in it).
    modules: dict[str, str]

    def module_escape(self, module: str) -> str | None:
        """Why ``module``'s names are reachable as a whole: through it or a package holding it."""
        parts = module.split(".")
        for k in range(len(parts), 0, -1):
            found = self.modules.get(".".join(parts[:k]))
            if found is not None:
                return found
        return None

    @classmethod
    def scan(cls, sources: PySources, route_decorators: set[int]) -> _NameUses:
        out = cls({}, route_decorators, [], {})
        for module, mod in sources.modules.items():
            out._scan_module(sources, module, mod)
        return out

    def _scan_module(self, sources: PySources, module: str, mod: PyModule) -> None:
        aliases = self._aliases(sources, module, mod.tree)
        callees: dict[int, int] = {}
        bases: set[int] = set()
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Call):
                callees[id(node.func)] = id(node)
            elif isinstance(node, ast.Attribute):
                bases.add(id(node.value))

        def dotted(expr: ast.AST) -> str | None:
            if isinstance(expr, ast.Name):
                return aliases.get(expr.id, expr.id)
            if isinstance(expr, ast.Attribute):
                base = dotted(expr.value)
                return f"{base}.{expr.attr}" if base is not None else None
            return None

        for node in ast.walk(mod.tree):
            where = f"{mod.rel}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.names.setdefault(node.id, []).append((where, callees.get(id(node))))
            elif isinstance(node, ast.Attribute):
                self.names.setdefault(node.attr, []).append((where, callees.get(id(node))))
                if node.attr == "__dict__":
                    self.dynamic.append(f"__dict__ at {where}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for name in {node.value, _NAME_PARTS.split(node.value)[-1], *_evaluated_names(node.value)}:
                    if name.isidentifier():
                        self.names.setdefault(name, []).append((where, None))
            elif isinstance(node, ast.alias) and node.asname and node.asname != node.name.rsplit(".", 1)[-1]:
                self.names.setdefault(node.name.rsplit(".", 1)[-1], []).append((where, None))
            if isinstance(node, (ast.Name, ast.Attribute)) and not isinstance(node.ctx, ast.Store):
                target = dotted(node)
                if target == "sys.modules":
                    self.dynamic.append(f"sys.modules at {where}")
                elif target is not None and id(node) not in bases and sources.has_module(target):
                    # The module object itself is the value: whatever it reaches, its names are reachable.
                    self.modules.setdefault(target, f"module {target} is used as a value at {where}")
            if isinstance(node, ast.Call):
                self._scan_call(node, dotted(node.func), module, where, id(node) in bases)

    def _scan_call(self, call: ast.Call, called: str | None, module: str, where: str, read_at_once: bool) -> None:
        if called is None:
            return
        called = called.removeprefix("builtins.")
        if called in _ATTRIBUTE_CALLS:
            if not (len(call.args) >= 2 and isinstance(call.args[1], ast.Constant)):
                self.dynamic.append(f"{called}() with a computed name at {where}")
        elif called in _NAMESPACE_CALLS:
            if call.args or call.keywords:
                self.dynamic.append(f"{called}(...) at {where}")
            else:
                self.modules.setdefault(module, f"{called}() at {where}")
        elif called in _DYNAMIC_CALLS or called.startswith(_DYNAMIC_PREFIXES):
            if not (called in _LOOKUP_CALLS and read_at_once):
                self.dynamic.append(f"{called}() at {where}")

    @staticmethod
    def _aliases(sources: PySources, module: str, tree: ast.Module) -> dict[str, str]:
        """Local name -> dotted name it is imported as, anywhere in the module."""
        out: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        out[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".", 1)[0]
                        out[head] = head
            elif isinstance(node, ast.ImportFrom):
                base = sources.resolve_import(module, node.level, node.module) if node.level else node.module
                if base is None:
                    continue
                for alias in node.names:
                    if alias.name != "*":
                        out[alias.asname or alias.name] = f"{base}.{alias.name}"
        return out


class _Builder:
    def __init__(
        self,
        sources: PySources,
        app: str,
        analyses: dict[str, ModuleAnalysis],
        analyze: Callable[[PyModule], ModuleAnalysis],
        *,
        prefilter: bool,
        prefilter_facts: Callable[[PyModule], router_bases.PrefilterFacts] = router_bases.prefilter_facts,
        include_model: IncludeModel = NO_INCLUDE_MODEL,
    ) -> None:
        self._prefilter = prefilter_facts
        self.sources = sources
        self.app_spec = app
        self.ev = ValueEvaluator(sources)
        self._analyses = analyses
        self._analyze = analyze
        self.problems: list[Problem] = []
        self._ctors: dict[int, tuple[str, RouterCtor]] = {}
        self._owners: dict[str, list[tuple[tuple[int, int], tuple[int, int], str]]] = {}
        self._name_cache: dict[tuple[str, str], list[_Res]] = {}
        self._active: set[tuple] = set()
        self.nodes: dict[tuple, _Node] = {}
        self._ids: set[str] = set()
        self._sites: dict[tuple[str, str], dict[tuple, _Frame]] = {}
        self._frames: dict[tuple, _Frame] = {}
        self._refused_sites: dict[tuple, str] = {}
        # Functions called through a call the analysis does not follow (too deep,
        # past the budget, recursive): when their code runs is unknown.
        self._refused_targets: set[tuple[str, str]] = set()
        # id() of the calls refused, which their own limitation reports.
        self._refused_calls: set[int] = set()
        self._unplaced_functions: set[tuple[str, str]] | None = None
        # Why a function may run at import other than through a call the
        # analysis follows (None: it cannot), by (module, qualname).
        self._escapes: dict[tuple[str, str], str | None] = {}
        self._name_uses: _NameUses | None = None
        self.include_model: IncludeModel = include_model
        # Calls past the depth limit or the budget: group -> [count, first call, first callee].
        self._skipped_calls: dict[tuple, list] = {}
        self._frame_keys: dict[tuple, tuple] = {}
        self._fn_infos: dict[tuple[str, str], _FnInfo | None] = {}
        self._lookups: dict[tuple, LocalLookup] = {}
        self._call_ctx: dict[int, ExecContext] = {}
        self._events_by_owner: dict[tuple[str, str | None], list[Event]] = {}
        self._calls_by_owner: dict[tuple[str, str | None], list[CallSite]] = {}
        self._escapes_by_call: dict[int, list[tuple[str, RouterEscape, ast.expr]]] = {}
        self._callable_names: frozenset[str] = frozenset()
        self._order: dict[tuple[str, int], int] = {}
        self._imported: set[str] = set()
        # Module -> (importing module, line) of the import that may not run.
        self._conditional_imports: dict[str, tuple[str, int | None]] = {}
        self._ranks: dict[tuple, float] = {}
        self._escape_memo: dict[tuple, _Effect | None] = {}
        # Recursive calls cut while following escapes; a result reached past a cut is not memoized.
        self._escape_cuts = 0
        self._reported: set[tuple] = set()
        self._event_modules: dict[int, str] = {}
        self.prefilter = prefilter

    # ── Facts ────────────────────────────────────────────────────────────

    def analysis(self, module: str) -> ModuleAnalysis | None:
        found = self._analyses.get(module)
        if found is None:
            mod = self.sources.module(module)
            if mod is None:
                return None
            found = self._analyze(mod)
            self._analyses[module] = found
            self._index(module, found)
        return found

    def _index(self, module: str, analysis: ModuleAnalysis) -> None:
        facts = analysis.facts
        for ctor in facts.ctors:
            self._ctors[id(ctor.node)] = (module, ctor)
        for event in facts.events:
            self._events_by_owner.setdefault((module, event.context.deferred_in), []).append(event)
            self._event_modules[id(event)] = module
            if isinstance(event, RouterEscape) and event.seq in analysis.escape_args:
                self._escapes_by_call.setdefault(id(event.call), []).append(
                    (module, event, analysis.escape_args[event.seq])
                )
        for site in facts.calls:
            self._call_ctx[id(site.call)] = site.context
            self._calls_by_owner.setdefault((module, site.context.deferred_in), []).append(site)

    def load_facts(self) -> None:
        modules = self.sources.modules
        if not self.prefilter:
            for name in modules:
                self.analysis(name)
            return
        features = {name: self._prefilter(mod) for name, mod in modules.items()}
        router_names: frozenset[tuple[str, str]] = frozenset()
        done: set[str] = set()
        while True:
            todo = [name for name in modules if name not in done and features[name].touches(router_names)]
            if not todo:
                break
            for name in todo:
                done.add(name)
                self.analysis(name)
            router_names = frozenset(self._router_names(done))

    def _router_names(self, modules: Iterable[str]) -> set[tuple[str, str]]:
        """(module, name) pairs another module could import to reach a router,
        or to call a function that changes routes when it runs."""
        out: set[tuple[str, str]] = set()
        analyses = [(m, self._analyses[m]) for m in modules if m in self._analyses]
        for module, analysis in analyses:
            facts = analysis.facts
            for name, verdict in facts.module_end.items():
                if verdict[0] in ("router", "router_rebound", "import", "call", "module"):
                    out.add((module, name))
            for qualname, verdict in facts.function_returns.items():
                if verdict[0] not in ("other",):
                    out.add((module, qualname.split(".")[0]))
        names = self._route_changing_functions(analyses)
        for module, analysis in analyses:
            for qualname in analysis.qualnames.values():
                if qualname.rsplit(".", 1)[-1] in names:
                    out.add((module, qualname.split(".")[0]))
        return out

    @staticmethod
    def _route_changing_functions(analyses: list[tuple[str, ModuleAnalysis]]) -> set[str]:
        """Names of functions whose body changes routes, or calls a function of
        such a name, matched by the callee's last name. The match only picks
        which calls are worth resolving; each picked call is then resolved to
        its exact definition before its body runs, so a same-named function
        elsewhere costs a lookup, never a wrong frame."""
        names = {
            event.context.deferred_in.rsplit(".", 1)[-1]
            for _module, analysis in analyses
            for event in analysis.facts.events
            if event.context.deferred_in is not None and not _passes_parameter(event)
        }
        callers = _callers_by_callee(analyses)
        todo = list(names)
        while todo:
            for caller in callers.get(todo.pop(), ()):
                if caller not in names:
                    names.add(caller)
                    todo.append(caller)
        return names

    # ── Owners, functions and positions ──────────────────────────────────

    def _owner(self, module: str, node: ast.AST) -> str | None:
        """Qualname of the innermost function whose body holds ``node``."""
        spans = self._owners.get(module)
        if spans is None:
            spans = []
            analysis = self.analysis(module)
            for qualname, fn in (analysis.functions if analysis is not None else {}).items():
                if fn.body:
                    start = (fn.body[0].lineno, fn.body[0].col_offset)
                    end = (fn.end_lineno or fn.lineno, fn.end_col_offset or 0)
                    spans.append((start, end, qualname))
            spans.sort(key=lambda s: (s[0], (-s[1][0], -s[1][1])))
            self._owners[module] = spans
        pos = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        best: str | None = None
        for start, end, qualname in spans:
            if start > pos:
                break
            if pos <= end:
                best = qualname
        return best

    def _at(self, module: str, node: ast.AST, ctx: ExecContext | None = None) -> int | None:
        if ctx is not None and ctx.deferred_in is not None:
            return None
        if self.sources.module(module) is None:
            return None
        return self.sources.scope(module).index_of(node)

    def _fn_info(self, module: str, qualname: str) -> _FnInfo | None:
        key = (module, qualname)
        if key in self._fn_infos:
            return self._fn_infos[key]
        analysis = self.analysis(module)
        fn = analysis.functions.get(qualname) if analysis is not None else None
        info: _FnInfo | None = None
        if fn is not None:
            args = fn.args
            positional = [a.arg for a in (*args.posonlyargs, *args.args)]
            kwonly = [a.arg for a in args.kwonlyargs]
            defaults = dict(zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=False))
            defaults.update(
                {a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=False) if d is not None}
            )
            scope = analysis.scopes.get(qualname) if analysis is not None else None
            if scope is not None:
                bound, declared_global, declared_nonlocal = scope.local, scope.declared_global, scope.declared_nonlocal
            else:
                found = router_bases.scope_names(fn.body)
                bound, declared_global, declared_nonlocal = (
                    frozenset(found[0]),
                    frozenset(found[1]),
                    frozenset(found[2]),
                )
            params = {*positional, *kwonly, *(a.arg for a in (args.vararg, args.kwarg) if a is not None)}
            parent = _enclosing_function(qualname)
            info = _FnInfo(
                node=fn,
                positional=positional,
                kwonly=kwonly,
                vararg=args.vararg.arg if args.vararg else None,
                kwarg=args.kwarg.arg if args.kwarg else None,
                defaults=defaults,
                local=set(bound - declared_nonlocal - params),
                declared_global=set(declared_global),
                parent=parent,
                def_at=self._at(module, fn) if parent is None else None,
            )
        self._fn_infos[key] = info
        return info

    def lookup(self, module: str, qualname: str | None, frame: _Frame | None) -> LocalLookup | None:
        """The local-name lookup of function ``qualname``; None for module-level code."""
        return None if qualname is None else self._function_lookup(module, qualname, frame)

    def _function_lookup(self, module: str, qualname: str, frame: _Frame | None) -> LocalLookup:
        """Names local to function ``qualname`` of ``module``, as run by ``frame``
        (when it runs that function): a parameter is the call's argument (or its
        default), any other local name is unknown."""
        if frame is not None and (frame.module, frame.qualname) != (module, qualname):
            frame = None
        key = (module, qualname, frame.key if frame else None)
        found = self._lookups.get(key)
        if found is not None:
            return found

        def local(name: str) -> LocalFound | Unknown | None:
            info = self._fn_info(module, qualname)
            if info is None or name in info.declared_global:
                return None
            label = f"{_short(qualname)}()"
            if name in info.params:
                return _flag_escaped(info, name, self._parameter_value(module, info, name, frame, label))
            if name in info.assigned_once:
                # A statement at the top of the body runs once, before any use that runs.
                found = Located(module, info.assigned_once[name], None), self.lookup(module, qualname, frame)
                return _flag_escaped(info, name, found)
            if name in info.local:
                return Unknown(
                    f"local variable of {label}, assigned more than once or not at the top of its body", name
                )
            if info.parent is not None:
                return self._function_lookup(module, info.parent, None)(name)
            return None

        self._lookups[key] = local
        return local

    def _parameter_value(
        self, module: str, info: _FnInfo, name: str, frame: _Frame | None, label: str
    ) -> tuple[Located, LocalLookup | None] | Unknown:
        """A parameter: the argument ``frame``'s call passes, else its default."""
        if frame is not None:
            passed = self._argument(frame, info, name)
            if passed is not None:
                return passed
            if name in info.defaults:
                outer = self.lookup(module, info.parent, None) if info.parent else None
                return Located(module, info.defaults[name], info.def_at), outer
        return Unknown(f"parameter of {label}, whose argument is not known here", name)

    def _site_lookup(self, frame: _Frame) -> LocalLookup | None:
        """The lookup for code at ``frame``'s call site."""
        site = frame.site
        owner = self._owner(site.module, site.node)
        if owner is None:
            return None
        parent = frame.parent if frame.parent is not None and frame.parent.qualname == owner else None
        return self.lookup(site.module, owner, parent)

    def _argument(self, frame: _Frame, info: _FnInfo, name: str) -> tuple[Located, LocalLookup | None] | Unknown | None:
        """What ``frame``'s call passes for parameter ``name``; None when it passes
        nothing (the default applies)."""
        call = frame.site.node
        assert isinstance(call, ast.Call)
        site_module, site_at = frame.site.module, frame.site.at
        site_lookup = self._site_lookup(frame)
        if name in (info.vararg, info.kwarg):
            return Unknown(f"*args or **kwargs parameter of {_short(frame.qualname)}()", name)
        for k, arg in enumerate(call.args):
            if isinstance(arg, ast.Starred):
                return Unknown("argument passed through *args", name)
            if k < len(info.positional) and info.positional[k] == name:
                return Located(site_module, arg, site_at), site_lookup
        for kw in call.keywords:
            if kw.arg == name:
                return Located(site_module, kw.value, site_at), site_lookup
        for kw in call.keywords:
            if kw.arg is None:
                mapping = self.ev.mapping_items(Located(site_module, kw.value, site_at), site_lookup)
                if isinstance(mapping, Unknown):
                    return mapping
                if name in mapping:
                    return mapping[name]
        return None

    def _extra_keywords(self, frame: _Frame, info: _FnInfo) -> dict[str, tuple[Located, LocalLookup | None]] | Unknown:
        """The keywords ``frame``'s call passes into the ``**kwargs`` parameter."""
        call = frame.site.node
        assert isinstance(call, ast.Call)
        site_lookup = self._site_lookup(frame)
        named = {*info.positional, *info.kwonly}
        out: dict[str, tuple[Located, LocalLookup | None]] = {}
        for kw in call.keywords:
            if kw.arg is None:
                mapping = self.ev.mapping_items(Located(frame.site.module, kw.value, frame.site.at), site_lookup)
                if isinstance(mapping, Unknown):
                    return mapping
                out.update({k: v for k, v in mapping.items() if k not in named})
            elif kw.arg not in named:
                out[kw.arg] = (Located(frame.site.module, kw.value, frame.site.at), site_lookup)
        return out

    # ── Import order ─────────────────────────────────────────────────────

    def import_order(self, app_module: str) -> None:
        """Run the imports of ``app_module`` depth first: every top-level
        statement of every module imported on import gets its position.

        A module runs at its first import. When that import may be skipped
        (under a branch, loop or handler), or sits in a module that itself may
        not run there, the module may run at that point, later, or never: what
        it changes is conditional (``_conditional_imports``)."""
        counter = 0
        started: set[str] = set()
        self._conditional_imports = {}
        # Each frame: [module or None, statement index, pending (target, may be skipped, line), statement count,
        # imports by index]
        stack: list[list] = [[None, 0, [(t, False, None) for t in _module_chain(app_module)], 0, {}]]
        while stack:
            top = stack[-1]
            if top[2]:
                target, skippable, line = top[2].pop(0)
                mod = self.sources.module(target)
                if mod is not None and target not in started:
                    started.add(target)
                    self._first_import(top[0], target, skippable, line)
                    stack.append([target, 0, [], len(mod.tree.body), self._imports_by_index(target)])
                continue
            module = top[0]
            if module is None or top[1] >= top[3]:
                stack.pop()
                continue
            index = top[1]
            if index in top[4]:
                top[2] = top[4].pop(index)
                continue
            self._order[(module, index)] = counter
            counter += 1
            top[1] += 1
        self._imported = started

    def _first_import(self, importer: str | None, module: str, skippable: bool, line: int | None) -> None:
        """Record whether ``module``'s first import may not run there."""
        if importer is None:
            return
        if importer in self._conditional_imports:
            self._conditional_imports[module] = self._conditional_imports[importer]
        elif skippable:
            self._conditional_imports[module] = (importer, line)

    def _imports_by_index(self, module: str) -> dict[int, list[tuple[str, bool, int]]]:
        """``(module imported, may be skipped, import line)`` by top-level
        statement index, each dotted import preceded by its parent packages."""
        by_index: dict[int, list[tuple[str, bool, int]]] = {}
        for index, targets, conditional, line in self.sources.scope(module).import_targets():
            by_index.setdefault(index, []).extend(
                (t, conditional, line) for dotted in targets for t in _module_chain(dotted)
            )
        return by_index

    def _level(self, module: str, at: int | None) -> tuple:
        """Execution key of top-level statement ``at`` of ``module``."""
        if at is not None and (module, at) in self._order:
            return (self._order[(module, at)],)
        return (INF, 0, module, at if at is not None else INF)

    @staticmethod
    def _effect_pos(event: Event) -> tuple[int, int]:
        """Where an event takes effect: a decorator when the ``def`` runs, a call when it returns."""
        if isinstance(event, RouteReg) and event.decorator and isinstance(event.endpoint, Located):
            node = event.endpoint.node
            return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        return _end_pos(event.call)

    def _rank(self, module: str, owner: str | None, call: ast.AST) -> float:
        """Where a call runs among the events of its function (or module level):
        just after the last event that took effect before the call returned."""
        key = (module, owner, id(call))
        found = self._ranks.get(key)
        if found is None:
            end = _end_pos(call)
            seqs = [
                e.seq
                for e in self._events_by_owner.get((module, owner), ())
                if not e.context.lazy and self._effect_pos(e) <= end
            ]
            found = max(seqs, default=0) + 0.5
            self._ranks[key] = found
        return found

    def _frame_key(self, frame: _Frame) -> tuple:
        found = self._frame_keys.get(frame.key)
        if found is None:
            site = frame.site
            if frame.parent is None:
                base = self._level(site.module, self._at(site.module, site.node))
                rank = self._rank(site.module, None, site.node)
            else:
                base = self._frame_key(frame.parent)
                rank = self._rank(site.module, frame.parent.qualname, site.node)
            found = (*base, rank)
            self._frame_keys[frame.key] = found
        return found

    def key(self, module: str, event: Event, frame: _Frame | None, executed: bool) -> tuple:
        """Execution key of an event: keys compare in the order the events run."""
        if not executed or event.context.lazy:
            return (INF, 1, module, event.seq)
        if frame is None:
            return (*self._level(module, self._at(module, event.call)), event.seq)
        return (*self._frame_key(frame), event.seq)

    @staticmethod
    def ran_before(key: tuple, upto: tuple) -> bool:
        """Whether an event at ``key`` ran before an include at ``upto``. Events
        that may run later or never are listed (as deferred) either way, unless
        the emitter places them after the include (``_Emitter._copied_before``); an event whose order
        the analysis cannot place (a module nothing imports on import) counts as
        earlier, as a child module finishes importing before another module can
        include its router."""
        if key[0] == INF and key[1] == 1:
            return True
        comparable = (key[0] != INF and upto[0] != INF) or (
            key[0] == INF and upto[0] == INF and key[1] == 0 and upto[1] == 0 and key[2] == upto[2]
        )
        return key < upto if comparable else True

    @staticmethod
    def runs_at_import(key: tuple) -> bool:
        """Whether an event or include at ``key`` runs at import (when it runs at all)."""
        return not (key[0] == INF and key[1] == 1)

    def runs_after_import(self, module: str, event: Event, frame: _Frame | None) -> str | None:
        """The function a deferred event runs in, when that function can only run
        once the import is over: it, and every function of the call chain running
        it, is never used at import except through calls the analysis follows
        (``escape``), no unfollowed call reaches it, and the event is not in lazy
        code. None when the event may run at import at a place the analysis does
        not know."""
        if event.context.lazy:
            return None
        unplaced = self._unplaced()
        if frame is None:
            owner = event.context.deferred_in
            if owner is None or (module, owner) in unplaced or self.escape(module, owner) is not None:
                return None
            return owner
        root = frame
        while True:
            ctx = self._call_ctx.get(id(root.site.node))
            if ctx is None or ctx.lazy or self.escape(root.module, root.qualname) is not None:
                return None
            if root.parent is None:
                break
            root = root.parent
        owner = self._owner(root.site.module, root.site.node)
        if owner is None or (root.site.module, owner) in unplaced or self.escape(root.site.module, owner) is not None:
            return None
        return owner

    def escape(self, module: str, qualname: str) -> str | None:
        """How function ``qualname`` of ``module`` may run without a call the
        analysis follows, or None when it cannot. Only a module-level function
        whose every use in the package is the callee of a call the analysis
        resolved to it, decorated only by route decorators or an app's or
        router's ``on_event`` (run after import), qualifies; any other use (an
        alias, an argument, a container, a string naming it, an aliased import, a
        name read dynamically) may run it at import."""
        key = (module, qualname)
        if key in self._escapes:
            return self._escapes[key]
        found = self._find_escape(module, qualname)
        self._escapes[key] = found
        return found

    def _find_escape(self, module: str, qualname: str) -> str | None:
        if "." in qualname:
            return f"{_short(qualname)}() is not a module-level function, so other code may hold and call it"
        mod = self.sources.module(module)
        defs = [
            stmt
            for stmt in (mod.tree.body if mod is not None else ())
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == qualname
        ]
        if len(defs) != 1:
            return f"{qualname}() is not defined exactly once at module level"
        uses = self._uses()
        for dec in defs[0].decorator_list:
            if id(dec) not in uses.route_decorators and not self._is_on_event(module, dec):
                return f"@{self.sources.text(module, dec, 60)} receives {qualname}() and may call it"
        if uses.dynamic:
            return f"the package reads names dynamically ({uses.dynamic[0]})"
        whole = uses.module_escape(module)
        if whole is not None:
            return f"every name of {module} is reachable ({whole})"
        resolved = self._resolved_calls()
        for where, callee in uses.names.get(qualname, ()):
            if callee is None or (module, qualname) not in resolved.get(callee, ()):
                return f"{qualname} is used at {where} other than as a call the analysis follows"
        return None

    def direct_use(self, module: str, qualname: str) -> str | None:
        """A use of module-level function ``qualname`` itself other than a call
        the analysis follows: a decorator that receives it, or its name read
        another way (an alias, an argument, a container, a string naming it).
        Unlike ``escape``, package-wide lookups are not counted: this asks
        whether the function may run more often than the followed calls say."""
        if "." in qualname:
            return None
        mod = self.sources.module(module)
        defs = [
            stmt
            for stmt in (mod.tree.body if mod is not None else ())
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == qualname
        ]
        if len(defs) != 1:
            return None
        uses = self._uses()
        for dec in defs[0].decorator_list:
            if id(dec) not in uses.route_decorators and not self._is_on_event(module, dec):
                return f"@{self.sources.text(module, dec, 60)} receives {qualname}() and may call it"
        resolved = self._resolved_calls()
        for where, callee in uses.names.get(qualname, ()):
            if callee in self._refused_calls:
                continue
            if callee is None or (module, qualname) not in resolved.get(callee, ()):
                return f"{qualname} is used at {where} other than as a call the analysis follows"
        return None

    def _is_on_event(self, module: str, dec: ast.expr) -> bool:
        """``@app.on_event(...)`` on an app or router: FastAPI runs the function
        at startup or shutdown, after the import."""
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "on_event"):
            return False
        base = dec.func.value
        if not isinstance(base, ast.Name):
            return False
        at = self.sources.scope(module).index_of(dec)
        targets = self.ev.resolve_expr(Located(module, base, at))
        if len(targets) != 1 or not isinstance(targets[0], Value):
            return False
        ctor = targets[0].loc
        if not isinstance(ctor.node, ast.Call):
            return False
        kinds = self.ev.resolve_expr(Located(ctor.module, ctor.node.func, ctor.at))
        return len(kinds) == 1 and isinstance(kinds[0], External) and kinds[0].dotted in _FASTAPI_CTORS

    def _resolved_calls(self) -> dict[int, set[tuple[str, str]]]:
        """id(call) -> the functions the analysis runs from that call."""
        out: dict[int, set[tuple[str, str]]] = {}
        for frame in self._frames.values():
            out.setdefault(id(frame.site.node), set()).add((frame.module, frame.qualname))
        return out

    def _uses(self) -> _NameUses:
        if self._name_uses is None:
            route_decorators = {
                id(event.call)
                for analysis in self._analyses.values()
                for event in analysis.facts.events
                if isinstance(event, RouteReg) and event.decorator
            }
            self._name_uses = _NameUses.scan(self.sources, route_decorators)
        return self._name_uses

    def _unplaced(self) -> set[tuple[str, str]]:
        """Functions a call the analysis does not follow may run, directly or
        through the functions they call: they may run at import, at a place unknown."""
        if self._unplaced_functions is None:
            found = set(self._refused_targets)
            calls: list[tuple[tuple[str, str], tuple[str, str]]] = []
            for frame in self._frames.values():
                site = frame.site
                caller = self._owner(site.module, site.node) if frame.parent is None else frame.parent.qualname
                caller_module = site.module if frame.parent is None else frame.parent.module
                if caller is not None:
                    calls.append(((caller_module, caller), (frame.module, frame.qualname)))
            changed = True
            while changed:
                changed = False
                for caller, callee in calls:
                    if caller in found and callee not in found:
                        found.add(callee)
                        changed = True
            self._unplaced_functions = found
        return self._unplaced_functions

    # ── Resolution ───────────────────────────────────────────────────────

    def resolve(self, ref, module: str, frame: _Frame | None) -> list[_Res]:
        if isinstance(ref, Unknown):
            return [("unresolved", f"{ref.text} ({ref.reason})")]
        kind = ref[0]
        if kind in ("router", "router_rebound"):
            return [self._ctor_res(module, ref[1], frame)]
        if kind == "import":
            source, dotted = ref[1], ref[2]
            if source is None:
                return [("unresolved", "relative import above the package")]
            parts = dotted.split(".")
            if source == module:
                # `from . import sub` in a package's own __init__ imports the submodule.
                submodule = f"{module}.{parts[0]}"
                results = [("module", submodule) if self.sources.has_module(submodule) else ("other",)]
            else:
                results = self._name(source, parts[0])
            for attr in parts[1:]:
                results = [r for res in results for r in self._attribute(res, attr)]
            return results
        if kind == "module":
            return [("module", ref[1])]
        if kind == "call":
            return self._call(ref[1], frame)
        if kind == "param":
            return self._param(module, ref[1], ref[2], frame)
        if kind in ("other", "unreachable"):
            return [("other",)]
        return [("unresolved", "")]

    def _param(self, module: str, qualname: str, name: str, frame: _Frame | None) -> list[_Res]:
        """A function's parameter, as the call running it passes a router."""
        if frame is None or (frame.module, frame.qualname) != (module, qualname):
            return [("param", qualname, name)]
        info = self._fn_info(module, qualname)
        if info is None:
            return [("param", qualname, name)]
        passed = self._argument(frame, info, name)
        if not isinstance(passed, tuple):
            return [("param", qualname, name)]
        arg = passed[0].node
        for escape_module, escape, carried in self._escapes_by_call.get(id(frame.site.node), ()):
            value = carried.value if isinstance(carried, ast.Starred) else carried
            if value is arg:
                return self.resolve(escape.router, escape_module, frame.parent)
        return [("param", qualname, name)]

    def _ctor_res(self, module: str, call: ast.Call, frame: _Frame | None) -> _Res:
        entry = self._ctors.get(id(call))
        if entry is None:
            return ("unresolved", "router call not recorded")
        owner = self._owner(entry[0], call)
        if owner is None:
            return ("node", (id(call), None))
        if frame is not None and frame.module == entry[0] and frame.qualname == owner:
            return ("node", (id(call), frame.key))
        return ("local", owner)

    def _name(self, module: str, name: str) -> list[_Res]:
        key = (module, name)
        cached = self._name_cache.get(key)
        if cached is not None:
            return cached
        hops = self._reexports(module, name)
        module, name = hops[-1]
        key = (module, name)
        cached = self._name_cache.get(key)
        if cached is not None:
            result = cached
        elif key in self._active:
            result = [("unresolved", f"import cycle at {module}.{name}")]
        elif len(self._active) >= NAME_MAX_DEPTH:
            result = [("unresolved", f"{module}.{name} is reached through more than {NAME_MAX_DEPTH} nested lookups")]
        else:
            self._active.add(key)
            try:
                result = self._name_uncached(module, name)
            finally:
                self._active.discard(key)
        for hop in hops:
            if hop not in self._active:
                self._name_cache[hop] = result
        return result

    def _reexports(self, module: str, name: str) -> list[tuple[str, str]]:
        """``(module, name)`` hops through plain ``from x import name`` re-exports,
        followed in a loop so a long chain of them does not nest calls; the last
        hop is the first one that is not a plain re-export."""
        hops = [(module, name)]
        seen = {(module, name)}
        while True:
            analysis = self._analyses.get(module)
            verdict = analysis.facts.module_end.get(name) if analysis is not None else None
            if verdict is None or verdict[0] != "import" or verdict[1] is None or verdict[1] == module:
                return hops
            source, dotted = verdict[1], verdict[2]
            if "." in dotted or (source, dotted) in seen or (source, dotted) in self._active:
                return hops
            module, name = source, dotted
            seen.add((module, name))
            hops.append((module, name))

    def _name_uncached(self, module: str, name: str) -> list[_Res]:
        # A module the prefilter skipped binds no router of its own; the evaluator
        # follows its imports and re-exports.
        analysis = self._analyses.get(module)
        if analysis is not None and name in analysis.facts.module_end:
            return self.resolve(analysis.facts.module_end[name], module, None)
        if self.sources.module(module) is not None:
            return self._targets(self.ev.resolve_name(module, name, None))
        if self.sources.has_module(f"{module}.{name}"):
            return [("module", f"{module}.{name}")]
        if self.sources.in_package(module):
            return [("unresolved", f"{module}.{name} is not defined")]
        return [("external", f"{module}.{name}")]

    def _targets(self, targets) -> list[_Res]:
        out: list[_Res] = []
        for target in targets:
            if isinstance(target, Value):
                node = target.loc.node
                if isinstance(node, ast.Call):
                    if id(node) in self._ctors:
                        out.append(self._ctor_res(target.loc.module, node, None))
                    else:
                        out.extend(self._call(target.loc, None))
                elif isinstance(node, (ast.Constant, ast.JoinedStr, ast.List, ast.Tuple, ast.Dict, ast.Set)):
                    out.append(("other",))
                else:
                    out.append(("unresolved", self.sources.text(target.loc.module, node, 60)))
            elif isinstance(target, ModuleRef):
                out.append(("module", target.name))
            elif isinstance(target, Def):
                out.append(("other",))
            elif isinstance(target, External):
                out.append(("external", target.dotted))
            else:
                out.append(("unresolved", f"{target.text} ({target.reason})"))
        return out

    def _attribute(self, res: _Res, attr: str) -> list[_Res]:
        if res[0] == "node":
            node = self.node(res[1])
            if attr == "router" and node is not None and node.ctor.kind == "FastAPI":
                return [res]
            return [("other",)]
        if res[0] == "module":
            return self._name(res[1], attr)
        if res[0] == "external":
            return [("external", f"{res[1]}.{attr}")]
        if res[0] in ("other", "local"):
            return [("other",)]
        return [res]

    def _call(self, loc: Located, frame: _Frame | None) -> list[_Res]:
        call = loc.node
        assert isinstance(call, ast.Call)
        owner = self._owner(loc.module, call)
        lookup = self.lookup(loc.module, owner, frame)
        local = self.ev.local_value(call.func, lookup)
        if local is not None:
            text = self.sources.text(loc.module, call.func, 60)
            reason = local.reason if isinstance(local, Unknown) else f"local to {_short(owner or '')}()"
            return [("unresolved", f"{text}: {reason}")]
        out: list[_Res] = []
        for target in self.ev.resolve_expr(Located(loc.module, call.func, loc.at)):
            out.extend(self._call_target(target, loc, frame, owner))
        return out

    def _call_target(self, target, loc: Located, frame: _Frame | None, owner: str | None) -> list[_Res]:
        """What calling one resolved callee gives: what a package function
        returns (its body run by this call), or the router a router class builds."""
        call = loc.node
        assert isinstance(call, ast.Call)
        if isinstance(target, Def) and isinstance(target.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            site = self._site(target, loc, frame, owner)
            if isinstance(site, str):
                return [("unresolved", site)]
            analysis = self.analysis(target.module)
            returned = analysis.facts.function_returns.get(target.qualname) if analysis else None
            return self.resolve(returned, target.module, site) if returned is not None else [("other",)]
        kind: RouterKind | None = None
        if isinstance(target, Def) and isinstance(target.node, ast.ClassDef):
            kind = self._router_class_kind(target)
            if kind is None:
                return [("other",)]
        elif isinstance(target, External) and target.dotted in _FASTAPI_CTORS:
            kind = _FASTAPI_CTORS[target.dotted]
        elif isinstance(target, External):
            return [("external", f"{target.dotted}()")]
        elif isinstance(target, Unknown):
            return [("unresolved", f"{target.text} ({target.reason})")]
        else:
            return [("unresolved", self.sources.text(loc.module, call, 60))]
        if id(call) not in self._ctors:
            self._ctors[id(call)] = (loc.module, RouterCtor(loc.module, call, kind, None, 0))
        return [self._ctor_res(loc.module, call, frame)]

    def _site(self, target: Def, loc: Located, frame: _Frame | None, owner: str | None) -> _Frame | str:
        """The frame for running ``target``'s body from call ``loc`` (made once),
        or why the analysis does not run it (reported as a limitation)."""
        call = loc.node
        parent = frame if frame is not None and owner == frame.qualname else None
        key = (id(call), *(parent.key if parent else ()))
        found = self._frames.get(key)
        if found is not None:
            return found
        refused = self._refused_sites.get(key)
        if refused is not None:
            return refused
        refusal = self._site_refusal(target, loc, parent, key)
        if refusal is not None:
            self._refused_sites[key] = refusal[1]
            self._refused_targets.add((target.module, target.qualname))
            self._refused_calls.add(id(call))
            self._unplaced_functions = None
            self._escapes = {}
            self._refuse(*refusal, target, loc, owner)
            return refusal[1]
        state = self._site_state(call, owner, parent)
        site = _Frame(target.module, target.qualname, loc, parent, state, key)
        self._frames[key] = site
        self._unplaced_functions = None
        self._escapes = {}
        self._sites.setdefault((target.module, target.qualname), {})[key] = site
        return site

    def _refuse(self, kind: str, reason: str, target: Def, loc: Located, owner: str | None) -> None:
        if kind != "recursive":
            # Counted, and reported once per calling function (depth) or once (budget).
            group = (kind, loc.module, owner) if kind == "depth" else (kind,)
            entry = self._skipped_calls.setdefault(group, [0, loc, target.qualname])
            entry[0] += 1
            return
        mod = self.sources.module(loc.module)
        self._limit(
            "recursive_router_call",
            mod.rel if mod else loc.module,
            getattr(loc.node, "lineno", None),
            f"{self.sources.text(loc.module, loc.node, 60)}: {reason}; the routes, includes and mounts "
            f"{target.qualname}() makes from this call are not in the route list",
        )

    def _site_state(self, call: ast.AST, owner: str | None, parent: _Frame | None) -> MountState:
        ctx = self._call_ctx.get(id(call))
        if owner is not None and parent is None:
            state: MountState = "deferred"  # the call sits in a function nothing runs at import
        elif ctx is not None and ctx.lazy:
            state = "deferred"
        elif ctx is not None and ctx.conditional:
            state = "conditional"
        else:
            state = "static"
        return _worst(state, parent.state) if parent is not None else state

    def _site_refusal(self, target: Def, loc: Located, parent: _Frame | None, key: tuple) -> tuple[str, str] | None:
        """``(kind, reason)`` when the analysis does not run ``target``'s body from
        call ``loc``: the call is recursive, too deep, or past the call chain budget."""
        chain = parent
        while chain is not None:
            if (chain.module, chain.qualname) == (target.module, target.qualname):
                return "recursive", f"recursive call: {target.qualname}() is already running in this call chain"
            chain = chain.parent
        if loc.module == target.module and self._owner(loc.module, loc.node) == target.qualname:
            return "recursive", f"recursive call: {target.qualname}() calls itself"
        if len(key) > FRAME_MAX_DEPTH:
            return "depth", f"{target.qualname}() is called more than {FRAME_MAX_DEPTH} function calls deep"
        if len(self._frames) >= FRAME_BUDGET:
            return "budget", f"the analysis follows at most {FRAME_BUDGET} call chains run at import"
        return None

    def _report_skipped_calls(self) -> None:
        """One ``router_call_limit`` per calling function past the depth limit,
        and one for every call past the call chain budget."""
        for group, (count, loc, callee) in self._skipped_calls.items():
            mod = self.sources.module(loc.module)
            where = f"{mod.rel if mod else loc.module}:{getattr(loc.node, 'lineno', '?')}"
            if group[0] == "depth":
                caller = f"{_short(group[2])}()" if group[2] else "module-level code"
                what = (
                    f"{count} call(s) in {caller} are more than {FRAME_MAX_DEPTH} function calls deep "
                    f"(the first calls {callee}() at {where})"
                )
            else:
                what = (
                    f"{count} call(s) are past the {FRAME_BUDGET} call chains the analysis follows "
                    f"(the first calls {callee}() at {where})"
                )
            self._limit(
                "router_call_limit",
                mod.rel if mod else loc.module,
                getattr(loc.node, "lineno", None),
                f"{what}; the routes, includes and mounts made from them are not in the route list",
            )

    def _router_class_kind(self, cls: Def, seen: frozenset[int] = frozenset()) -> RouterKind | None:
        assert isinstance(cls.node, ast.ClassDef)
        if id(cls.node) in seen:
            return None
        for base in cls.node.bases:
            for target in self.ev.resolve_expr(Located(cls.module, base, None)):
                if isinstance(target, External) and target.dotted in _FASTAPI_CTORS:
                    return _FASTAPI_CTORS[target.dotted]
                if isinstance(target, Def) and isinstance(target.node, ast.ClassDef):
                    kind = self._router_class_kind(target, seen | {id(cls.node)})
                    if kind is not None:
                        return kind
        return None

    def _router_class_overrides(self, module: str, call: ast.Call) -> tuple[str, list[str]] | None:
        """``(class qualname, overridden methods)`` when ``call`` builds a router
        through a package subclass that overrides how FastAPI builds or fills it."""
        found: list[str] = []
        name: str | None = None
        todo = [t for t in self.ev.resolve_expr(Located(module, call.func, None)) if isinstance(t, Def)]
        seen: set[int] = set()
        while todo:
            cls = todo.pop()
            if not isinstance(cls.node, ast.ClassDef) or id(cls.node) in seen:
                continue
            seen.add(id(cls.node))
            name = name or cls.qualname
            for stmt in cls.node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in _ROUTER_OVERRIDES:
                    found.append(f"{cls.qualname}.{stmt.name}")
            for base in cls.node.bases:
                todo.extend(t for t in self.ev.resolve_expr(Located(cls.module, base, None)) if isinstance(t, Def))
        return (name, found) if name is not None and found else None

    def nodes_of(self, results: list[_Res]) -> tuple[list[_Node], list[_Res]]:
        """Nodes among ``results``; the rest are returned as is."""
        nodes: list[_Node] = []
        rest: list[_Res] = []
        for res in results:
            node = self.node(res[1]) if res[0] == "node" else None
            if node is not None:
                if all(n is not node for n in nodes):
                    nodes.append(node)
            else:
                rest.append(res)
        return nodes, rest

    def why(self, module: str, expr: ast.expr | None, at: int | None, rest: list[_Res]) -> str:
        """Why an expression is not one known router, for messages: the reasons
        the resolution gave, else what the expression is."""
        reasons = sorted({str(r[1]) for r in rest if r[0] in ("unresolved", "external") and len(r) > 1 and r[1]})
        if reasons:
            return "; ".join(reasons)
        if expr is None:
            return "no router argument"
        text = self.sources.text(module, expr, 60)
        if isinstance(expr, ast.Subscript):
            return f"{text} is a subscript"
        if isinstance(expr, ast.Call):
            return f"{text} is the result of a call the analysis cannot follow"
        if isinstance(expr, (ast.Name, ast.Attribute)):
            targets = self.ev.resolve_expr(Located(module, expr, at))
            if len(targets) > 1:
                return f"{text} may hold one of {len(targets)} values here"
            for target in targets:
                if isinstance(target, Unknown):
                    return f"{target.text}: {target.reason}"
                if isinstance(target, External):
                    return f"{text} is {target.dotted}, outside the package"
            if isinstance(expr, ast.Name) and self.sources.module(module) is not None:
                scope = self.sources.scope(module)
                if expr.id not in scope.bindings and scope.star_imports:
                    return f"{text} may come from a star import"
            return f"{text} may be rebound or hold one of several routers"
        return f"{text} is a {type(expr).__name__} expression"

    # ── Nodes ────────────────────────────────────────────────────────────

    def node(self, key: tuple) -> _Node | None:
        found = self.nodes.get(key)
        if found is not None:
            return found
        entry = self._ctors.get(key[0])
        if entry is None:
            return None
        module, ctor = entry
        frame = None
        if key[1] is not None:
            frame = self._frames.get(key[1])
            if frame is None:
                return None
        node = _Node(key=key, ctor=ctor, site=frame)
        self.nodes[key] = node
        self._describe(node, module)
        return node

    def _node_id(self, node: _Node, label: str) -> str:
        ident = f"{node.module}:{label}"
        if ident in self._ids:
            if node.site is not None:
                site = node.site.site.node
                ident = f"{ident}:{getattr(site, 'col_offset', 0)}"
            else:
                ident = f"{ident}@L{node.ctor.node.lineno}"
        base, n = ident, 2
        while ident in self._ids:
            ident = f"{base}#{n}"
            n += 1
        self._ids.add(ident)
        return ident

    def _describe(self, node: _Node, module: str) -> None:
        ctor = node.ctor
        node.id = self._node_id(node, self._place(node, module))
        owner = self._owner(module, ctor.node)
        lookup = self.lookup(module, owner, node.site)
        at = self._at(module, ctor.node) if owner is None else None
        where = (node.file, ctor.node.lineno)
        self._check_route_arguments(node, module, at, lookup, where)
        override = self._router_class_overrides(module, ctor.node)
        if override is not None:
            self._limit(
                "custom_router_class",
                *where,
                f"router {node.id} is built by class {override[0]}, which overrides {', '.join(override[1])}; its prefix, "
                "dependencies and routes may differ from what the analysis reads",
            )
        changed_init = override is not None and any(
            m.rsplit(".", 1)[-1] in ("__init__", "__new__") for m in override[1]
        )

        def argument(name: str, it: str) -> tuple[Located, LocalLookup | None] | Unknown | None:
            if changed_init:
                return Unknown(f"{override[0]}.__init__ may set {it}", name)
            return self._keyword(ctor.node, name, module, at, lookup, node.site)

        if ctor.kind == "FastAPI":
            node.prefix = ""  # FastAPI() has no prefix; a prefix= keyword goes to **extra and is ignored
        else:
            self._describe_prefix(node, argument("prefix", "it"), where)
        node.dependencies = self._dependencies(
            argument("dependencies", "them"), node.file, ctor.node.lineno, f"router {node.id}"
        )
        if ctor.kind == "FastAPI":
            self._describe_redirect_slashes(node, argument("redirect_slashes", "it"), where)

    def _place(self, node: _Node, module: str) -> str:
        """Set where a router is built; the label of its id (the name it is bound
        to, else its line)."""
        ctor = node.ctor
        if node.site is not None:
            site = node.site.site
            mod = self.sources.module(site.module)
            node.module = site.module
            node.file = mod.rel if mod else site.module
            node.line = node.site.call.lineno
            return self._site_binding(node.site) or f"L{node.line}"
        mod = self.sources.module(module)
        node.module = module
        node.file = mod.rel if mod else module
        node.line = ctor.node.lineno
        analysis = self.analysis(module)
        binding = ctor.binding or self._statement_binding(module, ctor.node, self._at(module, ctor.node))
        if binding and analysis is not None:
            verdict = analysis.facts.module_end.get(binding)
            if verdict is None or not self._binds_call(verdict, ctor.node):
                binding = None
        return binding or f"L{node.line}"

    def _check_route_arguments(
        self, node: _Node, module: str, at: int | None, lookup: LocalLookup | None, where: tuple[str, int]
    ) -> None:
        for name in _ROUTE_ARGUMENTS:
            given = self._keyword(node.ctor.node, name, module, at, lookup, node.site)
            if given is None:
                continue
            how = (
                f"may be built with {name}= through {given.text} ({given.reason})"
                if isinstance(given, Unknown)
                else f"is built with {name}=..."
            )
            self._limit(
                "unsupported_router_argument",
                *where,
                f"router {node.id} {how}, whose routes the analysis does not read; they are not in the route list",
            )

    def _describe_prefix(
        self, node: _Node, prefix: tuple[Located, LocalLookup | None] | Unknown | None, where: tuple[str, int]
    ) -> None:
        if prefix is None:
            node.prefix = ""
        elif isinstance(prefix, Unknown):
            node.prefix = prefix
        else:
            node.prefix = self.ev.string(*prefix)
        if isinstance(node.prefix, Unknown):
            self._limit(
                "unresolved_route_prefix",
                *where,
                f"router {node.id}: {self.failed_text('prefix', prefix, node.prefix)}; "
                "its routes show the prefix as <unresolved: ...>",
            )

    def _describe_redirect_slashes(
        self, node: _Node, flag: tuple[Located, LocalLookup | None] | Unknown | None, where: tuple[str, int]
    ) -> None:
        value = self.ev.boolean(*flag) if isinstance(flag, tuple) else flag
        if isinstance(value, bool):
            node.redirect_slashes = value
        elif isinstance(value, Unknown):
            self._limit(
                "unresolved_redirect_slashes",
                *where,
                f"app {node.id}: {self.failed_text('redirect_slashes', flag, value)}; "
                "it is reported as True, FastAPI's default",
            )

    def _site_binding(self, frame: _Frame) -> str | None:
        if frame.parent is not None:
            return None
        return self._statement_binding(frame.site.module, frame.site.node, frame.site.at)

    def _statement_binding(self, module: str, call: ast.AST, at: int | None) -> str | None:
        """The module name a top-level ``name = <call>`` binds."""
        mod = self.sources.module(module)
        if mod is None or at is None:
            return None
        stmt = mod.tree.body[at]
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is call:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            return names[-1] if names else None
        return None

    @staticmethod
    def _binds_call(verdict: tuple, call: ast.Call) -> bool:
        """Whether a module-end verdict still holds the router ``call`` built."""
        if verdict[0] in ("router", "router_rebound"):
            return verdict[1] is call
        return verdict[0] == "call" and isinstance(verdict[1], Located) and verdict[1].node is call

    # ── Values ───────────────────────────────────────────────────────────

    def _keyword(
        self,
        call: ast.Call,
        name: str,
        module: str,
        at: int | None,
        lookup: LocalLookup | None,
        frame: _Frame | None,
    ) -> tuple[Located, LocalLookup | None] | Unknown | None:
        """The ``name=`` argument of ``call`` with the lookup to evaluate it in,
        looking through ``**mapping`` expansions and a function's ``**kwargs``;
        None when absent."""
        for kw in call.keywords:
            if kw.arg == name:
                return Located(module, kw.value, at), lookup
        for kw in call.keywords:
            if kw.arg is not None:
                continue
            extra = self._kwargs_parameter(kw.value, module, frame)
            if extra is None:
                extra = self.ev.mapping_items(Located(module, kw.value, at), lookup)
            if isinstance(extra, Unknown):
                return Unknown(extra.reason, f"**{self.sources.text(module, kw.value, 60)}")
            if name in extra:
                return extra[name]
        return None

    def _kwargs_parameter(
        self, value: ast.expr, module: str, frame: _Frame | None
    ) -> dict[str, tuple[Located, LocalLookup | None]] | Unknown | None:
        """The keywords in ``**kwargs`` when ``value`` is the ``**kwargs``
        parameter of the function ``frame`` runs; None for any other value."""
        if frame is None or frame.module != module or not isinstance(value, ast.Name):
            return None
        info = self._fn_info(module, frame.qualname)
        if info is None or info.kwarg != value.id:
            return None
        return self._extra_keywords(frame, info)

    def _dependencies(
        self, value: tuple[Located, LocalLookup | None] | Unknown | None, file: str, line: int, owner: str
    ) -> tuple[Located, ...] | Unknown:
        if value is None:
            return ()
        result = self.ev.sequence(*value) if isinstance(value, tuple) else value
        if isinstance(result, Unknown):
            self._limit(
                "unresolved_dependencies",
                file,
                line,
                f"{owner}: {self.failed_text('dependencies', value, result)}, so its dependencies are unknown",
            )
        return result

    def failed_text(
        self, name: str, given: tuple[Located, LocalLookup | None] | Located | Unknown | None, value: Unknown
    ) -> str:
        """``prefix=<source> could not be evaluated (<why>)`` for messages, naming
        the part that failed when it is not the whole argument."""
        if isinstance(given, tuple):
            given = given[0]
        if isinstance(given, Located):
            source: str | None = self.sources.text(given.module, given.node, 60)
        else:
            source = given.text if isinstance(given, Unknown) else None
        if not source or source == name:
            head = f"its {name}"
        elif source.startswith("**"):
            head = f"its {name} (from {source})"
        else:
            head = f"{name}={source}"
        part = value.text
        why = value.reason if not part or part in (source, name) else f"{part}: {value.reason}"
        return f"{head} could not be evaluated ({why})"

    def _limit(self, kind: str, file: str | None, line: int | None, message: str) -> None:
        key = (kind, file, line, message)
        if key in self._reported:
            return
        self._reported.add(key)
        self.problems.append(limitation(kind, stage=STAGE, file=file, line=line, message=message))

    # ── Assembly ─────────────────────────────────────────────────────────

    def _event_frames(self, module: str, event: Event) -> list[tuple[_Frame | None, bool]]:
        """(frame, executed) pairs an event runs in: once at import, once per
        call chain of its function, or once deferred (a function nothing runs)."""
        ctx = event.context
        if ctx.deferred_in is None:
            return [(None, True)]
        sites = self._sites.get((module, ctx.deferred_in))
        if sites:
            return [(frame, frame.executed) for frame in sites.values()]
        return [(None, False)]

    @staticmethod
    def _event_ref(event: Event):
        if isinstance(event, (IncludeCall, MountCall)):
            return event.parent
        if isinstance(event, RouterEscape):
            return event.router
        return event.base

    @staticmethod
    def _event_expr(event: Event) -> ast.expr | None:
        """The expression an event's router reference was read from."""
        if isinstance(event, (IncludeCall, MountCall)) or (isinstance(event, RouteReg) and not event.decorator):
            func = event.call.func
            return func.value if isinstance(func, ast.Attribute) else None
        if isinstance(event, RouteReg):
            func = event.call.func
            return func.value if isinstance(func, ast.Attribute) else None
        return None

    def resolve_at(self, ref, module: str, frame: _Frame | None, expr: ast.expr | None, at: int | None) -> list[_Res]:
        """``resolve``, then, for a name the replay could not pin because it may
        come from a star import, the evaluator's star-import resolution."""
        results = self.resolve(ref, module, frame)
        if (
            results == [("unresolved", "")]
            and isinstance(expr, ast.Name)
            and frame is None
            and self.sources.module(module) is not None
        ):
            scope = self.sources.scope(module)
            if expr.id not in scope.bindings and scope.star_imports:
                return self._targets(self.ev.resolve_name(module, expr.id, at, expr))
        return results

    def _event_owners(self, module: str, event: Event, frame: _Frame | None) -> list[_Res]:
        at = self._at(module, event.call, event.context)
        return self.resolve_at(self._event_ref(event), module, frame, self._event_expr(event), at)

    def _child_refs(self, module: str, event: IncludeCall, frame: _Frame | None) -> list[_Res]:
        at = self._at(module, event.call, event.context)
        expr = router_bases.call_argument(event.call, 0, "router")
        return self.resolve_at(event.child, module, frame, expr, at)

    def _run_context(self, module: str, owner: str | None, frame: _Frame | None) -> None:
        """Resolve what one module level or function body (as run by ``frame``)
        refers to, so the function calls it makes are known."""
        for event in self._events_by_owner.get((module, owner), ()):
            self._event_owners(module, event, frame)
            if isinstance(event, IncludeCall):
                self._child_refs(module, event, frame)
            elif isinstance(event, MountCall):
                self.resolve(event.app, module, frame)
        if owner is None:
            analysis = self._analyses.get(module)
            for verdict in analysis.facts.module_end.values() if analysis else ():
                if verdict[0] in ("router", "call"):
                    self.resolve(verdict, module, None)
        for site in self._calls_by_owner.get((module, owner), ()):
            if site.name in self._callable_names and not site.context.lazy:
                self._call(Located(module, site.call, site.at), frame)

    def discover(self, app_module: str, app_name: str) -> list[_Res]:
        """Resolve the app, and run every module level and every function body
        called at import, until no new call chain appears; then the bodies of
        functions nothing runs at import (their calls are deferred)."""
        self._callable_names = frozenset(self._route_changing_functions(list(self._analyses.items())))
        root = self._name(app_module, app_name)
        done: set[tuple] = set()
        for module in list(self._analyses):
            self._run_context(module, None, None)
        self._drain(done)
        owners = {key for key in (*self._events_by_owner, *self._calls_by_owner) if key[1] is not None}
        for module, owner in sorted(owners, key=lambda k: (k[0], k[1] or "")):
            if (module, owner) not in self._sites:
                self._run_context(module, owner, None)
        self._drain(done)
        self._report_skipped_calls()
        return root

    def _drain(self, done: set[tuple]) -> None:
        """Run every call chain not run yet, until running them makes no new one."""
        while True:
            todo = [f for f in self._frames.values() if f.key not in done]
            if not todo:
                return
            for frame in todo:
                done.add(frame.key)
                self._run_context(frame.module, frame.qualname, frame)

    def attach(self) -> tuple[list[_Edge], list[tuple[str, RouterEscape, _Frame | None, _Node]]]:
        edges: list[_Edge] = []
        escapes: list[tuple[str, RouterEscape, _Frame | None, _Node]] = []
        for module, analysis in list(self._analyses.items()):
            for event in analysis.facts.events:
                for frame, executed in self._event_frames(module, event):
                    self._attach_event(module, event, frame, executed, edges, escapes)
        for node in self.nodes.values():
            node.events.sort(key=lambda item: item[0])
        return edges, escapes

    def _attach_event(
        self,
        module: str,
        event: Event,
        frame: _Frame | None,
        executed: bool,
        edges: list[_Edge],
        escapes: list[tuple[str, RouterEscape, _Frame | None, _Node]],
    ) -> None:
        """Add one event, as run by ``frame``, to every router it acts on."""
        owners, rest = self.nodes_of(self._event_owners(module, event, frame))
        if isinstance(event, RouteReg):
            self._route_reg_problems(module, event, frame, owners, rest)
        elif isinstance(event, IncludeCall) and not owners:
            self._unresolved_parent(module, event, frame, rest)
        for owner in owners:
            if executed and self._not_imported(module, event, frame, owner):
                continue
            if executed and not isinstance(event, RouterEscape):
                self._conditional_import(module, event, frame)
            key = self.key(module, event, frame, executed)
            owner.events.append((key, event, frame, executed))
            if isinstance(event, IncludeCall):
                edges.extend(self._edges(module, owner, event, frame, executed, key))
            elif isinstance(event, RouterEscape) and not _passes_parameter(event):
                escapes.append((module, event, frame, owner))

    def _origin(self, module: str, frame: _Frame | None) -> str:
        """The module whose import runs an event: its own, or where its call chain starts."""
        while frame is not None:
            module = frame.site.module
            frame = frame.parent
        return module

    def _not_imported(self, module: str, event: Event, frame: _Frame | None, owner: _Node) -> bool:
        """Report and skip a change to the router of a module the app imports,
        made by a module that importing the app never runs. Between two modules
        nothing imports, the change is kept: it shows under unmounted routes."""
        origin = self._origin(module, frame)
        if origin in self._imported or origin == owner.module or owner.module not in self._imported:
            return False
        mod = self.sources.module(module)
        what = self._event_label(module, event)
        self._limit(
            "unimported_router_change",
            mod.rel if mod else module,
            getattr(event.call, "lineno", None),
            f"{what} changes router {owner.id}, but {origin} is not imported when {self.app_spec} is imported "
            "(no import statement that runs on import reaches it), so the change is not counted; if the module "
            "is imported another way (importlib, inside a function), it is missing",
        )
        return True

    def _conditional_import(self, module: str, event: Event, frame: _Frame | None) -> None:
        """Report a change to a router made by a module imported under a branch."""
        origin = self._origin(module, frame)
        if origin not in self._conditional_imports or event.context.lazy:
            return
        importer, line = self._conditional_imports[origin]
        mod = self.sources.module(importer)
        self._limit(
            "conditional_import",
            mod.rel if mod else importer,
            line,
            f"{origin} is imported under a branch, loop or handler at {self._import_text(origin)}, so the routes, "
            "includes and mounts it makes may not happen at runtime; they are reported as conditional",
        )

    def _event_label(self, module: str, event: Event) -> str:
        if isinstance(event, RouteReg):
            return self._reg_label(module, event)
        if isinstance(event, (IncludeCall, MountCall)):
            func = event.call.func
            base = router_bases.dotted_label(func.value) if isinstance(func, ast.Attribute) else "?"
            return f"{base}.{func.attr if isinstance(func, ast.Attribute) else '?'}(...)"
        return self.sources.text(module, event.callee, 60)

    def _route_reg_problems(
        self, module: str, event: RouteReg, frame: _Frame | None, owners: list[_Node], rest: list[_Res]
    ) -> None:
        mod = self.sources.module(module)
        file = mod.rel if mod else module
        label = self._reg_label(module, event)
        if event.base[0] == "router_rebound":
            self._limit(
                "unresolved_route_router",
                file,
                event.call.lineno,
                f"{label} is counted as a route, but a function may reassign its router "
                "(`global` or `nonlocal`) when it runs",
            )
        if owners:
            return
        if any(r[0] in ("unresolved", "external") for r in rest):
            at = self._at(module, event.call, event.context)
            detail = self.why(module, self._event_expr(event), at, rest)
            self._limit(
                "unresolved_route_router",
                file,
                event.call.lineno,
                f"{label} is not counted as a route: its router could not be resolved ({detail})",
            )

    def _reg_label(self, module: str, event: RouteReg) -> str:
        call = event.call
        func = call.func
        name = f"{router_bases.dotted_label(func.value)}.{func.attr}(...)" if isinstance(func, ast.Attribute) else "?"
        endpoint = event.endpoint
        if isinstance(endpoint, Located) and isinstance(endpoint.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return f"@{name} on {endpoint.node.name}()"
        return name

    def _unresolved_parent(self, module: str, event: IncludeCall, frame: _Frame | None, rest: list[_Res]) -> None:
        if not any(r[0] in ("unresolved", "external") for r in rest):
            return
        mod = self.sources.module(module)
        children, _ = self.nodes_of(self._child_refs(module, event, frame))
        if not children:
            return
        func = event.call.func
        base = router_bases.dotted_label(func.value) if isinstance(func, ast.Attribute) else "?"
        at = self._at(module, event.call, event.context)
        self._limit(
            "unresolved_include",
            mod.rel if mod else module,
            event.call.lineno,
            f"{base}.include_router({', '.join(c.id for c in children)}): the router it is included into "
            f"could not be resolved ({self.why(module, self._event_expr(event), at, rest)}), so these routes "
            "may be mounted somewhere the analysis cannot see",
        )

    def _edges(
        self, module: str, parent: _Node, event: IncludeCall, frame: _Frame | None, executed: bool, key: tuple
    ) -> list[_Edge]:
        mod = self.sources.module(module)
        file = mod.rel if mod else module
        line = event.call.lineno
        children, rest = self.nodes_of(self._child_refs(module, event, frame))
        child_text = self._child_text(module, event)
        if not children or len(children) + len(rest) > 1:
            at = self._at(module, event.call, event.context)
            reasons = self.why(module, router_bases.call_argument(event.call, 0, "router"), at, rest)
            ambiguous = " (it may be one of several routers)" if children else ""
            self._limit(
                "unresolved_include",
                file,
                line,
                f"{parent.id}.include_router({child_text}): the included router could not be resolved "
                f"({reasons}){ambiguous}, so its routes are missing here",
            )
            if not children or rest:
                return []
        lookup = self.lookup(module, event.context.deferred_in, frame)
        at = self._at(module, event.call, event.context)
        prefix_loc = self._keyword(event.call, "prefix", module, at, lookup, frame)
        prefix: str | Unknown = ""
        if isinstance(prefix_loc, Unknown):
            prefix = prefix_loc
        elif prefix_loc is not None:
            prefix = self.ev.string(*prefix_loc)
        if isinstance(prefix, Unknown):
            self._limit(
                "unresolved_include_prefix",
                file,
                line,
                f"{parent.id}.include_router({child_text}): {self.failed_text('prefix', prefix_loc, prefix)}; "
                "the routes show it as <unresolved: ...>",
            )
        deps = self._dependencies(
            self._keyword(event.call, "dependencies", module, at, lookup, frame),
            file,
            line,
            f"{parent.id}.include_router({child_text})",
        )
        mount = self.mount_state(module, event.context, frame, executed)
        if mount != "static":
            self._mount_limit(module, file, line, parent, child_text, event.context, frame, mount)
        return [
            _Edge(
                parent=parent,
                child=child,
                event=event,
                frame=frame,
                prefix=prefix,
                dependencies=deps,
                mount=mount,
                file=file,
                line=line,
                snapshot=key,
            )
            for child in children
        ]

    def _child_text(self, module: str, event: IncludeCall) -> str:
        child = router_bases.call_argument(event.call, 0, "router")
        return self.sources.text(module, child, 60) if child is not None else "?"

    def mount_state(self, module: str, ctx: ExecContext, frame: _Frame | None, executed: bool) -> MountState:
        """Whether code of ``module`` (run by ``frame``) runs for sure, maybe, or
        later or never: its own branch, its call chain, and whether the module
        whose import runs it is imported under a branch."""
        state: MountState = "static"
        if ctx.lazy or not executed:
            state = "deferred"
        elif ctx.conditional or self._origin(module, frame) in self._conditional_imports:
            state = "conditional"
        return _worst(state, frame.state) if frame is not None else state

    def _import_text(self, module: str) -> str:
        """Where a module imported under a branch is imported, for messages."""
        importer, line = self._conditional_imports[module]
        mod = self.sources.module(importer)
        return f"{mod.rel if mod else importer}:{line if line is not None else '?'}"

    def _site_text(self, frame: _Frame) -> str:
        mod = self.sources.module(frame.site.module)
        return f"{mod.rel if mod else frame.site.module}:{getattr(frame.site.node, 'lineno', '?')}"

    def where_text(self, module: str, ctx: ExecContext, frame: _Frame | None, mount: MountState) -> str:
        """Why code is reported as conditional or deferred, for messages."""
        origin = self._origin(module, frame)
        if (
            mount == "conditional"
            and not ctx.conditional
            and (frame is None or frame.state == "static")
            and origin in self._conditional_imports
        ):
            return f"runs when {origin} is imported, which {self._import_text(origin)} does under a branch"
        if ctx.lazy:
            return "sits in code that runs lazily or never (a lambda, a generator expression or an annotation)"
        if frame is not None and frame.state != "static" and (mount == "deferred" or not ctx.conditional):
            if frame.state == "deferred":
                return (
                    f"runs inside {_short(frame.qualname)}(), called at {self._site_text(frame)} from code that "
                    "may run later or never"
                )
            return f"runs inside {_short(frame.qualname)}(), called at {self._site_text(frame)} under a branch"
        escape = self.escape(module, ctx.deferred_in) if mount == "deferred" and ctx.deferred_in else None
        if escape is not None and (module, ctx.deferred_in or "") not in self._unplaced() and frame is None:
            return (
                f"runs inside {_short(ctx.deferred_in or '?')}(), which the analysis sees no call of at import, "
                f"but {escape}, so whether and when it runs is unknown"
            )
        if mount == "deferred" and (module, ctx.deferred_in or "") in self._unplaced():
            return (
                f"runs inside {_short(ctx.deferred_in or '?')}(), called at import through a call the analysis "
                "does not follow, so when it runs is unknown"
            )
        if mount == "deferred":
            return (
                f"runs inside {_short(ctx.deferred_in or '?')}(), which nothing calls at import, so the analysis "
                "cannot tell whether it is ever called"
            )
        return "runs under a branch, loop, handler or conditional expression"

    def _mount_limit(
        self,
        module: str,
        file: str,
        line: int,
        parent: _Node,
        child_text: str,
        ctx: ExecContext,
        frame: _Frame | None,
        mount: MountState,
    ) -> None:
        call = f"{parent.id}.include_router({child_text})"
        where = self.where_text(module, ctx, frame, mount)
        if mount == "conditional":
            self._limit(
                "conditional_router_mount",
                file,
                line,
                f"{call} {where}, so FastAPI may not apply it; its routes are reported as conditional",
            )
        else:
            self._limit(
                "deferred_router_mount",
                file,
                line,
                f"{call} {where}; its routes are reported as deferred",
            )


# ── Route enumeration ────────────────────────────────────────────────────


class _Emitter:
    def __init__(self, builder: _Builder, edges: list[_Edge]) -> None:
        self.b = builder
        self.edges_by_event: dict[tuple[int, tuple | None, tuple], list[_Edge]] = {}
        for edge in edges:
            frame_key = edge.frame.key if edge.frame else None
            self.edges_by_event.setdefault((id(edge.event), frame_key, edge.parent.key), []).append(edge)
        self.routes: list[LiveRoute] = []
        self._qualnames: dict[str, dict[int, str]] = {}
        self._reported_cycles: set[tuple] = set()
        # Mounts at a known path: (the route list, its length after the mounted
        # app's routes, path, label, file, line).
        self.mounts: list[tuple[list[LiveRoute], int, str, str, str, int]] = []

    def _qualname(self, module: str, node: ast.AST) -> str:
        names = self._qualnames.get(module)
        if names is None:
            analysis = self.b.analysis(module)
            names = analysis.qualnames if analysis else {}
            self._qualnames[module] = names
        return names.get(id(node), getattr(node, "name", "?"))

    def emit(
        self,
        node: _Node,
        path_prefix: str,
        layers: tuple[DepLayer, ...],
        chain: tuple[str, ...],
        mount: MountState,
        upto: tuple | None,
        own_via: LayerVia,
        out: list[LiveRoute],
        stack: tuple[tuple, ...],
        *,
        included: bool = False,
        include: _Edge | None = None,
    ) -> None:
        """Routes of ``node`` in dispatch order. ``upto``: the key of the include
        (``include``) reaching them; under the ``copy`` model only events that
        ran before it count. ``included``: reached through an include."""
        own = DepLayer(own_via, node.id, node.dependencies)
        node_prefix = self._prefix_text(node.prefix)
        for key, event, frame, executed in node.events:
            module = self.b._event_modules[id(event)]
            event_mount = self._event_mount(include, upto, key, module, event, frame, mount)
            if event_mount is None:
                continue
            if isinstance(event, RouteReg):
                self._route(
                    node,
                    event,
                    frame,
                    executed,
                    module,
                    path_prefix + node_prefix,
                    layers + (own,),
                    chain,
                    event_mount,
                    out,
                )
            elif isinstance(event, IncludeCall):
                frame_key = frame.key if frame else None
                for edge in self.edges_by_event.get((id(event), frame_key, node.key), []):
                    child = edge.child
                    if child.key in stack or child.key == node.key:
                        self._cycle(edge)
                        continue
                    child.included = True
                    edge_label = f"{node.id} -> {child.id}"
                    self.emit(
                        child,
                        path_prefix + node_prefix + self._prefix_text(edge.prefix),
                        layers + (own, DepLayer("router", edge_label, edge.dependencies)),
                        chain + (child.id,),
                        _worst(event_mount, edge.mount),
                        edge.snapshot,
                        "router",
                        out,
                        stack + (node.key,),
                        included=True,
                        include=edge,
                    )
            elif isinstance(event, MountCall) and not included:
                self._mount(
                    node, event, frame, executed, module, path_prefix + node_prefix, chain, event_mount, out, stack
                )
            elif isinstance(event, MountCall) and include is not None:
                self._included_mount(
                    include, node, event, frame, executed, module, path_prefix, chain, event_mount, out, stack
                )

    def _event_mount(
        self,
        include: _Edge | None,
        upto: tuple | None,
        key: tuple,
        module: str,
        event: Event,
        frame: _Frame | None,
        mount: MountState,
    ) -> MountState | None:
        """How an event on a router reached through ``include`` is listed, or
        None when that include does not serve it. Under ``copy`` the include
        serves only what ran before it; under ``live`` it serves everything;
        under ``unknown`` what only ``live`` serves is listed as deferred."""
        if include is None or upto is None:
            return mount
        kind = self.b.include_model.kind
        if kind == "live":
            self._extra_runs(module, event, frame)
            return mount
        late = not self.b.ran_before(key, upto)
        if late:
            if kind == "unknown":
                self._unknown_model(include, module, event)
                return "deferred"
            if not self._may_run_before(include, module, event, frame):
                return None
            return "deferred"
        if kind == "unknown":
            if self._runs_only_after(include, key, module, event, frame) is not None:
                self._unknown_model(include, module, event)
            return mount
        if self._copied_before(include, key, module, event, frame):
            return None
        self._extra_runs(module, event, frame)
        return mount

    def _extra_runs(self, module: str, event: Event, frame: _Frame | None) -> None:
        """Report a registration in a function the analysis runs from a followed
        call that code it does not follow may run again: the route may be
        registered more than once, or with other arguments."""
        if frame is None or not frame.executed or not isinstance(event, (RouteReg, IncludeCall)):
            return
        b = self.b
        link: _Frame | None = frame
        while link is not None:
            reason = b.direct_use(link.module, link.qualname)
            if reason is not None:
                mod = b.sources.module(module)
                file = mod.rel if mod else module
                what = (
                    b._reg_label(module, event)
                    if isinstance(event, RouteReg)
                    else (f"include_router({b._child_text(module, event)})")
                )
                b._limit(
                    "deferred_route_registration" if isinstance(event, RouteReg) else "deferred_router_mount",
                    file,
                    event.call.lineno,
                    f"{what} runs inside {_short(frame.qualname)}(), called at {b._site_text(frame)}, but {reason}, "
                    "so it may run more often than the calls the analysis follows: its routes are listed once "
                    "for each followed call",
                )
                return
            link = link.parent

    def _unknown_model(self, include: _Edge, module: str, event: Event) -> None:
        """Report an event only the ``live`` include model serves, under an unknown FastAPI version."""
        b = self.b
        if not isinstance(event, (RouteReg, IncludeCall, MountCall)):
            return
        mod = b.sources.module(module)
        file = mod.rel if mod else module
        model = b.include_model
        if isinstance(event, RouteReg):
            kind, what = "deferred_route_registration", b._reg_label(module, event)
        elif isinstance(event, IncludeCall):
            kind, what = "deferred_router_mount", f"{include.child.id}.include_router({b._child_text(module, event)})"
        else:
            kind, what = "deferred_router_mount", f"{include.child.id}.mount(...)"
        b._limit(
            kind,
            file,
            event.call.lineno,
            f"{what} on a router {include.parent.id}.include_router({include.child.id}) at {include.file}:"
            f"{include.line} reaches, which FastAPI up to 0.136 does not serve through that include (it copies the "
            f"router's routes when the include runs) and 0.137 and later does; the FastAPI version is unknown "
            f"({model.reason}), so its routes are reported as deferred",
        )

    def _included_mount(
        self,
        include: _Edge,
        node: _Node,
        event: MountCall,
        frame: _Frame | None,
        executed: bool,
        module: str,
        path_prefix: str,
        chain: tuple[str, ...],
        mount: MountState,
        out: list[LiveRoute],
        stack: tuple[tuple, ...],
    ) -> None:
        """A mount on an included router: FastAPI up to 0.136 drops it, 0.137 and
        later serves it under the include's prefix (not the router's own, which
        Starlette's ``mount`` never applies)."""
        kind = self.b.include_model.kind
        if kind == "copy":
            return
        if kind == "unknown":
            self._unknown_model(include, module, event)
            mount = "deferred"
        self._mount(node, event, frame, executed, module, path_prefix, chain, mount, out, stack)

    def _may_run_before(self, include: _Edge, module: str, event: Event, frame: _Frame | None) -> bool:
        """Whether an event the call chains place after ``include`` may also run
        before it: a function of its chain can be reached other than through the
        calls the analysis follows. It is then kept, as deferred, and reported."""
        b = self.b
        if frame is None or not isinstance(event, (RouteReg, IncludeCall)):
            return False
        unplaced = b._unplaced()
        reason: str | None = None
        link: _Frame | None = frame
        while link is not None and reason is None:
            if (link.module, link.qualname) in unplaced:
                reason = f"{_short(link.qualname)}() is also reached through a call the analysis does not follow"
            else:
                reason = b.escape(link.module, link.qualname)
            link = link.parent
        if reason is None:
            return False
        mod = b.sources.module(module)
        file = mod.rel if mod else module
        what = (
            b._reg_label(module, event)
            if isinstance(event, RouteReg)
            else f"{include.child.id}.include_router({b._child_text(module, event)})"
        )
        b._limit(
            "deferred_route_registration" if isinstance(event, RouteReg) else "deferred_router_mount",
            file,
            event.call.lineno,
            f"{what} runs inside {_short(frame.qualname)}(), called at {b._site_text(frame)} after "
            f"{include.parent.id}.include_router({include.child.id}) at {include.file}:{include.line} copies "
            f"{include.child.id}'s routes, but {reason}, so it may also run before that include; its routes are "
            "reported as deferred",
        )
        return True

    def _runs_only_after(
        self, include: _Edge, key: tuple, module: str, event: Event, frame: _Frame | None
    ) -> str | None:
        """The function a deferred event runs in, when it can only run after
        ``include``, which runs at import; None otherwise."""
        b = self.b
        if not isinstance(event, (RouteReg, IncludeCall, MountCall)):
            return None
        if b.runs_at_import(key) or not b.runs_at_import(include.snapshot):
            return None
        return b.runs_after_import(module, event, frame)

    def _copied_before(self, include: _Edge, key: tuple, module: str, event: Event, frame: _Frame | None) -> bool:
        """Whether ``include`` copied its child's routes before a deferred event
        on the child can run: the include runs at import and the event only after
        it. FastAPI does not serve what the event adds through that include, so it
        is left out, and reported, since a call the analysis cannot see could
        still run it at import."""
        b = self.b
        function = self._runs_only_after(include, key, module, event, frame)
        if function is None:
            return False
        mod = b.sources.module(module)
        file = mod.rel if mod else module
        copied = (
            f"FastAPI copied {include.child.id}'s routes when {include.parent.id}.include_router("
            f"{include.child.id}) ran at import ({include.file}:{include.line}), so"
        )
        where = f"runs inside {_short(function)}(), which nothing calls at import"
        if isinstance(event, MountCall):
            return True  # FastAPI up to 0.136 drops a mount on an included router anyway
        if isinstance(event, RouteReg):
            b._limit(
                "deferred_route_registration",
                file,
                event.call.lineno,
                f"{b._reg_label(module, event)} {where}; {copied} the route is not served through that include "
                "and is left out of it",
            )
        elif isinstance(event, IncludeCall):
            b._limit(
                "deferred_router_mount",
                file,
                event.call.lineno,
                f"{include.child.id}.include_router({b._child_text(module, event)}) {where}; {copied} the routes "
                "it adds are not served through that include and are left out of it",
            )
        return True

    def _prefix_text(self, prefix: str | Unknown) -> str:
        if isinstance(prefix, Unknown):
            return unresolved_path_part(prefix.text[:_UNRESOLVED_PATH_MAX])
        return prefix

    def _cycle(self, edge: _Edge) -> None:
        key = (edge.file, edge.line)
        if key in self._reported_cycles:
            return
        self._reported_cycles.add(key)
        self.b._limit(
            "router_include_cycle",
            edge.file,
            edge.line,
            f"{edge.parent.id}.include_router({edge.child.id}) closes an include cycle; the edge is cut",
        )

    def _methods(self, module: str, event: RouteReg, lookup: LocalLookup | None, file: str) -> tuple[str, ...]:
        methods = event.methods
        if not isinstance(methods, Located):
            return methods
        b = self.b
        value: Located | None = methods
        reason: str | None = None
        written = f"methods={b.sources.text(module, methods.node, 60)}"
        if methods.node is event.call:
            value, reason, written = self._methods_from_kwargs(module, event, methods, lookup)
        names: list[str] = []
        if reason is None and value is not None and b.ev.is_none(value, lookup) is not True:
            found = b.ev.sequence(value, lookup)
            reason = found.reason if isinstance(found, Unknown) else None
            if not isinstance(found, Unknown):
                for element in found:
                    text = b.ev.string(element)
                    if isinstance(text, Unknown):
                        reason = f"element {text.text}: {text.reason}"
                        break
                    names.append(text)
        elif reason is None:
            # No methods=, or methods=None: FastAPI's default.
            return router_bases.DEFAULT_METHODS
        if reason is not None:
            b._limit(
                "unresolved_route_methods",
                file,
                event.call.lineno,
                f"{b._reg_label(module, event)}: {written} could not be evaluated ({reason}), so the route is "
                "reported with method ANY",
            )
            return ("ANY",)
        return router_bases.method_names(names)

    def _methods_from_kwargs(
        self, module: str, event: RouteReg, call: Located, lookup: LocalLookup | None
    ) -> tuple[Located | None, str | None, str]:
        """``(methods value, why it is unknown, the text read)`` from the
        registration's ``**`` arguments; no value when none sets ``methods``."""
        b = self.b
        found: Located | None = None
        source = ""
        for kw in event.call.keywords:
            if kw.arg is not None:
                continue
            written = f"**{b.sources.text(module, kw.value, 60)}"
            items = b.ev.mapping(Located(call.module, kw.value, call.at), lookup)
            if isinstance(items, Unknown):
                return None, items.reason, written
            if "methods" in items:
                found, source = items["methods"], written
        if found is None:
            return None, None, "methods="
        return found, None, f"methods={b.sources.text(found.module, found.node, 60)} (from {source})"

    def _route(
        self,
        node: _Node,
        event: RouteReg,
        frame: _Frame | None,
        executed: bool,
        module: str,
        prefix: str,
        layers: tuple[DepLayer, ...],
        chain: tuple[str, ...],
        mount: MountState,
        out: list[LiveRoute],
    ) -> None:
        b = self.b
        mod = b.sources.module(module)
        file = mod.rel if mod else module
        lookup = b.lookup(module, event.context.deferred_in, frame)
        at = b._at(module, event.call, event.context)
        path = b.ev.string(Located(module, event.path, at), lookup)
        if isinstance(path, Unknown):
            text = b.sources.text(module, event.path, _UNRESOLVED_PATH_MAX)
            failed = b.failed_text("path", Located(module, event.path, at), path)
            b._limit(
                "unresolved_route_path",
                file,
                event.call.lineno,
                f"{b._reg_label(module, event)}: {failed}; "
                "the route is reported with an <unresolved: ...> path that matches no frontend call",
            )
            path_text = unresolved_path_part(text)
        else:
            path_text = path
        handler, handler_id, def_file, def_line, endpoint = self._endpoint(module, event, file, lookup)
        dec_deps = b._dependencies(
            b._keyword(event.call, "dependencies", module, at, lookup, frame),
            file,
            event.call.lineno,
            b._reg_label(module, event),
        )
        route_layers = layers + (DepLayer("route decorator", f"{node.id}:{handler}", dec_deps),)
        own_state = b.mount_state(module, event.context, frame, executed)
        route_mount = _worst(mount, own_state)
        if own_state == "deferred":
            where = b.where_text(module, event.context, frame, own_state)
            b._limit(
                "deferred_route_registration",
                file,
                event.call.lineno,
                f"{b._reg_label(module, event)} {where}; its routes are reported as deferred",
            )
        for method in self._methods(module, event, lookup, file):
            out.append(
                LiveRoute(
                    method=method,
                    path=prefix + path_text,
                    kind=event.kind,
                    handler=handler,
                    handler_id=handler_id,
                    file=def_file,
                    line=def_line,
                    registered_file=file,
                    registered_line=event.call.lineno,
                    chain=chain,
                    mount=route_mount,
                    layers=route_layers,
                    order=0,
                    endpoint=endpoint,
                )
            )

    def _endpoint(
        self, module: str, event: RouteReg, file: str, lookup: LocalLookup | None
    ) -> tuple[str, str, str, int, Located]:
        b = self.b
        endpoint = event.endpoint
        if not isinstance(endpoint, Located):
            return "?", f"unresolved:{module}:{endpoint.text}", file, event.call.lineno, Located(module, event.call)
        node = endpoint.node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            applied = node.decorator_list
            if event.decorator:
                # Decorators below the route decorator wrap the function before it registers.
                index = next((k for k, d in enumerate(applied) if d is event.call), len(applied))
                applied = applied[index + 1 :]
            self._check_decorators(module, node, applied, file)
            return node.name, f"{module}:{self._qualname(module, node)}", file, node.lineno, endpoint
        if isinstance(node, ast.Lambda):
            owner = b._owner(module, node)
            qualname = f"{owner}.<locals>.{lambda_qualname(node)}" if owner else lambda_qualname(node)
            return "<lambda>", f"{module}:{qualname}", file, node.lineno, endpoint
        local = b.ev.local_value(node, lookup) if isinstance(node, (ast.Name, ast.Attribute)) else None
        nested = self._local_def(module, event, node) if local is not None else None
        if nested is not None:
            self._check_decorators(module, nested, nested.decorator_list, file)
            return (
                nested.name,
                f"{module}:{self._qualname(module, nested)}",
                file,
                nested.lineno,
                Located(module, nested),
            )
        if local is None:
            for target in b.ev.resolve_expr(endpoint):
                if isinstance(target, Def) and isinstance(target.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    tmod = b.sources.module(target.module)
                    tfile = tmod.rel if tmod else target.module
                    self._check_decorators(target.module, target.node, target.node.decorator_list, tfile)
                    return (
                        target.node.name,
                        f"{target.module}:{target.qualname}",
                        tfile,
                        target.node.lineno,
                        Located(target.module, target.node, None),
                    )
        text = b.sources.text(module, node, 60)
        return text, f"unresolved:{module}:{text}", file, event.call.lineno, endpoint

    def _local_def(self, module: str, event: RouteReg, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """The function a name defined by one ``def`` in the registering function's body denotes."""
        owner = event.context.deferred_in
        info = self.b._fn_info(module, owner) if owner else None
        if info is None or not isinstance(node, ast.Name) or node.id in info.params:
            return None
        defs = _nested_defs(info.node, node.id)
        return defs[0] if len(defs) == 1 and not _rebinds(info.node, node.id) else None

    def _check_decorators(
        self, module: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, decorators: list[ast.expr], file: str
    ) -> None:
        b = self.b
        at = b._at(module, fn)
        for dec in decorators:
            reason = opaque_decorator(b.ev, module, dec, at)
            if reason is not None:
                b._limit(
                    "opaque_decorator",
                    file,
                    getattr(dec, "lineno", fn.lineno),
                    f"@{b.sources.text(module, dec, 60)} on {fn.name}(): {reason}, so FastAPI registers that wrapper, "
                    f"not {fn.name}(); the handler and its dependencies are reported from {fn.name}()",
                )

    def _mount(
        self,
        node: _Node,
        event: MountCall,
        frame: _Frame | None,
        executed: bool,
        module: str,
        prefix: str,
        chain: tuple[str, ...],
        mount: MountState,
        out: list[LiveRoute],
        stack: tuple[tuple, ...],
    ) -> None:
        b = self.b
        mod = b.sources.module(module)
        file = mod.rel if mod else module
        at = b._at(module, event.call, event.context)
        lookup = b.lookup(module, event.context.deferred_in, frame)
        path = b.ev.string(Located(module, event.path, at), lookup)
        apps, _rest = b.nodes_of(b.resolve(event.app, module, frame))
        sub = apps[0] if len(apps) == 1 and apps[0].ctor.kind == "FastAPI" else None
        if sub is None:
            text = b.sources.text(module, router_bases.call_argument(event.call, 1, "app") or event.call, 60)
            b._limit(
                "asgi_mount",
                file,
                event.call.lineno,
                f"{node.id}.mount(..., {text}) mounts an application that is not a FastAPI app the analysis can "
                "follow; its routes are not analyzed",
            )
            return
        if isinstance(path, Unknown):
            path_text = unresolved_path_part(path.text[:_UNRESOLVED_PATH_MAX])
            b._limit(
                "unresolved_mount_path",
                file,
                event.call.lineno,
                f"{node.id}.mount() path could not be evaluated ({path.text[:_UNRESOLVED_PATH_MAX]}: {path.reason}); "
                f"the mounted app's routes show it as <unresolved: ...>",
            )
        else:
            path_text = path.rstrip("/")  # Starlette's Mount strips a trailing slash
        sub.included = True
        if sub.key in stack:
            return
        mount_state = _worst(mount, b.mount_state(module, event.context, frame, executed))
        self.emit(sub, prefix + path_text, (), chain + (sub.id,), mount_state, None, "app", out, stack + (node.key,))
        if not isinstance(path, Unknown):
            self.mounts.append((out, len(out), prefix + path_text, f"{node.id}.mount()", file, event.call.lineno))

    def report_mount_shadows(self) -> None:
        """Starlette serves a request from the first route that matches, and a
        mount matches every path under its own: a route registered after it
        that the mount's pattern overlaps is not reached for those requests.
        Matching does not model this, so it is reported."""
        for out, end, path, label, file, line in self.mounts:
            for route in out[end:]:
                caught = _mount_catches(path, route.path)
                if caught is None:
                    continue
                effect = (
                    "Starlette never reaches the route"
                    if caught == "always"
                    else "Starlette may send some of the route's requests to the mount"
                )
                self.b._limit(
                    "mount_shadows_route",
                    route.registered_file,
                    route.registered_line,
                    f"{route.method} {route.path} is registered after {label} at {file}:{line}, which takes every "
                    f"request under {path + '/'}; {effect}, but the analysis still matches it",
                )


# ── Escape analysis ──────────────────────────────────────────────────────


class _Effect(NamedTuple):
    """Why handing a router on may change routes. ``changes``: by changing its
    routes, prefix or dependencies in place, which can move or remove routes the
    analysis counts; otherwise only by adding routes or includes."""

    text: str
    changes: bool = False


class _Escapes:
    """Whether a router handed to other code can change routes: the callee is
    followed into the package (up to ``ESCAPE_MAX_DEPTH`` calls deep); a use
    that only reads attributes or compares is harmless; anything that registers,
    includes, mounts, mutates or passes the router on to unknown code is not."""

    def __init__(self, builder: _Builder) -> None:
        self.b = builder

    def check(self, module: str, event: RouterEscape, arg: ast.expr | None, node: _Node, frame: _Frame | None) -> None:
        b = self.b
        mod = b.sources.module(module)
        file = mod.rel if mod else module
        line = getattr(event.call, "lineno", None)
        callee = event.callee
        if isinstance(callee, ast.Attribute) and callee.attr in router_bases.STARLETTE_REGISTRATIONS:
            b._limit(
                "unsupported_route_registration",
                file,
                line,
                f"{node.id}.{callee.attr}(...) registers a Starlette-level route, which the analysis does not "
                "model; it is not in the route list",
            )
            return
        if event.call is callee:
            what = b.sources.text(module, callee, 60)
            b._limit(
                "opaque_router_call",
                file,
                line,
                f"{node.id}: {what} is used without being called here (bound to a name or passed on), so routes "
                "or includes made through it are not in the analysis",
            )
            return
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr in router_bases.ALIASED_ATTRIBUTES
            and isinstance(callee.ctx, ast.Load)
        ):
            if self._reads_list(module, event.call):
                return
            b._limit(
                "opaque_router_change",
                file,
                line,
                f"{node.id}: its .{callee.attr} list is bound to a name or passed on here, so a change made through "
                "it is not in the analysis",
            )
            return
        if not isinstance(event.call, ast.Call) or arg is None:
            what = b.sources.text(module, callee, 60)
            b._limit(
                "opaque_router_change",
                file,
                line,
                f"{node.id}: {what} is changed directly, so its routes, prefix or dependencies may differ from "
                "what the analysis reports",
            )
            return
        effect = self._call_effect(module, event.call, arg, 0, frozenset(), frame)
        if effect is not None:
            callee_text = b.sources.text(module, callee, 60)
            b._limit(
                "opaque_router_change" if effect.changes else "opaque_router_call",
                file,
                line,
                f"{node.id} is passed to {callee_text}(...), which {effect.text}; routes or dependencies "
                f"{'changed' if effect.changes else 'added'} there are not in the analysis",
            )

    def _reads_list(self, module: str, call: ast.AST) -> bool:
        """Whether ``call`` is an outside function known to only read the list it is handed."""
        if not isinstance(call, ast.Call):
            return False
        targets = self.b.ev.resolve_expr(Located(module, call.func, None))
        return bool(targets) and all(isinstance(t, External) and t.dotted in LIST_SAFE_EXTERNAL_CALLS for t in targets)

    def _hands_on(self, module: str, value: ast.expr, parents: dict[int, ast.AST]) -> bool:
        """Whether a router's list, read as ``value``, is bound, stored or passed
        to code that is not known to only read it, as at module level: an index,
        a loop, an unpacking copy or a reading builtin is a read."""
        child: ast.AST = value
        parent = parents.get(id(value))
        if isinstance(parent, (ast.keyword, ast.Starred)):
            unpacked = isinstance(parent, ast.Starred)
            child, parent = parent, parents.get(id(parent))
            if unpacked and not isinstance(parent, ast.Call):
                return False  # `[*router.routes]` copies the list
        if isinstance(parent, ast.Call) and parent.func is not child:
            func = parent.func
            if isinstance(func, ast.Name) and func.id in router_bases.LIST_READING_CALLS:
                return False
            return not self._reads_list(module, parent)
        return isinstance(parent, _BINDING)

    def _call_effect(
        self,
        module: str,
        call: ast.Call,
        arg: ast.expr,
        depth: int,
        seen: frozenset[tuple[str, str, str]],
        frame: _Frame | None,
    ) -> _Effect | None:
        """Why passing ``arg`` to ``call`` may change routes, or None when it can't.
        When ``call`` runs at import, the graph runs the callee's body with this
        argument, so the registrations and includes it makes there are counted.
        Code the analysis does not follow is taken to use FastAPI's registration
        API, which only adds routes."""
        b = self.b
        func = call.func
        if isinstance(func, ast.Name) and func.id in router_bases.SAFE_ARGUMENT_CALLS:
            return None
        targets = b.ev.resolve_expr(Located(module, func, None))
        effects: list[_Effect] = []
        for target in targets:
            if isinstance(target, External):
                if target.dotted in ROUTER_SAFE_EXTERNAL_CALLS:
                    continue
                effects.append(_Effect(f"is outside the analyzed package ({target.dotted})"))
            elif isinstance(target, Def) and isinstance(target.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                effect = self._function_effect(target, target.node, call, arg, depth, seen, frame)
                if effect is not None:
                    effects.append(effect)
            elif isinstance(target, Def):
                effects.append(_Effect(f"is a class ({target.qualname}) the analysis does not follow"))
            elif isinstance(target, Unknown):
                effects.append(_Effect(f"could not be resolved ({target.text}: {target.reason})"))
            else:
                effects.append(_Effect("could not be resolved"))
        if not effects:
            return None
        return _Effect("; ".join(e.text for e in effects), any(e.changes for e in effects))

    def _function_effect(
        self,
        target: Def,
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        call: ast.Call,
        arg: ast.expr,
        depth: int,
        seen: frozenset[tuple[str, str, str]],
        frame: _Frame | None,
    ) -> _Effect | None:
        """Why a package function handed ``arg`` by ``call`` may change routes."""
        b = self.b
        param = _parameter_for(fn, call, arg)
        # Past these the package code is not read, so it may change the router in place.
        if param is None:
            return _Effect(f"receives it through *args or **kwargs ({target.qualname})", changes=True)
        if depth >= ESCAPE_MAX_DEPTH:
            b._escape_cuts += 1  # the result depends on how deep this call is reached
            return _Effect(f"calls more than {ESCAPE_MAX_DEPTH} functions deep ({target.qualname})", changes=True)
        site_key = (id(call), *(frame.key if frame is not None else ()))
        site = b._frames.get(site_key)
        runs = site is not None and site.executed
        modeled = runs and site is not None and (site.module, site.qualname) == (target.module, target.qualname)
        key = (target.module, target.qualname, param)
        if key in seen:
            b._escape_cuts += 1
            return None
        memo = (*key, site_key if modeled else None)
        if memo in b._escape_memo:
            inner = b._escape_memo[memo]
        else:
            cuts = b._escape_cuts
            inner = self._param_effect(target, param, depth + 1, seen | {key}, site if modeled else None)
            # Past a cut the result depends on the chain that led here.
            if b._escape_cuts == cuts:
                b._escape_memo[memo] = inner
        return _Effect(f"{inner.text} (in {target.qualname})", inner.changes) if inner is not None else None

    def _param_effect(
        self, target: Def, param: str, depth: int, seen: frozenset, frame: _Frame | None
    ) -> _Effect | None:
        fn = target.node
        assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(fn):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        # Every use counts: an add-only use must not hide a later change in place.
        effects: list[_Effect] = []
        for stmt in fn.body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and node.id == param and isinstance(node.ctx, ast.Load):
                    effect = self._use_effect(target.module, node, parents, depth, seen, frame)
                    if effect is not None and effect.changes:
                        return effect
                    if effect is not None and effect not in effects:
                        effects.append(effect)
        return _Effect("; ".join(e.text for e in effects)) if effects else None

    def _use_effect(
        self, module: str, node: ast.Name, parents: dict[int, ast.AST], depth: int, seen, frame: _Frame | None
    ) -> _Effect | None:
        current: ast.AST = node
        value: ast.expr = node
        parent = parents.get(id(node))
        while isinstance(parent, ast.Attribute) and parent.attr == "router":
            current = value = parent
            parent = parents.get(id(parent))
        if isinstance(parent, ast.Attribute):
            return self._attribute_effect(module, parent, parents, frame)
        if isinstance(parent, ast.Starred):
            current = value = parent
            parent = parents.get(id(parent))
        if isinstance(parent, ast.keyword):
            current, value = parent, parent.value
            parent = parents.get(id(parent))
        if isinstance(parent, ast.Call):
            if parent.func is current:
                return None  # calling the app as an ASGI application
            func = parent.func
            if frame is not None and isinstance(func, ast.Attribute) and func.attr in _MODELED_USES:
                return None  # included, mounted or registered in a body the graph runs with the argument
            return self._call_effect(module, parent, value, depth, seen, frame)
        if parent is None or isinstance(parent, _HARMLESS_PARENTS):
            return None
        # Package code the analysis reads loses track of the router here.
        return _Effect(f"stores or returns it ({type(parent).__name__})", changes=True)

    def _attribute_effect(
        self, module: str, parent: ast.Attribute, parents: dict[int, ast.AST], frame: _Frame | None
    ) -> _Effect | None:
        """Why ``<router>.<attr>`` used in a callee may change routes: a call of a
        registration, include or mount method, or a change to its prefix,
        dependencies or routes."""
        attr = parent.attr
        grand = parents.get(id(parent))
        if isinstance(grand, ast.Call) and grand.func is parent:
            if frame is not None and attr in _MODELED_USES:
                return None  # the graph runs this body with the argument
            if attr in router_bases.ROUTE_DECORATORS or attr in router_bases.REGISTRATION_CALLS:
                return _Effect(f"registers routes on it (.{attr})")
            if attr in (router_bases.INCLUDE_ROUTER, router_bases.MOUNT):
                return _Effect(f"calls .{attr}() on it")
            if attr in router_bases.STARLETTE_REGISTRATIONS:
                return _Effect(f"registers Starlette routes on it (.{attr})")
            return None
        if attr not in router_bases.MUTATED_ATTRIBUTES:
            return None
        if isinstance(parent.ctx, (ast.Store, ast.Del)):
            return _Effect(f"assigns its .{attr}", changes=True)
        if isinstance(grand, ast.Attribute) and isinstance(parents.get(id(grand)), ast.Call):
            return _Effect(f"changes its .{attr}", changes=True)
        if attr in router_bases.ALIASED_ATTRIBUTES and self._hands_on(module, parent, parents):
            return _Effect(f"binds or hands on its .{attr} list", changes=True)
        if isinstance(grand, ast.Subscript) and isinstance(grand.ctx, (ast.Store, ast.Del)):
            return _Effect(f"changes its .{attr}", changes=True)
        return None


# Where a value is bound to a name or stored.
_BINDING = (
    ast.Assign,
    ast.AnnAssign,
    ast.NamedExpr,
    ast.Return,
    ast.Yield,
    ast.YieldFrom,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
)


# A whole path segment that is one `str` parameter: it matches any segment.
_ANY_SEGMENT = re.compile(r"\{[^{}:]+(?::str)?\}")


def _mount_catches(mount: str, route: str) -> str | None:
    """Whether requests for ``route`` fall under ``mount``, which Starlette
    matches as ``{mount}/{anything}``: ``always``, ``sometimes`` (a parameter on
    either side may or may not overlap) or None."""
    mount_segments = mount.split("/")[1:] if mount else []
    route_segments = route.split("/")[1:]
    always = True
    for k, mount_seg in enumerate(mount_segments):
        if k >= len(route_segments):
            return None
        route_seg = route_segments[k]
        if ":path}" in route_seg:
            return "sometimes"  # spans segments; the overlap is not worked out
        if "{" not in mount_seg and "{" not in route_seg:
            if mount_seg != route_seg:
                return None
        elif not _ANY_SEGMENT.fullmatch(mount_seg):
            always = False
    if len(route_segments) <= len(mount_segments):
        return None  # the mount needs a `/` after its own path
    return "always" if always else "sometimes"


def _parameter_for(fn: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call, arg: ast.expr) -> str | None:
    """The parameter of ``fn`` that receives ``arg`` in ``call``."""
    params = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
    for k, value in enumerate(call.args):
        if isinstance(value, ast.Starred):
            return None
        if value is arg:
            return params[k] if k < len(params) else (fn.args.vararg.arg if fn.args.vararg else None)
    names = set(params) | {a.arg for a in fn.args.kwonlyargs}
    for kw in call.keywords:
        if kw.value is arg:
            if kw.arg in names:
                return kw.arg
            return None
    return None


# ── Entry points ─────────────────────────────────────────────────────────


class BackendBuilder:
    """Builds ``BackendModel`` and keeps per-module router facts between builds,
    so after a one-file edit only that file is re-parsed and re-analyzed."""

    def __init__(
        self,
        import_root: Path,
        package: str,
        app: str,
        repo_root: Path,
        *,
        sources: PySources | None = None,
        prefilter: bool = True,
        clock: Callable[[], int] = time.time_ns,
        include_model: IncludeModel | None = None,
        lock: str | None = None,
    ) -> None:
        """``include_model``: how ``include_router`` treats routes added later;
        by default the one for the FastAPI version ``<repo_root>/<lock>``
        locks, read on every build."""
        self.sources = sources or PySources(import_root, package, repo_root, clock=clock)
        self.app = app
        self.prefilter = prefilter
        self.repo_root = Path(repo_root)
        self.include_model = include_model
        self.lock = lock
        self._cache: dict[str, tuple[PyModule, ModuleAnalysis]] = {}
        self._prefilter_cache: dict[str, tuple[PyModule, router_bases.PrefilterFacts]] = {}

    def _prefilter_facts(self, mod: PyModule) -> router_bases.PrefilterFacts:
        cached = self._prefilter_cache.get(mod.name)
        if cached is not None and cached[0] is mod:
            return cached[1]
        found = router_bases.prefilter_facts(mod)
        self._prefilter_cache[mod.name] = (mod, found)
        return found

    def _analyze(self, mod: PyModule) -> ModuleAnalysis:
        cached = self._cache.get(mod.name)
        if cached is not None and cached[0] is mod:
            return cached[1]
        analysis = router_bases.analyze(mod)
        self._cache[mod.name] = (mod, analysis)
        return analysis

    def build(self, visible: AbstractSet[str] | None = None) -> BackendModel:
        """``visible``: the git-visible files the build may read (``PySources.load``)."""
        sources = self.sources.load(visible)
        self._cache = {name: entry for name, entry in self._cache.items() if sources.modules.get(name) is entry[0]}
        model = self.include_model or include_models.read(self.repo_root, self.lock)
        return _build(
            sources, self.app, self._analyze, self._prefilter_facts, prefilter=self.prefilter, include_model=model
        )


def build_backend_model(
    import_root: Path,
    package: str,
    app: str,
    repo_root: Path,
    *,
    sources: PySources | None = None,
    prefilter: bool = True,
    include_model: IncludeModel | None = None,
    lock: str | None = None,
    visible: AbstractSet[str] | None = None,
) -> BackendModel:
    """The router graph and live routes of ``app`` (``"pkg.main:app"``) in
    ``<import_root>/<package>``; file paths are relative to ``repo_root``.
    ``include_model`` defaults to the one ``<repo_root>/<lock>`` gives;
    ``visible`` limits the files read (``PySources.load``)."""
    return BackendBuilder(
        import_root,
        package,
        app,
        repo_root,
        sources=sources,
        prefilter=prefilter,
        include_model=include_model,
        lock=lock,
    ).build(visible)


def _build(
    sources: PySources,
    app: str,
    analyze: Callable[[PyModule], ModuleAnalysis],
    prefilter_facts: Callable[[PyModule], router_bases.PrefilterFacts],
    *,
    prefilter: bool,
    include_model: IncludeModel,
) -> BackendModel:
    builder = _Builder(
        sources, app, {}, analyze, prefilter=prefilter, prefilter_facts=prefilter_facts, include_model=include_model
    )
    builder.load_facts()
    app_module, _, app_name = app.partition(":")
    builder.import_order(app_module)
    root_results = builder.discover(app_module, app_name.split(".")[0])
    edges, escapes = builder.attach()
    roots, rest = builder.nodes_of(root_results)
    root = roots[0] if len(roots) == 1 and not rest else None
    emitter = _Emitter(builder, edges)
    routes = _app_routes(builder, emitter, root, rest, app)
    emitter.report_mount_shadows()
    unmounted = _unmounted_routes(builder, emitter, edges, root, app)
    _check_escapes(builder, escapes)
    ordered = [_with_order(route, k) for k, route in enumerate(_without_repeated_deferred(routes))]
    unmounted_ordered = [_with_order(route, k) for k, route in enumerate(_without_repeated_deferred(unmounted))]
    router_nodes = tuple(_router_node(n) for n in sorted(builder.nodes.values(), key=lambda n: (n.file, n.line, n.id)))
    edge_rows = tuple(_edge_row(e) for e in edges)
    problems = list(sources.problems)
    for analysis in builder._analyses.values():
        problems.extend(analysis.facts.problems)
    problems.extend(builder.problems)
    diagnostics, limitations = partition(problems)
    limitations.sort(key=lambda p: (p.file or "", p.line or 0, p.kind, p.message))
    return BackendModel(
        app_id=root.id if root else app,
        redirect_slashes=root.redirect_slashes if root else True,
        routes=tuple(ordered),
        unmounted_routes=tuple(unmounted_ordered),
        routers=router_nodes,
        edges=edge_rows,
        diagnostics=tuple(diagnostics),
        limitations=tuple(limitations),
        files_scanned=sources.files_scanned,
        sources=sources,
        resolver=builder.ev,
        include_model=include_model,
    )


def _app_routes(builder: _Builder, emitter: _Emitter, root: _Node | None, rest: list, app: str) -> list[LiveRoute]:
    routes: list[LiveRoute] = []
    if root is not None:
        root.included = True
        emitter.emit(root, "", (), (root.id,), "static", None, "app", routes, ())
        return routes
    app_module = app.partition(":")[0]
    mod = builder.sources.module(app_module)
    reasons = "; ".join(sorted({str(r[1]) for r in rest if len(r) > 1 and r[1]})) or "not a FastAPI app"
    builder.problems.append(
        diagnostic(
            "unresolved_app",
            stage=STAGE,
            step="resolve app",
            file=mod.rel if mod else app_module,
            message=f"{app}: could not resolve the app ({reasons})",
        )
    )
    return routes


def _unmounted_routes(
    builder: _Builder, emitter: _Emitter, edges: list[_Edge], root: _Node | None, app: str
) -> list[LiveRoute]:
    """Routes of routers nothing the analysis follows includes, each router reported."""
    unmounted: list[LiveRoute] = []
    for node in sorted(builder.nodes.values(), key=lambda n: (n.file, n.line, n.id)):
        if node.included or node is root:
            continue
        if any(edge.child is node for edge in edges):
            continue  # reported through the router that includes it
        regs = sum(1 for _, event, _, _ in node.events if isinstance(event, RouteReg))
        mine: list[LiveRoute] = []
        emitter.emit(node, "", (), (node.id,), "static", None, "router", mine, ())
        if not mine and not regs:
            continue
        unmounted.extend(mine)
        builder._limit(
            "unmounted_router",
            node.file,
            node.line,
            f"router {node.id} has {len(mine)} route(s), but no include_router or mount the analysis can follow "
            f"reaches it from {app}; they are listed as unmounted, not live",
        )
    return unmounted


def _check_escapes(builder: _Builder, escapes: list[tuple[str, RouterEscape, _Frame | None, _Node]]) -> None:
    checker = _Escapes(builder)
    for module, event, frame, node in escapes:
        if not node.included:
            continue
        analysis = builder._analyses.get(module)
        arg = analysis.escape_args.get(event.seq) if analysis else None
        checker.check(module, event, arg, node, frame)


def _without_repeated_deferred(routes: list[LiveRoute]) -> list[LiveRoute]:
    """Drop a deferred route equal to an earlier one: a function nothing calls at
    import may be reached through several call chains, and each would list the
    same route that may run later or never."""
    seen: set[tuple] = set()
    out: list[LiveRoute] = []
    for route in routes:
        if route.mount == "deferred":
            key = (
                route.method,
                route.path,
                route.handler_id,
                route.registered_file,
                route.registered_line,
                route.chain,
                tuple((layer.via, layer.owner, _deps_key(layer.dependencies)) for layer in route.layers),
            )
            if key in seen:
                continue
            seen.add(key)
        out.append(route)
    return out


def _deps_key(dependencies: tuple[Located, ...] | Unknown) -> tuple:
    if isinstance(dependencies, Unknown):
        return ("unknown", dependencies.reason, dependencies.text)
    return tuple((loc.module, id(loc.node)) for loc in dependencies)


def _with_order(route: LiveRoute, order: int) -> LiveRoute:
    return replace(route, order=order)


def _router_node(node: _Node) -> RouterNode:
    return RouterNode(
        id=node.id,
        module=node.module,
        file=node.file,
        line=node.line,
        kind=node.ctor.kind,
        prefix=node.prefix,
        dependencies=node.dependencies,
        redirect_slashes=node.redirect_slashes,
        events=tuple(event for _, event, _, _ in node.events),
    )


def _edge_row(edge: _Edge) -> IncludeEdge:
    return IncludeEdge(
        parent=edge.parent.id,
        child=edge.child.id,
        prefix=edge.prefix,
        dependencies=edge.dependencies,
        file=edge.file,
        line=edge.line,
        seq=edge.event.seq,
        mount=edge.mount,
    )


def _nested_defs(fn: ast.AST, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every ``def name`` anywhere inside ``fn``, not counting ``fn`` itself."""
    return [
        stmt
        for stmt in ast.walk(fn)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name and stmt is not fn
    ]


def _rebinds(fn: ast.AST, name: str) -> bool:
    """Whether anything inside ``fn`` assigns or deletes the plain name ``name``."""
    return any(
        isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, (ast.Store, ast.Del)) for n in ast.walk(fn)
    )
