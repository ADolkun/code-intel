"""The shared constant and value evaluator for backend analysis.

``ValueEvaluator`` implements ``backend_model.NameResolver``. It resolves names
through position-aware module bindings (``py_sources.ModuleScope``), imports,
re-export barrels and ``from x import *``, and evaluates the values route and
dependency analysis need: strings (route paths, prefixes), sequences
(``dependencies=``), mappings (``**kwargs``) and booleans. Anything it cannot
determine is an ``Unknown`` naming the reason and the source text, never a guess:
a value changed in place (``X.append(...)``, ``X[k] = v``, ``X.attr = v``, also
from another module or inside a function), an instance attribute a method or an
outside base class may set, or a name local to the function the code runs in.

Code inside a function body passes a ``LocalLookup``: it maps a name local to
that function to the expression it holds (with the lookup to evaluate that
expression in), or to ``Unknown``; None means the name is a module name.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from code_intel import py_escapes
from code_intel.ast_nodes import start_position
from code_intel.backend_model import Def, External, Located, ModuleRef, Target, Unknown, Value
from code_intel.py_sources import PySources

# What a local name holds: (value, lookup to evaluate it in), with a third
# element True when the function may change that container in place.
type LocalFound = tuple[Located, LocalLookup | None] | tuple[Located, LocalLookup | None, bool]
type LocalLookup = Callable[[str], LocalFound | Unknown | None]

_BUILTINS = frozenset(dir(builtins))
# Binding kinds bound to a module, whose attributes are checked where they are defined.
_IMPORT_KINDS = frozenset({"import", "from_import", "star_import"})
# Why a container name has no value: a use that may change it in place.
ESCAPED_REASON = "may be changed in place: used where it is aliased, stored, returned or passed on"
_SEQUENCE_BUILTINS = frozenset({"builtins.list", "builtins.tuple"})
# Binding kinds whose value is not an expression the evaluator can follow.
_OPAQUE_KINDS = {
    "augmented_assign": "augmented assignment",
    "for": "loop target",
    "with": "with target",
    "except": "exception name",
    "match": "match capture",
}


def lambda_qualname(node: ast.Lambda) -> str:
    return f"<lambda>@L{node.lineno}"


class _Limit(Exception):
    """Raised inside one evaluation when the cycle or depth guard trips."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ValueEvaluator:
    """Resolve names and evaluate constant values across the analyzed package.

    ``at`` (or ``Located.at``) is the top-level statement index of the use, so
    a name sees only the bindings that ran before it; None means module end,
    for code that runs after import. Several reaching bindings give several
    targets, and callers treat more than one distinct target as ambiguous.
    The guard is the set of evaluations in progress plus ``max_depth``, the
    deepest nesting of evaluations (about one level per alias or import hop):
    a cycle or a deeper chain gives ``Unknown("cycle" | "depth", text)``.
    """

    def __init__(self, sources: PySources, *, max_depth: int = 32) -> None:
        self.sources = sources
        self.max_depth = max_depth
        self._active: set[tuple] = set()
        self._depth = 0
        # Nonzero while a value (string, sequence, mapping, boolean) is evaluated:
        # then a name whose value may have been changed in place has no value.
        self._values = 0
        # What each value evaluation in progress builds, innermost last: True for
        # a list, tuple or mapping, whose contents another use may change.
        self._containers: list[bool] = []

    @contextmanager
    def _value_mode(self, container: bool = False) -> Iterator[None]:
        self._values += 1
        self._containers.append(container)
        try:
            yield
        finally:
            self._values -= 1
            self._containers.pop()

    # ── Guard ────────────────────────────────────────────────────────────

    @contextmanager
    def _enter(self, key: tuple) -> Iterator[None]:
        if key in self._active:
            raise _Limit("cycle")
        if self._depth >= self.max_depth:
            raise _Limit("depth")
        self._active.add(key)
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            self._active.discard(key)

    def _text(self, module: str, node: ast.AST) -> str:
        return self.sources.text(module, node)

    # ── Names ────────────────────────────────────────────────────────────

    def resolve_name(self, module: str, name: str, at: int | None, node: ast.AST | None = None) -> list[Target]:
        """Every target a module-level ``name`` may hold at a use.

        ``node`` (optional) places the use inside top-level statement ``at``.
        Imports are followed to the defining module; an assignment gives
        ``Value`` of its right-hand side (``resolve_expr`` follows aliases).
        """
        try:
            with self._enter(("name", module, name, at, start_position(node))):
                return self._resolve_name(module, name, at, node)
        except _Limit as limit:
            if self._depth:
                raise
            return [Unknown(limit.reason, name)]

    def _resolve_name(self, module: str, name: str, at: int | None, node: ast.AST | None) -> list[Target]:
        if self.sources.module(module) is None:
            if self.sources.has_module(f"{module}.{name}"):
                return [ModuleRef(f"{module}.{name}")]
            return [Unknown("missing module", f"{module}.{name}")]
        out: list[Target] = []
        reaching = self.sources.scope(module).reaching(name, at, node)
        changed = self._changed(module, name, reaching, at, node) if self._values and reaching else None
        if changed is not None:
            return [changed]
        for binding in reaching:
            out.extend(self._binding_targets(module, name, binding))
        if not out:
            if self.sources.has_module(f"{module}.{name}"):
                # A submodule is an attribute of its package once imported.
                return [ModuleRef(f"{module}.{name}")]
            if name in _BUILTINS:
                return [External(f"builtins.{name}")]
            return [Unknown("unbound", name)]
        return _unique(out)

    def _changed(self, module: str, name: str, reaching, at: int | None, node: ast.AST | None) -> Unknown | None:
        """Why the value bound to ``name`` may have been changed in place by the
        time ``at`` runs, if it may."""
        if any(b.kind not in _IMPORT_KINDS for b in reaching) and self.sources.scope(module).mutated(name, at, node):
            return Unknown("changed in place", name)
        if self.sources.changed_elsewhere(module, name):
            return Unknown("changed in place by another module", f"{module}.{name}")
        if self._containers and self._containers[-1] and self._container_escaped(module, name, reaching, at, node):
            return Unknown(ESCAPED_REASON, name)
        return None

    def _container_escaped(self, module: str, name: str, reaching, at: int | None, node: ast.AST | None) -> bool:
        """Whether a container bound to ``name`` may have been changed in place
        through a use of the name that does not only read it (``py_escapes``)."""
        if not any(
            (b.kind in ("assign", "annotated_assign", "walrus") and py_escapes.mutable_value(b.value))
            or b.kind == "class"
            for b in reaching
        ):
            return False
        return self.sources.scope(module).escaped(name, at, node) or self.sources.escaped_elsewhere(module, name)

    def _binding_targets(self, module: str, name: str, binding) -> list[Target]:
        kind = binding.kind
        if kind in ("def", "class"):
            return [Def(module, name, binding.node)]
        if kind == "type_alias":
            # At runtime the name holds a TypeAliasType, never the aliased value, so
            # the statement itself is the value: nothing follows it to what it aliases,
            # across imports included, and consumers that unpack aliases (FastAPI
            # unpacks one level) decide how far to go.
            return [Value(Located(module, binding.node, binding.index))]
        if kind in ("assign", "annotated_assign", "walrus"):
            if binding.value is None:
                return [Unknown("unpacked value", name)]
            return [Value(Located(module, binding.value, binding.index))]
        if kind in _OPAQUE_KINDS:
            return [Unknown(_OPAQUE_KINDS[kind], name)]
        if kind == "import":
            dotted = binding.imported_from or ""
            return [self._module_target(dotted)]
        if kind == "from_import":
            if binding.imported_from is None:
                return [Unknown("relative import above the package", name)]
            if binding.imported_from == module:
                # `from . import sub` in a package's own __init__ imports the submodule.
                sub = f"{module}.{binding.imported_name or name}"
                return [ModuleRef(sub) if self.sources.has_module(sub) else Unknown("missing module", sub)]
            return self._import_member(binding.imported_from, binding.imported_name or name)
        if kind == "star_import":
            source = binding.imported_from
            if source is None:
                return [Unknown("relative import above the package", name)]
            if self.sources.module(source) is None:
                return [Unknown("star import from outside the package", f"{source}.*")]
            exports = self.sources.scope(source).exports()
            if exports is not None and name not in exports and not self.sources.changed_elsewhere(source, "__all__"):
                return []
            return self._import_member(source, name, star=True)
        return [Unknown(f"binding kind {kind}", name)]

    def _module_target(self, dotted: str) -> Target:
        if self.sources.has_module(dotted):
            return ModuleRef(dotted)
        if self.sources.in_package(dotted):
            return Unknown("missing module", dotted)
        return External(dotted)

    def _import_member(self, source: str, name: str, *, star: bool = False) -> list[Target]:
        """``from source import name``: the module's binding, else its submodule."""
        if self.sources.module(source) is not None:
            targets = self.resolve_name(source, name, None)
            unbound = len(targets) == 1 and isinstance(targets[0], Unknown) and targets[0].reason == "unbound"
            if not unbound:
                return self._follow(targets)
            if star:
                return []
        if not star and self.sources.has_module(f"{source}.{name}"):
            return [ModuleRef(f"{source}.{name}")]
        if self.sources.has_module(source) or self.sources.in_package(source):
            return [Unknown("missing name", f"{source}.{name}")]
        return [External(f"{source}.{name}")]

    # ── Expressions ──────────────────────────────────────────────────────

    def resolve_expr(self, loc: Located) -> list[Target]:
        """Targets of an expression: names and attribute chains are resolved and
        aliases followed; any other expression is ``Value`` of itself (a call
        result, a literal), and a lambda is a ``Def``."""
        try:
            with self._enter(("expr", loc.module, id(loc.node), loc.at)):
                return self._resolve_expr(loc)
        except _Limit as limit:
            if self._depth:
                raise
            return [Unknown(limit.reason, self._text(loc.module, loc.node))]

    def _resolve_expr(self, loc: Located) -> list[Target]:
        node = loc.node
        if isinstance(node, ast.Name):
            return self._follow(self.resolve_name(loc.module, node.id, loc.at, node))
        if isinstance(node, ast.Attribute):
            out: list[Target] = []
            for base in self.resolve_expr(Located(loc.module, node.value, loc.at)):
                out.extend(self._attribute(base, node.attr, loc))
            return _unique(out)
        if isinstance(node, ast.Lambda):
            return [Def(loc.module, lambda_qualname(node), node)]
        if isinstance(node, ast.NamedExpr):
            return self.resolve_expr(Located(loc.module, node.value, loc.at))
        return [Value(loc)]

    def _follow(self, targets: list[Target]) -> list[Target]:
        """Follow ``Value`` targets that are names or attributes (aliases), and turn lambdas into ``Def``."""
        out: list[Target] = []
        for target in targets:
            if isinstance(target, Value) and isinstance(
                target.loc.node, (ast.Name, ast.Attribute, ast.NamedExpr, ast.Lambda)
            ):
                out.extend(self.resolve_expr(target.loc))
            else:
                out.append(target)
        return _unique(out)

    def _attribute(self, base: Target, attr: str, loc: Located) -> list[Target]:
        if isinstance(base, ModuleRef):
            if self.sources.module(base.name) is not None:
                targets = self.resolve_name(base.name, attr, None)
                if not (len(targets) == 1 and isinstance(targets[0], Unknown) and targets[0].reason == "unbound"):
                    return self._follow(targets)
            if self.sources.has_module(f"{base.name}.{attr}"):
                return [ModuleRef(f"{base.name}.{attr}")]
            return [Unknown("missing attribute", f"{base.name}.{attr}")]
        if isinstance(base, External):
            return [External(f"{base.dotted}.{attr}")]
        if isinstance(base, Def):
            if isinstance(base.node, ast.ClassDef):
                return self._class_member(base, attr, set())
            return [Unknown("function attribute", f"{base.qualname}.{attr}")]
        if isinstance(base, Value) and isinstance(base.loc.node, ast.Call):
            # An instance attribute: `svc.method` where `svc = Svc()`.
            out: list[Target] = []
            for cls in self.resolve_expr(Located(base.loc.module, base.loc.node.func, base.loc.at)):
                if isinstance(cls, Def) and isinstance(cls.node, ast.ClassDef):
                    why = self._instance_attribute_set(cls, attr, set())
                    if why is not None:
                        out.append(Unknown(why, self._text(loc.module, loc.node)))
                    else:
                        out.extend(self._class_member(cls, attr, set()))
                else:
                    out.append(Unknown("attribute of a call result", self._text(loc.module, loc.node)))
            return _unique(out)
        if isinstance(base, Unknown):
            return [base]
        return [Unknown("attribute of a value", self._text(loc.module, loc.node))]

    def _class_member(self, cls: Def, attr: str, seen: set[str]) -> list[Target]:
        """``Class.attr``: the last binding of ``attr`` in the class body, else a base class's."""
        key = f"{cls.module}:{cls.qualname}"
        if key in seen:
            return [Unknown("cycle", f"{cls.qualname}.{attr}")]
        seen.add(key)
        node = cls.node
        assert isinstance(node, ast.ClassDef)
        found: Target | None = None
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == attr:
                found = Def(cls.module, f"{cls.qualname}.{attr}", stmt)
            elif isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in stmt.targets):
                found = Value(Located(cls.module, stmt.value, None))
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == attr
                and stmt.value is not None
            ):
                found = Value(Located(cls.module, stmt.value, None))
        if found is not None:
            return self._follow([found])
        for base_expr in node.bases:
            for base in self.resolve_expr(Located(cls.module, base_expr, None)):
                if isinstance(base, Def) and isinstance(base.node, ast.ClassDef):
                    targets = self._class_member(base, attr, seen)
                    if targets and not all(isinstance(t, Unknown) for t in targets):
                        return targets
        return [Unknown("class attribute", f"{cls.qualname}.{attr}")]

    def _instance_attribute_set(self, cls: Def, attr: str, seen: set[str]) -> str | None:
        """Why an instance of ``cls`` may hold an ``attr`` other than the class's:
        a method assigns ``self.<attr>`` (or calls ``setattr``), or, for values, a
        class decorator or a base outside the package builds the instance."""
        key = f"{cls.module}:{cls.qualname}"
        if key in seen:
            return None
        seen.add(key)
        node = cls.node
        assert isinstance(node, ast.ClassDef)
        if self._values and node.decorator_list:
            return f"instance attribute of decorated class {cls.qualname}"
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                why = _method_sets(cls, stmt, attr)
                if why is not None:
                    return why
        for base_expr in node.bases:
            for base in self.resolve_expr(Located(cls.module, base_expr, None)):
                if isinstance(base, Def) and isinstance(base.node, ast.ClassDef):
                    why = self._instance_attribute_set(base, attr, seen)
                    if why is not None:
                        return why
                elif self._values and not (isinstance(base, External) and base.dotted == "builtins.object"):
                    return f"instance attribute of {cls.qualname}, whose base {self._text(cls.module, base_expr)} is outside the package"
        return None

    # ── Values ───────────────────────────────────────────────────────────

    def local_value(self, node: ast.AST, local: LocalLookup | None) -> LocalFound | Unknown | None:
        """What a name, or the root name of an attribute chain, local to the
        running function holds; None for module names."""
        if local is None:
            return None
        if isinstance(node, ast.Name):
            return local(node.id)
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            found = local(root.id)
            if isinstance(found, Unknown):
                return found
            if found is not None:
                return Unknown(f"attribute of local name {root.id}", ast.unparse(node))
        return None

    def _single_value(self, loc: Located, what: str) -> Located | Unknown:
        """The one expression a name or attribute holds, or why there isn't one."""
        values: list[Located] = []
        for target in self.resolve_expr(loc):
            if isinstance(target, Unknown):
                return target
            if not isinstance(target, Value):
                return Unknown(f"not a {what}", self._text(loc.module, loc.node))
            values.append(target.loc)
        if not values:
            return Unknown("unbound", self._text(loc.module, loc.node))
        if len(values) > 1:
            dumps = {ast.dump(v.node) for v in values}
            modules = {v.module for v in values}
            if len(dumps) > 1 or len(modules) > 1:
                return Unknown("ambiguous", self._text(loc.module, loc.node))
        return values[0]

    def string(self, loc: Located, local: LocalLookup | None = None) -> str | Unknown:
        """A ``str`` value: literals, names and attributes bound to one, ``+``,
        and f-strings whose every part is known (no conversion or format spec)."""
        try:
            with self._enter(("string", loc.module, id(loc.node), loc.at, id(local))), self._value_mode():
                return self._string(loc, local)
        except _Limit as limit:
            if self._depth:
                raise
            return Unknown(limit.reason, self._text(loc.module, loc.node))

    def _string(self, loc: Located, local: LocalLookup | None) -> str | Unknown:
        node = loc.node
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(loc, local, "string", self.string)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            return Unknown("not a string", self._text(loc.module, node))
        if isinstance(node, ast.JoinedStr):
            return self._joined_string(loc, node, local)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.string(Located(loc.module, node.left, loc.at), local)
            if isinstance(left, Unknown):
                return left
            right = self.string(Located(loc.module, node.right, loc.at), local)
            if isinstance(right, Unknown):
                return right
            return left + right
        return Unknown("not a string", self._text(loc.module, node))

    def _joined_string(self, loc: Located, node: ast.JoinedStr, local: LocalLookup | None) -> str | Unknown:
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue) and value.conversion == -1 and value.format_spec is None:
                part = self.string(Located(loc.module, value.value, loc.at), local)
                if isinstance(part, Unknown):
                    return part
                parts.append(part)
            else:
                return Unknown("formatted value", self._text(loc.module, node))
        return "".join(parts)

    def _named(self, loc: Located, local: LocalLookup | None, what: str, evaluate: Callable[..., Any]) -> Any:
        """A name or attribute evaluated with ``evaluate``: what the running
        function's local holds, else the one value the module binds to it."""
        found = self.local_value(loc.node, local)
        if isinstance(found, Unknown):
            return found
        if found is not None:
            if len(found) > 2 and found[2] and self._containers and self._containers[-1]:
                return Unknown(ESCAPED_REASON, self._text(loc.module, loc.node))
            return evaluate(found[0], found[1])
        value = self._single_value(loc, what)
        if isinstance(value, Unknown):
            return value
        return evaluate(value)

    def sequence(self, loc: Located, local: LocalLookup | None = None) -> tuple[Located, ...] | Unknown:
        """Elements of a list or tuple: literals, ``+`` concatenation, ``*starred``
        known sequences, ``list(...)``/``tuple(...)`` of one, and names bound to
        those. Each element keeps the module and statement index it was written at."""
        try:
            with self._enter(("sequence", loc.module, id(loc.node), loc.at, id(local))), self._value_mode(True):
                return self._sequence(loc, local)
        except _Limit as limit:
            if self._depth:
                raise
            return Unknown(limit.reason, self._text(loc.module, loc.node))

    def _sequence(self, loc: Located, local: LocalLookup | None) -> tuple[Located, ...] | Unknown:
        node = loc.node
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(loc, local, "sequence", self.sequence)
        if isinstance(node, (ast.List, ast.Tuple)):
            return self._sequence_literal(loc, node, local)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.sequence(Located(loc.module, node.left, loc.at), local)
            if isinstance(left, Unknown):
                return left
            right = self.sequence(Located(loc.module, node.right, loc.at), local)
            if isinstance(right, Unknown):
                return right
            return left + right
        if isinstance(node, ast.Call) and self._is_sequence_builtin_call(loc, node, local):
            return self.sequence(Located(loc.module, node.args[0], loc.at), local)
        return Unknown("not a sequence", self._text(loc.module, node))

    def _sequence_literal(
        self, loc: Located, node: ast.List | ast.Tuple, local: LocalLookup | None
    ) -> tuple[Located, ...] | Unknown:
        out: list[Located] = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                inner = self.sequence(Located(loc.module, elt.value, loc.at), local)
                if isinstance(inner, Unknown):
                    return inner
                out.extend(inner)
            else:
                out.append(Located(loc.module, elt, loc.at))
        return tuple(out)

    def _is_sequence_builtin_call(self, loc: Located, node: ast.Call, local: LocalLookup | None) -> bool:
        """``list(x)`` or ``tuple(x)`` with the builtin, not a local or package name."""
        if len(node.args) != 1 or node.keywords or isinstance(node.args[0], ast.Starred):
            return False
        if self.local_value(node.func, local) is not None:
            return False
        callee = self.resolve_expr(Located(loc.module, node.func, loc.at))
        return len(callee) == 1 and isinstance(callee[0], External) and callee[0].dotted in _SEQUENCE_BUILTINS

    def is_none(self, loc: Located, local: LocalLookup | None = None) -> bool | Unknown:
        """Whether a value is ``None``: the literal, or a name or attribute bound to it."""
        try:
            with self._enter(("none", loc.module, id(loc.node), loc.at, id(local))), self._value_mode():
                return self._is_none(loc, local)
        except _Limit as limit:
            if self._depth:
                raise
            return Unknown(limit.reason, self._text(loc.module, loc.node))

    def _is_none(self, loc: Located, local: LocalLookup | None) -> bool | Unknown:
        node = loc.node
        if isinstance(node, ast.Constant):
            return node.value is None
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(loc, local, "value", self.is_none)
        return False

    def mapping(self, loc: Located, local: LocalLookup | None = None) -> dict[str, Located] | Unknown:
        """Items of a mapping with string keys, for ``**kwargs``: dict literals
        (``**`` merges included), ``dict(k=v, **m)``, and names bound to those."""
        items = self.mapping_items(loc, local)
        return items if isinstance(items, Unknown) else {key: value[0] for key, value in items.items()}

    def mapping_items(
        self, loc: Located, local: LocalLookup | None = None
    ) -> dict[str, tuple[Located, LocalLookup | None]] | Unknown:
        """``mapping``, each value with the lookup to evaluate it in."""
        try:
            with self._enter(("mapping", loc.module, id(loc.node), loc.at, id(local))), self._value_mode(True):
                return self._mapping(loc, local)
        except _Limit as limit:
            if self._depth:
                raise
            return Unknown(limit.reason, self._text(loc.module, loc.node))

    def _mapping(
        self, loc: Located, local: LocalLookup | None
    ) -> dict[str, tuple[Located, LocalLookup | None]] | Unknown:
        node = loc.node
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(loc, local, "mapping", self.mapping_items)
        if isinstance(node, ast.Dict):
            return self._dict_literal(loc, node, local)
        if isinstance(node, ast.Call):
            return self._dict_call(loc, node, local)
        return Unknown("not a mapping", self._text(loc.module, node))

    def _dict_literal(
        self, loc: Located, node: ast.Dict, local: LocalLookup | None
    ) -> dict[str, tuple[Located, LocalLookup | None]] | Unknown:
        out: dict[str, tuple[Located, LocalLookup | None]] = {}
        for key, value in zip(node.keys, node.values, strict=False):
            if key is None:
                inner = self.mapping_items(Located(loc.module, value, loc.at), local)
                if isinstance(inner, Unknown):
                    return inner
                out.update(inner)
                continue
            text = self.string(Located(loc.module, key, loc.at), local)
            if isinstance(text, Unknown):
                return Unknown("non-string key", self._text(loc.module, key))
            out[text] = (Located(loc.module, value, loc.at), local)
        return out

    def _dict_call(
        self, loc: Located, node: ast.Call, local: LocalLookup | None
    ) -> dict[str, tuple[Located, LocalLookup | None]] | Unknown:
        """``dict(m, k=v, **other)`` with the builtin ``dict``."""
        if self.local_value(node.func, local) is not None:
            return Unknown("not a mapping", self._text(loc.module, node))
        callee = self.resolve_expr(Located(loc.module, node.func, loc.at))
        if callee != [External("builtins.dict")]:
            return Unknown("not a mapping", self._text(loc.module, node))
        if len(node.args) > 1:
            return Unknown("dict() with several positional arguments", self._text(loc.module, node))
        out: dict[str, tuple[Located, LocalLookup | None]] = {}
        # (key or None for a mapping to merge, value), in call order
        parts = [(None, arg) for arg in node.args] + [(keyword.arg, keyword.value) for keyword in node.keywords]
        for key, value in parts:
            if key is not None:
                out[key] = (Located(loc.module, value, loc.at), local)
                continue
            inner = self.mapping_items(Located(loc.module, value, loc.at), local)
            if isinstance(inner, Unknown):
                return inner
            out.update(inner)
        return out

    def boolean(self, loc: Located, local: LocalLookup | None = None) -> bool | Unknown:
        """A ``bool`` value: ``True``/``False``, ``not`` of one, and names bound to one."""
        try:
            with self._enter(("boolean", loc.module, id(loc.node), loc.at, id(local))), self._value_mode():
                return self._boolean(loc, local)
        except _Limit as limit:
            if self._depth:
                raise
            return Unknown(limit.reason, self._text(loc.module, loc.node))

    def _boolean(self, loc: Located, local: LocalLookup | None) -> bool | Unknown:
        node = loc.node
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = self.boolean(Located(loc.module, node.operand, loc.at), local)
            return inner if isinstance(inner, Unknown) else not inner
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named(loc, local, "boolean", self.boolean)
        return Unknown("not a constant bool", self._text(loc.module, node))


def _method_sets(cls: Def, method: ast.FunctionDef | ast.AsyncFunctionDef, attr: str) -> str | None:
    """Why ``method`` may set instance attribute ``attr``: it assigns
    ``self.<attr>`` or calls ``setattr``."""
    params = [*method.args.posonlyargs, *method.args.args]
    first = params[0].arg if params else None
    for sub in ast.walk(method):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.ctx, (ast.Store, ast.Del))
            and sub.attr == attr
            and isinstance(sub.value, ast.Name)
            and sub.value.id == first
        ):
            return f"instance attribute {attr} set by {cls.qualname}.{method.name}()"
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "setattr":
            return f"instance attributes set by setattr() in {cls.qualname}.{method.name}()"
    return None


# ── Decorators ───────────────────────────────────────────────────────────

_WRAPS = frozenset({"functools.wraps", "functools.update_wrapper"})


def opaque_decorator(resolver, module: str, expr: ast.expr, at: int | None) -> str | None:
    """Why a decorator from the analyzed package may hand FastAPI a wrapper
    instead of the decorated function, or None when it returns the function
    itself or a wrapper made with ``functools.wraps`` (FastAPI then reads the
    wrapped signature). Decorators from outside the package are assumed to
    keep the signature; ``resolver`` is a ``NameResolver``."""
    if isinstance(expr, ast.Call):
        targets = resolver.resolve_expr(Located(module, expr.func, at))
        if len(targets) != 1:
            return None
        factory = targets[0]
        if isinstance(factory, Def) and isinstance(factory.node, ast.ClassDef):
            return f"it is an instance of {factory.qualname}, which wraps the function"
        if not (isinstance(factory, Def) and isinstance(factory.node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            return None
        reasons = [_returned_decorator(resolver, factory, value) for value in _own_returns(factory.node)] or [
            f"{factory.qualname}() returns no decorator the analysis can read"
        ]
        found = [r for r in reasons if r is not None]
        return found[0] if found else None
    if not isinstance(expr, (ast.Name, ast.Attribute)):
        return None
    targets = resolver.resolve_expr(Located(module, expr, at))
    if len(targets) != 1 or not isinstance(targets[0], Def):
        return None
    target = targets[0]
    if isinstance(target.node, ast.ClassDef):
        if _class_updates_wrapper(resolver, target):
            return None
        return f"{target.qualname} is a class, whose instance wraps the function"
    if isinstance(target.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _decorator_function(resolver, target)
    return None


def _returned_decorator(resolver, factory: Def, value: ast.expr) -> str | None:
    """What a decorator factory returns must be a decorator that keeps the signature."""
    nested = _called_or_named_def(factory.node, value)
    if nested is not None:
        reason = _decorator_function(resolver, _nested(factory, nested))
        if reason is None or isinstance(value, ast.Name):
            return reason
    return f"{factory.qualname}() returns {ast.unparse(value)}, which the analysis cannot read as a decorator"


def _decorator_function(resolver, decorator: Def) -> str | None:
    fn = decorator.node
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    positional = [*fn.args.posonlyargs, *fn.args.args]
    wrapped = positional[0].arg if positional else None
    defaults = fn.args.defaults
    # `def deco(func=None, *, option=...)`: called with options, it returns the decorator.
    optional = bool(positional) and len(defaults) == len(positional) and _is_none(defaults[0])
    returns = _own_returns(fn)
    if not returns:
        return f"{decorator.qualname}() returns None"
    for value in returns:
        if isinstance(value, ast.Name) and value.id == wrapped:
            continue
        if isinstance(value, ast.Call) and _is_wraps(resolver, decorator.module, value):
            continue
        nested = _called_or_named_def(fn, value)
        if nested is not None:
            if isinstance(value, ast.Name) and (
                any(_is_wraps(resolver, decorator.module, d) for d in nested.decorator_list)
                or _updates_wrapper(resolver, decorator.module, fn, value.id)
            ):
                continue
            # `decorator(func)` returns what the inner decorator makes of the function;
            # a bare `decorator` is what a call with options returns.
            inner_ok = _decorator_function(resolver, _nested(decorator, nested)) is None
            if inner_ok and (isinstance(value, ast.Call) or optional):
                continue
            if isinstance(value, ast.Name):
                return f"{decorator.qualname}() returns {nested.name} without functools.wraps"
        return f"{decorator.qualname}() returns {ast.unparse(value)}, which the analysis cannot read"
    return None


def _nested(outer: Def, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Def:
    return Def(outer.module, f"{outer.qualname}.<locals>.{node.name}", node)


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _called_or_named_def(fn: ast.AST, value: ast.expr) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function defined in ``fn``'s body that ``value`` names, or calls."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return _nested_def(fn, value.func.id)
    if isinstance(value, ast.Name):
        return _nested_def(fn, value.id)
    return None


def _is_wraps(resolver, module: str, node: ast.expr) -> bool:
    """``functools.wraps(fn)``, ``functools.wraps(fn)(w)`` or ``functools.update_wrapper(w, fn)``."""
    while isinstance(node, ast.Call):
        targets = resolver.resolve_expr(Located(module, node.func, None))
        if len(targets) == 1 and isinstance(targets[0], External) and targets[0].dotted in _WRAPS:
            return True
        node = node.func
    return False


def _updates_wrapper(resolver, module: str, fn: ast.AST, name: str) -> bool:
    """Whether ``fn``'s own body copies the wrapped function's metadata onto
    ``name`` (``functools.update_wrapper(name, f)`` or ``functools.wraps(f)(name)``),
    which sets ``__wrapped__``, so FastAPI reads the wrapped signature."""
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == name
            and _is_wraps(resolver, module, node)
        ):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _class_updates_wrapper(resolver, cls: Def) -> bool:
    """Whether a decorator class's ``__init__`` copies the wrapped function's
    metadata onto the instance."""
    assert isinstance(cls.node, ast.ClassDef)
    for stmt in cls.node.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__":
            positional = [*stmt.args.posonlyargs, *stmt.args.args]
            return bool(positional) and _updates_wrapper(resolver, cls.module, stmt, positional[0].arg)
    return False


def _nested_def(fn: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The last function named ``name`` defined in ``fn``'s own scope, under branches too."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    stack = list(reversed(getattr(fn, "body", [])))
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == name:
                found.append(stmt)
            continue
        if isinstance(stmt, ast.ClassDef):
            continue
        for child in reversed(list(ast.iter_child_nodes(stmt))):
            if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                stack.append(child)
    return found[-1] if found else None


def _own_returns(fn: ast.AST) -> list[ast.expr]:
    """``return <expr>`` values of ``fn``'s own body (not of nested scopes)."""
    out: list[ast.expr] = []
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Return):
            if node.value is not None:
                out.append(node.value)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _key(target: Target) -> tuple:
    if isinstance(target, Def):
        return ("def", target.module, target.qualname, id(target.node))
    if isinstance(target, Value):
        return ("value", target.loc.module, id(target.loc.node), target.loc.at)
    if isinstance(target, ModuleRef):
        return ("module", target.name)
    if isinstance(target, External):
        return ("external", target.dotted)
    return ("unknown", target.reason, target.text)


def _unique(targets: list[Target]) -> list[Target]:
    seen: set[tuple] = set()
    out: list[Target] = []
    for target in targets:
        key = _key(target)
        if key not in seen:
            seen.add(key)
            out.append(target)
    return out
