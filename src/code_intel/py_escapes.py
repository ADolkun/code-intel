"""Where a module-level value is used in a way that may change it in place.

A list, dict or set bound to a module name can be changed through any other
reference to it: a second name, a container that holds it, a function that
returns it, a loop variable, a walrus. Rather than follow each of those, the
analysis counts every use of the name as a possible change, except the uses
listed here as only reading it (the ``dependencies=`` of a FastAPI call, a loop
over it, ``len(...)``, a membership test, ...). A call is only taken for
FastAPI's or a builtin's when every binding of its name in the module says so;
calling a class is a use, since an instance shares the class's attributes.
``collect_escapes`` finds the other uses; the value evaluator treats a
container name with one as unknown.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

# FastAPI calls that copy (or only read) the containers given to them.
FASTAPI_CALLS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "options",
        "head",
        "trace",
        "api_route",
        "websocket",
        "add_api_route",
        "add_api_websocket_route",
        "include_router",
        "APIRouter",
        "FastAPI",
    }
)
# Where those calls are confirmed to come from: the app and router classes, by
# import, and the methods, on a name bound only to an instance of one.
FASTAPI_MODULES = frozenset({"fastapi", "fastapi.routing", "fastapi.applications"})
FASTAPI_CLASSES = frozenset({"APIRouter", "FastAPI"})
FASTAPI_METHODS = FASTAPI_CALLS - FASTAPI_CLASSES
# Keywords of those calls whose value FastAPI copies or reads.
FASTAPI_READ_KEYWORDS = frozenset({"dependencies", "prefix", "tags", "responses"})
# Builtins that read their argument and return something that does not share it.
READ_BUILTINS = frozenset(
    {
        "len",
        "bool",
        "list",
        "tuple",
        "set",
        "frozenset",
        "sorted",
        "reversed",
        "iter",
        "enumerate",
        "any",
        "all",
        "sum",
        "min",
        "max",
        "isinstance",
        "print",
        "repr",
        "str",
        "id",
        "hash",
        "type",
    }
)
# Calls whose result shares the argument's elements (``dict(m)``, ``m.copy()``,
# ``m.get(k)``, ``m.values()``): the result is followed like the value itself.
ELEMENT_BUILTINS = frozenset({"dict"})
ELEMENT_METHODS = frozenset({"copy", "get", "values", "items"})
# Methods that only read.
READ_METHODS = frozenset({"count", "index", "keys", "__contains__", "__len__"})
# Nodes whose operand is only read.
_READING_PARENTS = (
    ast.Compare,
    ast.UnaryOp,
    ast.BinOp,
    ast.FormattedValue,
    ast.JoinedStr,
    ast.Expr,
    ast.Assert,
    ast.If,
    ast.While,
)
# How far a use is followed up through elements, attributes and ``or``.
_MAX_FOLLOW = 32


@dataclass(frozen=True)
class Escapes:
    at_import: Mapping[str, list[tuple[int, int]]]  # name -> sorted positions of uses that run at import
    anytime: frozenset[str]  # names with such a use inside a function or lambda body
    refs: tuple[tuple[str, tuple[str, ...]], ...]  # (root name, attributes) of every such use


def collect_escapes(tree: ast.Module) -> Escapes:
    """Every use of a module-level name that is not known to only read it."""
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    ctx = _Context(parents, Names.of(tree, parents))
    at_import: dict[str, list[tuple[int, int]]] = {}
    anytime: set[str] = set()
    refs: list[tuple[str, tuple[str, ...]]] = []
    for name, deferred in _module_name_uses(tree):
        if _reads_only(name, ctx):
            continue
        refs.append((name.id, _attribute_chain(name, parents)))
        if deferred:
            anytime.add(name.id)
        else:
            at_import.setdefault(name.id, []).append((name.lineno, name.col_offset))
    for positions in at_import.values():
        positions.sort()
    return Escapes(at_import, frozenset(anytime), tuple(dict.fromkeys(refs)))


# ── What a name may be bound to ──────────────────────────────────────────

_TYPE_PARAMS = frozenset({"TypeVar", "ParamSpec", "TypeVarTuple"})
_OTHER: tuple = ("other",)


@dataclass(frozen=True)
class Names:
    """Every binding of each name anywhere in a tree, in any scope: ``("def",)``,
    ``("class",)``, ``("import", dotted)``, ``("from", module, name)``,
    ``("value", expr)`` for ``name = expr``, or ``("other",)``. Deciding from
    every binding in every scope over-approximates which object a use is, so a
    name confirmed to be a builtin, a function, or FastAPI's is one in every
    scope."""

    bindings: Mapping[str, tuple[tuple, ...]]
    star: bool  # a star import may bind any name

    @staticmethod
    def of(tree: ast.AST, parents: dict[int, ast.AST]) -> Names:
        found: dict[str, list[tuple]] = {}
        star = False
        for node in ast.walk(tree):
            star |= isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
            for name, binding in _bindings(node, parents):
                found.setdefault(name, []).append(binding)
        return Names({k: tuple(v) for k, v in found.items()}, star)

    def builtin(self, name: str) -> bool:
        """Whether ``name`` can only be the builtin of that name."""
        return not self.star and name not in self.bindings

    def only(self, name: str, test: Callable[[tuple], bool]) -> bool:
        bindings = self.bindings.get(name)
        return bool(bindings) and all(test(b) for b in bindings)


def _bindings(node: ast.AST, parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    """``(name, binding)`` for each name ``node`` binds."""
    rule = _BINDING_RULES.get(type(node))
    if rule is not None:
        return rule(node, parents)
    if type(node).__name__ in _TYPE_PARAMS:
        return [(node.name, _OTHER)]  # type: ignore[attr-defined]
    return []


def _def_binding(node: ast.FunctionDef | ast.AsyncFunctionDef, _parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    return [(node.name, ("def",))]


def _class_binding(node: ast.ClassDef, _parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    return [(node.name, ("class",))]


def _name_binding(node: ast.Name, parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    if not isinstance(node.ctx, (ast.Store, ast.Del)):
        return []
    return [(node.id, _assigned(node, parents.get(id(node))))]


def _import_binding(node: ast.Import, _parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    """``import a.b as c`` binds ``c`` to ``a.b``; ``import a.b`` binds ``a`` to ``a``."""
    out: list[tuple[str, tuple]] = []
    for alias in node.names:
        bound = alias.asname or alias.name.split(".")[0]
        out.append((bound, ("import", alias.name if alias.asname else bound)))
    return out


def _from_binding(node: ast.ImportFrom, _parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    module = node.module if not node.level else None
    return [(a.asname or a.name, ("from", module, a.name)) for a in node.names if a.name != "*"]


def _other_binding(node: ast.AST, _parents: dict[int, ast.AST]) -> list[tuple[str, tuple]]:
    """Parameters, ``except ... as e``, and ``match`` capture names."""
    name = node.arg if isinstance(node, ast.arg) else node.rest if isinstance(node, ast.MatchMapping) else node.name  # type: ignore[attr-defined]
    return [(name, _OTHER)] if isinstance(name, str) else []


_BINDING_RULES: dict[type, Callable[[Any, dict[int, ast.AST]], list[tuple[str, tuple]]]] = {
    ast.FunctionDef: _def_binding,
    ast.AsyncFunctionDef: _def_binding,
    ast.ClassDef: _class_binding,
    ast.Name: _name_binding,
    ast.Import: _import_binding,
    ast.ImportFrom: _from_binding,
    ast.arg: _other_binding,
    ast.ExceptHandler: _other_binding,
    ast.MatchAs: _other_binding,
    ast.MatchStar: _other_binding,
    ast.MatchMapping: _other_binding,
}


def _assigned(target: ast.Name, parent: ast.AST | None) -> tuple:
    if isinstance(parent, ast.Assign) and parent.targets == [target]:
        return ("value", parent.value)
    if isinstance(parent, ast.AnnAssign) and parent.target is target and parent.value is not None:
        return ("value", parent.value)
    return _OTHER


def _imported_module(node: ast.expr, names: Names) -> str | None:
    """The dotted module ``node`` is, when every binding of its root is an ``import``."""
    if isinstance(node, ast.Attribute):
        base = _imported_module(node.value, names)
        return f"{base}.{node.attr}" if base else None
    if not isinstance(node, ast.Name):
        return None
    modules = {b[1] if b[0] == "import" else None for b in names.bindings.get(node.id, ())}
    return modules.pop() if len(modules) == 1 else None


def fastapi_class(func: ast.expr, names: Names) -> bool:
    """Whether ``func`` is FastAPI's ``APIRouter`` or ``FastAPI`` class."""
    if isinstance(func, ast.Name):
        return names.only(func.id, lambda b: b[0] == "from" and b[1] in FASTAPI_MODULES and b[2] in FASTAPI_CLASSES)
    if isinstance(func, ast.Attribute) and func.attr in FASTAPI_CLASSES:
        return _imported_module(func.value, names) in FASTAPI_MODULES
    return False


def _router_name(name: str, names: Names) -> bool:
    """Whether every binding of ``name`` is a new FastAPI router or app."""
    return names.only(
        name, lambda b: b[0] == "value" and isinstance(b[1], ast.Call) and fastapi_class(b[1].func, names)
    )


def is_fastapi_call(call: ast.Call, names: Names) -> bool:
    """A route, include, router or app call confirmed to be FastAPI's: the class
    by import, or a method on a name bound only to a new router or app."""
    func = call.func
    if fastapi_class(func, names):
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr in FASTAPI_METHODS
        and isinstance(func.value, ast.Name)
        and _router_name(func.value.id, names)
    )


# ── Uses ─────────────────────────────────────────────────────────────────


def _module_name_uses(tree: ast.Module) -> Iterator[tuple[ast.Name, bool]]:
    """``(name load, inside a function body)`` for each use of a module-level
    name: a name a function binds locally is not one."""
    todo: list[tuple[ast.AST, frozenset[str], bool]] = [(tree, frozenset(), False)]
    while todo:
        node, shadowed, deferred = todo.pop()
        if isinstance(node, _FUNCTION_TYPES):
            todo.extend(_function_parts(node, shadowed, deferred))
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in shadowed:
            yield node, deferred
        todo.extend((child, shadowed, deferred) for child in ast.iter_child_nodes(node))


def _function_parts(
    node: ast.AST, shadowed: frozenset[str], deferred: bool
) -> list[tuple[ast.AST, frozenset[str], bool]]:
    """Decorators and defaults run where the ``def`` is; the body later, with
    the function's own names hiding the module's."""
    args = node.args  # type: ignore[attr-defined]
    local = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    local.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    body = node.body if isinstance(node.body, list) else [node.body]  # type: ignore[attr-defined]
    declared: set[str] = set()
    todo: list[ast.AST] = list(body)
    while todo:
        found = todo.pop()
        if isinstance(found, (ast.Global, ast.Nonlocal)):
            declared.update(found.names)
        if _binds(found, local):
            todo.extend(ast.iter_child_nodes(found))
    inner = frozenset((shadowed | local) - declared)
    outer = [*getattr(node, "decorator_list", ()), *args.defaults, *(d for d in args.kw_defaults if d is not None)]
    return [*((child, shadowed, deferred) for child in outer), *((child, inner, True) for child in body)]


def _binds(node: ast.AST, local: set[str]) -> bool:
    """Add the names ``node`` binds in the function's scope to ``local``; False
    when its children are in a scope of their own."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        local.add(node.name)  # a nested scope's own names are not this function's
        return False
    if isinstance(node, ast.Lambda):
        return False
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        local.add(node.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        local.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
    return True


def _attribute_chain(name: ast.Name, parents: dict[int, ast.AST]) -> tuple[str, ...]:
    attrs: list[str] = []
    node: ast.AST = name
    parent = parents.get(id(node))
    while isinstance(parent, ast.Attribute) and parent.value is node:
        attrs.append(parent.attr)
        node, parent = parent, parents.get(id(parent))
    return tuple(attrs)


@dataclass(frozen=True)
class _Context:
    parents: dict[int, ast.AST]
    names: Names


# Names of the trees ``reads_only`` was last asked about, keyed by the parents map.
_CONTEXTS: dict[int, tuple[dict[int, ast.AST], _Context]] = {}
_MAX_CONTEXTS = 64


def reads_only(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Whether the value ``node`` evaluates to is only read where it is used,
    judged within the tree ``parents`` maps: followed up through attribute
    loads, element loads, ``or``/``and`` and calls whose result shares its
    elements."""
    cached = _CONTEXTS.get(id(parents))
    if cached is None or cached[0] is not parents:
        root = node
        while id(root) in parents:
            root = parents[id(root)]
        if len(_CONTEXTS) >= _MAX_CONTEXTS:
            _CONTEXTS.clear()
        cached = _CONTEXTS[id(parents)] = (parents, _Context(parents, Names.of(root, parents)))
    return _reads_only(node, cached[1])


def _reads_only(node: ast.AST, ctx: _Context) -> bool:
    for _ in range(_MAX_FOLLOW):
        parent = ctx.parents.get(id(node))
        step = _use(node, parent, ctx)
        if isinstance(step, bool):
            return step
        node = step
    return False


def _use(node: ast.AST, parent: ast.AST | None, ctx: _Context) -> bool | ast.AST:
    """True when ``parent`` only reads ``node``, False when it may change it or
    hand it on, or the node whose use decides it."""
    if parent is None or isinstance(parent, _READING_PARENTS):
        return True
    rule = _USE_RULES.get(type(parent))
    return rule(node, parent, ctx) if rule is not None else False


def _subscript_use(node: ast.AST, parent: ast.Subscript, _ctx: _Context) -> bool | ast.AST:
    if parent.value is not node:
        return True  # an index or key
    return parent if isinstance(parent.ctx, ast.Load) else False


def _choice_use(node: ast.AST, parent: ast.BoolOp | ast.IfExp, _ctx: _Context) -> bool | ast.AST:
    """``a or b`` and ``a if t else b`` evaluate to an operand, followed; the test is only read."""
    return True if isinstance(parent, ast.IfExp) and parent.test is node else parent


def _loop_use(node: ast.AST, parent: ast.For | ast.AsyncFor | ast.comprehension, _ctx: _Context) -> bool:
    return parent.iter is node


def _keyword_use(_node: ast.AST, parent: ast.keyword, ctx: _Context) -> bool:
    call = ctx.parents.get(id(parent))
    reads = parent.arg is None or parent.arg in FASTAPI_READ_KEYWORDS
    return reads and isinstance(call, ast.Call) and is_fastapi_call(call, ctx.names)


def _dict_use(node: ast.AST, parent: ast.Dict, _ctx: _Context) -> bool:
    return any(key is node for key in parent.keys)  # a key is hashed, a value is stored


def _attribute_use(_node: ast.AST, attribute: ast.Attribute, ctx: _Context) -> bool | ast.AST:
    if not isinstance(attribute.ctx, ast.Load):
        return False  # `value.attr = v` changes the value
    call = ctx.parents.get(id(attribute))
    if not (isinstance(call, ast.Call) and call.func is attribute):
        return attribute  # an attribute of it, followed
    if attribute.attr in READ_METHODS:
        return True
    return call if attribute.attr in ELEMENT_METHODS else False


def _starred_use(_node: ast.AST, starred: ast.Starred, ctx: _Context) -> bool:
    """``[*value]`` copies the elements; ``f(*value)`` hands them on."""
    holder = ctx.parents.get(id(starred))
    if isinstance(holder, (ast.List, ast.Tuple, ast.Set)):
        return True
    return isinstance(holder, ast.Call) and is_fastapi_call(holder, ctx.names)


def _argument_use(node: ast.AST, call: ast.Call, ctx: _Context) -> bool | ast.AST:
    names = ctx.names
    if call.func is node:
        # Calling a function or a builtin only reads it; calling a class makes an
        # instance that shares the class's attributes, and anything else may be one.
        return isinstance(node, ast.Name) and (names.builtin(node.id) or names.only(node.id, _is_def))
    func = call.func
    if not (isinstance(func, ast.Name) and names.builtin(func.id)):
        return False
    if func.id in READ_BUILTINS:
        return True
    return call if func.id in ELEMENT_BUILTINS else False


def _is_def(binding: tuple) -> bool:
    return binding[0] == "def"


# How each kind of parent node uses its child; a parent not listed may change it.
_USE_RULES: dict[type, Callable[[Any, Any, _Context], bool | ast.AST]] = {
    ast.Attribute: _attribute_use,
    ast.Subscript: _subscript_use,
    ast.BoolOp: _choice_use,
    ast.IfExp: _choice_use,
    ast.For: _loop_use,
    ast.AsyncFor: _loop_use,
    ast.comprehension: _loop_use,
    ast.Starred: _starred_use,
    ast.keyword: _keyword_use,
    ast.Call: _argument_use,
    ast.Dict: _dict_use,
}


def mutable_value(value: ast.expr | None) -> bool:
    """Whether a binding's value may be a container changed in place: anything
    but a constant, an f-string, a tuple display, a ``tuple(...)`` or
    ``frozenset(...)`` copy, or another name (followed to its own binding)."""
    if value is None or isinstance(value, (ast.Constant, ast.JoinedStr, ast.Tuple, ast.Name, ast.Attribute)):
        return False
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id not in ("tuple", "frozenset", "str", "int", "float", "bool")
    return True
