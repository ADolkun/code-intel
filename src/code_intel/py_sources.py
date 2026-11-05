"""Backend Python sources: walk, read, decode and parse one package, name its
modules, resolve imports, and record which module-level bindings reach a use.

``PySources`` loads ``<import_root>/<package>/**/*.py``. Each step has its own
diagnostic (``list directory``, ``stat file``, ``read file``, ``decode file``,
``parse file``), and any other per-file exception becomes ``analysis_failed``
at step ``analyze module``, so one bad file never hides the others and never
passes as complete. Parsed modules are cached per file stamp; a file modified
within git's racy window is also checked by content hash.

``ModuleScope`` lists the module-level bindings made by statements that run at
import (the top level and the bodies of ``if``, ``try``, ``with``, ``for``,
``while`` and ``match``; never an ``if TYPE_CHECKING:`` body or an
``if __name__ == "__main__":`` body) and answers which of them can reach a use,
which modules each statement imports, and where a name's value is changed in
place (``X.append(...)``, ``X[k] = v``, ``X.attr = v``).
"""

from __future__ import annotations

import ast
import bisect
import functools
import re
import time
from collections.abc import Callable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from code_intel import code_paths, py_escapes
from code_intel.backend_model import Binding, BindingKind, PyModule
from code_intel.code_paths import FileStamp, SourceFile
from code_intel.problems import Problem, diagnostic

STAGE = "load backend sources"

_TRY_TYPES: tuple[type, ...] = (ast.Try, ast.TryStar)
_TYPE_ALIAS_TYPES: tuple[type, ...] = (ast.TypeAlias,)
# What `ast.parse` raises for source it cannot turn into a tree: bad syntax, a
# null byte, or nesting deeper than the parser or the C stack allow.
_PARSE_ERRORS: tuple[type[BaseException], ...] = (SyntaxError, ValueError, RecursionError, MemoryError)

_SEGMENT_ERRORS: tuple[type[Exception], ...] = (TypeError, ValueError, IndexError, UnicodeError)
_UNPARSE_ERRORS: tuple[type[BaseException], ...] = (RecursionError, ValueError, AttributeError, TypeError)

# One block a statement sits in: (id of the compound statement, branch label).
# A binding inside a conditional block reaches a use only "maybe", unless the
# use sits in that same block.
_Block = tuple[int, str]

_TYPING_MODULES = frozenset({"typing", "typing_extensions"})
# Methods that change a list, dict or set in place.
MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "update",
        "setdefault",
        "popitem",
        "add",
        "discard",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
        "__setitem__",
        "__delitem__",
    }
)
_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


# ── Static truth ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StaticNames:
    """Names whose truth is known without running the module: ``flags`` bound to
    ``typing.TYPE_CHECKING`` (False at runtime), ``typing_modules`` bound to the
    ``typing`` module, and whether ``__name__`` keeps its import-time value (an
    imported module is never ``"__main__"``)."""

    flags: frozenset[str]
    typing_modules: frozenset[str]
    dunder_name: bool

    def truth(self, test: ast.expr, depth: int = 0) -> bool | None:
        """False when ``test`` is never true when the module is imported, True
        when always, else None: ``TYPE_CHECKING`` and ``__name__ == "__main__"``
        combined with ``not``, ``and`` and ``or``."""
        if depth > _MAX_TRUTH_DEPTH:
            return None
        if isinstance(test, ast.Name):
            return False if test.id in self.flags else None
        if isinstance(test, ast.Attribute):
            is_flag = (
                test.attr == "TYPE_CHECKING"
                and isinstance(test.value, ast.Name)
                and test.value.id in self.typing_modules
            )
            return False if is_flag else None
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            inner = self.truth(test.operand, depth + 1)
            return None if inner is None else not inner
        if isinstance(test, ast.BoolOp):
            return self._bool_op_truth(test, depth)
        if isinstance(test, ast.Compare):
            return self._main_check_truth(test)
        return None

    def _bool_op_truth(self, test: ast.BoolOp, depth: int) -> bool | None:
        values = [self.truth(v, depth + 1) for v in test.values]
        stop = isinstance(test.op, ast.Or)
        if stop in values:
            return stop
        return (not stop) if all(v is not None for v in values) else None

    def _main_check_truth(self, test: ast.Compare) -> bool | None:
        """``__name__ == "__main__"`` is False on import, ``!=`` True."""
        if not self.dunder_name or len(test.ops) != 1:
            return None
        sides = (test.left, test.comparators[0])
        names = [s for s in sides if isinstance(s, ast.Name) and s.id == "__name__"]
        mains = [s for s in sides if isinstance(s, ast.Constant) and s.value == "__main__"]
        if len(names) != 1 or len(mains) != 1:
            return None
        if isinstance(test.ops[0], (ast.Eq, ast.Is)):
            return False
        if isinstance(test.ops[0], (ast.NotEq, ast.IsNot)):
            return True
        return None


_MAX_TRUTH_DEPTH = 50


def static_names(tree: ast.Module, *, nested: bool = False) -> StaticNames:
    """The ``StaticNames`` of a module, leaving out any name the module also binds
    some other way (``TYPE_CHECKING = True``, or a function's ``global
    TYPE_CHECKING`` and assignment). With ``nested``, they are the names for
    tests inside function and class bodies too, so a name any scope binds (a
    parameter ``TYPE_CHECKING``) is left out as well."""
    flags: set[str] = set()
    modules: set[str] = set()
    other: set[str] = set()
    for node in ast.walk(tree) if nested else _module_scope_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            _from_import_names(node, flags, other)
        elif isinstance(node, ast.Import):
            _import_names(node, modules, other)
        else:
            name = bound_name(node)
            if name:
                other.add(name)
    return StaticNames(frozenset(flags - other), frozenset(modules - other), "__name__" not in other)


def _from_import_names(node: ast.ImportFrom, flags: set[str], other: set[str]) -> None:
    from_typing = node.level == 0 and node.module in _TYPING_MODULES
    for alias in node.names:
        if from_typing and alias.name == "TYPE_CHECKING":
            flags.add(alias.asname or alias.name)
        elif alias.name != "*":
            other.add(alias.asname or alias.name)


def _import_names(node: ast.Import, modules: set[str], other: set[str]) -> None:
    for alias in node.names:
        if alias.asname:
            bound, module = alias.asname, alias.name
        else:
            bound = module = alias.name.split(".")[0]
        (modules if module in _TYPING_MODULES else other).add(bound)


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _module_scope_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """The nodes whose bindings are module names: what runs in the module's own
    scope (a ``def``'s decorators and defaults, not its body), and a binding in
    a function or class body of a name that body declares ``global``."""
    todo: list[ast.AST] = list(ast.iter_child_nodes(tree))
    while todo:
        node = todo.pop()
        yield node
        todo.extend(_module_scope_children(node))


def _module_scope_children(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, _SCOPES):
        args = getattr(node, "args", None)
        outer: list[ast.AST] = [*getattr(node, "decorator_list", ()), *getattr(node, "bases", ())]
        outer += [kw.value for kw in getattr(node, "keywords", ())]
        if args is not None:
            outer += [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
        return outer + ([] if isinstance(node, ast.Lambda) else _global_bindings(node))
    if isinstance(node, _COMPREHENSIONS):
        # The loop targets are the comprehension's own; a walrus binds the module name.
        return [
            *(getattr(node, field) for field in ("elt", "key", "value") if hasattr(node, field)),
            *(part for gen in node.generators for part in (gen.iter, *gen.ifs)),
        ]
    return list(ast.iter_child_nodes(node))


def _global_bindings(scope: ast.AST) -> list[ast.AST]:
    """Bindings in ``scope``'s body, or in a scope nested in it, of names that
    scope declares ``global``."""
    declared: set[str] = set()
    found: list[ast.AST] = []
    nested: list[ast.AST] = []
    todo: list[ast.AST] = list(getattr(scope, "body", ()))
    while todo:
        node = todo.pop()
        if isinstance(node, ast.Global):
            declared.update(node.names)
        if isinstance(node, _SCOPES):
            nested.append(node)
            found.append(node)  # a nested def binds its name in this scope
            continue
        found.append(node)
        todo.extend(ast.iter_child_nodes(node))
    out = [node for node in found if _binds_any(node, declared)]
    for inner in nested:
        if not isinstance(inner, ast.Lambda):  # a lambda has no statements, so no `global`
            out.extend(_global_bindings(inner))
    return out


def _binds_any(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any((a.asname or a.name).split(".")[0] in names for a in node.names)
    return bound_name(node) in names


def bound_name(node: ast.AST) -> str | None:
    """The name a node other than an import binds, if any."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return node.id
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def module_name_for(path: Path, import_root: Path) -> str | None:
    """Dotted module name of ``path`` under ``import_root`` (``pkg/__init__.py`` -> ``pkg``)."""
    try:
        parts = list(path.relative_to(import_root).parts)
    except ValueError:
        return None
    if not parts or not parts[-1].endswith(".py"):
        return None
    parts[-1] = parts[-1][: -len(".py")]
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str | None:
    """Absolute module for ``from <level dots><target> import ...`` written in ``module``.

    Returns None when the dots climb above the top-level package, as Python
    would raise ``ImportError`` there.
    """
    if level == 0:
        return target or None
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    keep = len(package) - (level - 1)
    if keep <= 0:
        return None
    base = package[:keep]
    if target:
        base = [*base, target]
    return ".".join(base)


@functools.lru_cache(maxsize=1024)
def _source_lines(text: str) -> tuple[str, ...]:
    """The lines of ``text`` with their line breaks, split the way ``ast``
    counts lines; cached, so messages do not split a module again per node."""
    lines: list[str] = []
    start = 0
    for match in _LINE_END.finditer(text):
        lines.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        lines.append(text[start:])
    return tuple(lines)


_LINE_END = re.compile(r"\r\n|\r|\n")


def _source_segment(text: str, node: ast.AST) -> str | None:
    """``ast.get_source_segment`` over the cached lines (columns are UTF-8 offsets)."""
    try:
        first, last = node.lineno - 1, node.end_lineno - 1  # type: ignore[attr-defined]
        start, end = node.col_offset, node.end_col_offset  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if None in (first, last, start, end):
        return None
    lines = _source_lines(text)
    if not 0 <= first <= last < len(lines):
        return None
    if first == last:
        return lines[first].encode()[start:end].decode()
    head = lines[first].encode()[start:].decode()
    tail = lines[last].encode()[:end].decode()
    return head + "".join(lines[first + 1 : last]) + tail


def source_text(mod: PyModule, node: ast.AST, limit: int = 200) -> str:
    """Source text of ``node`` for messages, collapsed to one line and capped at ``limit``."""
    text = None
    try:
        text = _source_segment(mod.text, node)
    except _SEGMENT_ERRORS:
        text = None
    if text is None:
        try:
            text = ast.unparse(node)  # type: ignore[arg-type]
        except _UNPARSE_ERRORS:
            text = f"<{type(node).__name__}>"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Module scope ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ExecStmt:
    """A statement that runs at import, with its position and enclosing blocks."""

    node: ast.stmt
    index: int  # top-level statement index
    blocks: tuple[_Block, ...]  # enclosing compound blocks, outermost first
    conditional: tuple[bool, ...]  # per block: whether it may be skipped
    parent: _ExecStmt | None  # the compound statement whose block holds this one


@dataclass(frozen=True)
class _Bound:
    binding: Binding
    end: tuple[int, int]  # position after which the name holds this value
    blocks: tuple[_Block, ...]
    conditional_blocks: frozenset[_Block]


class ModuleScope:
    """Module-level bindings of one module, in execution order.

    A use sees the bindings made before it (by source position, which is
    execution order for straight-line module code). Walking back from the use,
    every binding counts until one that certainly ran: a binding in a block that
    may be skipped (``if``/``elif``/``else``, a loop body, an ``except`` handler,
    ``try``'s ``else``, a ``match`` case) only "maybe" replaces the earlier ones,
    unless the use sits inside that same block. ``try`` and ``with`` bodies count
    as running. Function and class bodies bind no module names here. A block
    that never runs on import (``if TYPE_CHECKING:``) is left out, and the
    ``else`` of such a test always runs.
    """

    def __init__(self, mod: PyModule, resolve_import: Callable[[int, str | None], str | None]) -> None:
        self.module = mod
        self._resolve_import = resolve_import
        self._static = static_names(mod.tree)
        self._stmts: list[_ExecStmt] = []
        self._by_name: dict[str, list[_Bound]] = {}
        self._stars: list[_Bound] = []
        self._top_starts: list[tuple[int, int]] = []
        self._top_ends: list[tuple[int, int]] = []
        for _index, stmt in enumerate(mod.tree.body):
            self._top_starts.append(_stmt_start(stmt))
            self._top_ends.append(_end(stmt))
        self._collect()
        self._stmts_by_index: dict[int, list[_ExecStmt]] = {}
        for exec_stmt in self._stmts:
            self._stmts_by_index.setdefault(exec_stmt.index, []).append(exec_stmt)
        self._starts_by_index: dict[int, list[tuple[int, int]]] = {
            index: [_stmt_start(s.node) for s in stmts] for index, stmts in self._stmts_by_index.items()
        }
        self._mutations: _Mutations | None = None
        self._escapes: py_escapes.Escapes | None = None
        for bound_list in self._by_name.values():
            bound_list.sort(key=lambda b: b.end)
        self._stars.sort(key=lambda b: b.end)
        self.bindings: Mapping[str, tuple[Binding, ...]] = {
            name: tuple(b.binding for b in bound) for name, bound in self._by_name.items()
        }
        self.star_imports: tuple[Binding, ...] = tuple(b.binding for b in self._stars)

    # ── Collection ───────────────────────────────────────────────────────

    def _collect(self) -> None:
        todo: list[tuple[list[ast.stmt], int | None, tuple[_Block, ...], tuple[bool, ...], _ExecStmt | None]] = [
            (self.module.tree.body, None, (), (), None)
        ]
        while todo:
            body, top, blocks, cond, parent = todo.pop()
            for position, stmt in enumerate(body):
                index = position if top is None else top
                exec_stmt = _ExecStmt(stmt, index, blocks, cond, parent)
                self._stmts.append(exec_stmt)
                self._bind_stmt(stmt, index, blocks, cond)
                for label, sub, may_skip in _child_blocks(stmt, self._static):
                    todo.append((sub, index, (*blocks, (id(stmt), label)), (*cond, may_skip), exec_stmt))
        self._stmts.sort(key=lambda s: _stmt_start(s.node))

    def _add(
        self,
        name: str,
        kind: BindingKind,
        node: ast.AST,
        value: ast.expr | None,
        index: int,
        end: tuple[int, int],
        blocks: tuple[_Block, ...],
        cond: tuple[bool, ...],
        *,
        imported_from: str | None = None,
        imported_name: str | None = None,
        always_maybe: bool = False,
    ) -> None:
        conditional_blocks = frozenset(b for b, c in zip(blocks, cond, strict=False) if c)
        binding = Binding(
            name=name,
            index=index,
            kind=kind,
            node=node,
            value=value,
            conditional=bool(conditional_blocks) or always_maybe,
            imported_from=imported_from,
            imported_name=imported_name,
        )
        if always_maybe:
            # A loop target may never be bound (an empty iterable).
            conditional_blocks = conditional_blocks | {(id(node), "maybe")}
        bound = _Bound(binding, end, blocks, conditional_blocks)
        if kind == "star_import":
            self._stars.append(bound)
        else:
            self._by_name.setdefault(name, []).append(bound)

    def _bind_target(self, target: ast.expr, value: ast.expr | None, kind: BindingKind, stmt: ast.stmt, *ctx) -> None:
        if isinstance(target, ast.Name):
            self._add(target.id, kind, stmt, value, *ctx)
        elif isinstance(target, (ast.Tuple, ast.List)):
            pairwise = (
                isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == len(target.elts)
                and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
            )
            for k, elt in enumerate(target.elts):
                inner = value.elts[k] if pairwise and isinstance(value, (ast.Tuple, ast.List)) else None
                self._bind_target(elt.value if isinstance(elt, ast.Starred) else elt, inner, kind, stmt, *ctx)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value, None, kind, stmt, *ctx)

    def _bind_stmt(self, stmt: ast.stmt, index: int, blocks: tuple[_Block, ...], cond: tuple[bool, ...]) -> None:
        end = _end(stmt)
        ctx = (index, end, blocks, cond)
        for walrus in _executed_walrus(stmt):
            assert isinstance(walrus.target, ast.Name)
            self._add(walrus.target.id, "walrus", walrus, walrus.value, index, _end(walrus), blocks, cond)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            self._bind_assignment(stmt, ctx)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._add(stmt.name, "def", stmt, None, *ctx)
        elif isinstance(stmt, ast.ClassDef):
            self._add(stmt.name, "class", stmt, None, *ctx)
        elif _TYPE_ALIAS_TYPES and isinstance(stmt, _TYPE_ALIAS_TYPES):
            name = stmt.name  # type: ignore[attr-defined]
            if isinstance(name, ast.Name):
                self._add(name.id, "type_alias", stmt, stmt.value, *ctx)  # type: ignore[attr-defined]
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self._bind_import(stmt, ctx)
        else:
            self._bind_block_names(stmt, ctx)

    def _bind_assignment(self, stmt: ast.Assign | ast.AnnAssign | ast.AugAssign, ctx: tuple) -> None:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                self._bind_target(target, stmt.value, "assign", stmt, *ctx)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self._bind_target(stmt.target, stmt.value, "annotated_assign", stmt, *ctx)
        else:
            self._bind_target(stmt.target, None, "augmented_assign", stmt, *ctx)

    def _bind_import(self, stmt: ast.Import | ast.ImportFrom, ctx: tuple) -> None:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    self._add(alias.asname, "import", stmt, None, *ctx, imported_from=alias.name)
                else:
                    top = alias.name.split(".")[0]
                    self._add(top, "import", stmt, None, *ctx, imported_from=top)
            return
        source = self._resolve_import(stmt.level or 0, stmt.module)
        for alias in stmt.names:
            if alias.name == "*":
                self._add("*", "star_import", stmt, None, *ctx, imported_from=source)
            else:
                self._add(
                    alias.asname or alias.name,
                    "from_import",
                    stmt,
                    None,
                    *ctx,
                    imported_from=source,
                    imported_name=alias.name,
                )

    def _bind_block_names(self, stmt: ast.stmt, ctx: tuple) -> None:
        """Names a compound statement binds itself: a loop target, ``with ... as``,
        an ``except ... as`` name, a ``match`` capture."""
        index, _end_pos, blocks, cond = ctx
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._bind_loop_target(stmt.target, stmt, *ctx)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    self._bind_target(item.optional_vars, None, "with", stmt, *ctx)
        elif _TRY_TYPES and isinstance(stmt, _TRY_TYPES):
            for k, handler in enumerate(stmt.handlers):  # type: ignore[attr-defined]
                if handler.name:
                    hblocks = (*blocks, (id(stmt), f"handler{k}"))
                    self._add(handler.name, "except", handler, None, index, _end(handler), hblocks, (*cond, True))
        elif isinstance(stmt, ast.Match):
            for k, case in enumerate(stmt.cases):
                cblocks = (*blocks, (id(stmt), f"case{k}"))
                for capture in _match_captures(case.pattern):
                    self._add(capture[0], "match", capture[1], None, index, _end(case.pattern), cblocks, (*cond, True))

    def _bind_loop_target(self, target: ast.expr, stmt: ast.stmt, index, _end_pos, blocks, cond) -> None:
        # The target is bound before the body runs, so uses in the body see it.
        names: list[ast.Name] = []
        _target_names(target, names)
        start = (stmt.lineno, stmt.col_offset)
        for name in names:
            self._add(name.id, "for", stmt, None, index, start, blocks, cond, always_maybe=True)

    # ── Queries ──────────────────────────────────────────────────────────

    def index_of(self, node: ast.AST) -> int | None:
        """Top-level statement index whose source span holds ``node``, if any."""
        pos = _start(node)
        if pos is None:
            return None
        k = bisect.bisect_right(self._top_starts, pos) - 1
        if k < 0:
            return None
        return k if pos <= self._top_ends[k] else None

    def _use_blocks(self, at: int, pos: tuple[int, int]) -> tuple[_Block, ...]:
        """Blocks enclosing the deepest import-time statement at ``pos`` in statement ``at``.

        Statements nest, so the deepest one holding ``pos`` is the last one
        starting at or before it, or the nearest enclosing statement of that one
        (a bisect and a short walk up, not a scan of every nested statement).
        """
        stmts = self._stmts_by_index.get(at)
        if not stmts:
            return ()
        k = bisect.bisect_right(self._starts_by_index[at], pos) - 1
        found: _ExecStmt | None = stmts[k] if k >= 0 else None
        while found is not None and not (_stmt_start(found.node) <= pos <= _end(found.node)):
            found = found.parent
        return found.blocks if found is not None else ()

    def _use_position(self, at: int | None, node: ast.AST | None) -> tuple[int, int] | None:
        if at is None:
            return None
        pos = _start(node)
        if pos is None or node is None or self.index_of(node) != at:
            pos = self._top_starts[at] if 0 <= at < len(self._top_starts) else None
        return pos

    def reaching(self, name: str, at: int | None, node: ast.AST | None = None) -> list[Binding]:
        """Bindings of ``name`` that may hold when the use runs, latest first.

        ``at`` is the top-level statement index of the use (None: after import,
        for function bodies and other deferred code); ``node`` places the use
        inside that statement. Star imports that ran after every certain
        binding are included; the caller checks whether they export ``name``.
        """
        pos = self._use_position(at, node)
        use_blocks = frozenset(self._use_blocks(at, pos)) if pos is not None and at is not None else frozenset()
        found: list[_Bound] = []
        certain_end: tuple[int, int] | None = None
        for bound in reversed(self._by_name.get(name, ())):
            if pos is not None and bound.end > pos:
                continue
            found.append(bound)
            if bound.conditional_blocks <= use_blocks:
                certain_end = bound.end
                break
        for star in self._stars:
            if pos is not None and star.end > pos:
                continue
            if certain_end is not None and star.end < certain_end:
                continue
            found.append(star)
        found.sort(key=lambda b: b.end, reverse=True)
        return [b.binding for b in found]

    def import_targets(self) -> list[tuple[int, tuple[str, ...], bool, int]]:
        """``(top-level index, modules, may be skipped, line)`` for each import that
        runs at import, in source order. ``import a.b`` lists ``a`` then ``a.b``;
        ``from p import x`` lists ``p`` then ``p.x`` (which is a module only when
        ``x`` is a submodule). Relative imports are absolute here; ones that climb
        above the package are left out. An import under a branch, loop or
        handler may be skipped."""
        out: list[tuple[int, tuple[str, ...], bool, int]] = []
        for exec_stmt in self._stmts:
            targets = self._statement_imports(exec_stmt.node)
            if targets:
                out.append((exec_stmt.index, targets, any(exec_stmt.conditional), exec_stmt.node.lineno))
        return out

    def _statement_imports(self, stmt: ast.stmt) -> tuple[str, ...]:
        if isinstance(stmt, ast.Import):
            return tuple(module for alias in stmt.names for module in _module_and_parents(alias.name))
        if not isinstance(stmt, ast.ImportFrom):
            return ()
        source = self._resolve_import(stmt.level or 0, stmt.module)
        if source is None:
            return ()
        submodules = (f"{source}.{alias.name}" for alias in stmt.names if alias.name != "*")
        return (*_module_and_parents(source), *submodules)

    # ── Changes in place ─────────────────────────────────────────────────

    def mutated(self, name: str, at: int | None, node: ast.AST | None = None) -> bool:
        """Whether the value of module name ``name`` may have been changed in place
        (``name.append(...)``, ``name[k] = v``, ``name.attr = v``, or a function
        assigning it through ``global``) by the time a use at ``at`` runs. A
        change made inside a function may run at any time, so it always counts."""
        found = self.mutations()
        if name in found.anytime:
            return True
        positions = found.at_import.get(name)
        if not positions:
            return False
        pos = self._use_position(at, node)
        return pos is None or positions[0] < pos

    def escaped(self, name: str, at: int | None, node: ast.AST | None = None) -> bool:
        """Whether module name ``name`` is used, by the time a use at ``at``
        runs, anywhere but where its value is only read (``py_escapes``): a
        container bound to it may then have been changed in place. A use inside
        a function may run at any time, so it always counts."""
        found = self.escapes()
        if name in found.anytime:
            return True
        positions = found.at_import.get(name)
        if not positions:
            return False
        pos = self._use_position(at, node)
        return pos is None or positions[0] < pos

    def escapes(self) -> py_escapes.Escapes:
        if self._escapes is None:
            self._escapes = py_escapes.collect_escapes(self.module.tree)
        return self._escapes

    def mutation_receivers(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """``(root name, attribute chain)`` of every in-place change in the module,
        e.g. ``("config", ("COMMON",))`` for ``config.COMMON.append(x)``; used to
        find changes another module makes to this one's names."""
        return self.mutations().receivers

    def mutations(self) -> _Mutations:
        if self._mutations is None:
            self._mutations = _collect_mutations(self.module.tree)
        return self._mutations

    def exports(self) -> tuple[str, ...] | None:
        """Names ``from <module> import *`` binds: a literal ``__all__``, else every
        public module-level name. None when the names are not known for certain:
        ``__all__`` is not one unconditional assignment of a literal list or tuple
        of strings, the module changes it in place (``+=``, ``append``, ...), or the
        module star-imports names it re-exports. A caller then resolves the name
        as if exported, which can only add a binding, never hide one."""
        all_bindings = self._by_name.get("__all__")
        if all_bindings:
            if len(all_bindings) != 1 or self.mutated("__all__", None):
                return None
            binding = all_bindings[0].binding
            value = binding.value
            if (
                binding.kind not in ("assign", "annotated_assign")
                or binding.conditional
                or not isinstance(value, (ast.List, ast.Tuple))
                or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts)
            ):
                return None
            return tuple(e.value for e in value.elts)  # type: ignore[union-attr]
        if self._stars:
            return None  # names a star import brings in are re-exported too
        return tuple(sorted(n for n in self._by_name if not n.startswith("_")))


def _stmt_start(stmt: ast.stmt) -> tuple[int, int]:
    """Where a statement's source starts: a decorated definition starts at its
    first decorator, which ``lineno`` (the ``def``/``class`` line) does not cover."""
    start = (stmt.lineno, stmt.col_offset)
    for dec in getattr(stmt, "decorator_list", ()):
        start = min(start, (dec.lineno, dec.col_offset))
    return start


def _start(node: ast.AST | None) -> tuple[int, int] | None:
    lineno = getattr(node, "lineno", None)
    if lineno is None:
        return None
    return (lineno, getattr(node, "col_offset", 0) or 0)


def _end(node: ast.AST) -> tuple[int, int]:
    end_lineno = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)
    end_col = getattr(node, "end_col_offset", None)
    return (end_lineno, end_col if end_col is not None else 0)


def _child_blocks(stmt: ast.stmt, static: StaticNames | None = None) -> Iterator[tuple[str, list[ast.stmt], bool]]:
    """(label, body, may be skipped) for each nested block that runs at import."""
    if isinstance(stmt, ast.If):
        truth = static.truth(stmt.test) if static is not None else None
        if truth is not False:
            yield "body", stmt.body, truth is None
        if stmt.orelse and truth is not True:
            yield "orelse", stmt.orelse, truth is None
    elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        yield "body", stmt.body, True
        if stmt.orelse:
            yield "orelse", stmt.orelse, True
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        yield "body", stmt.body, False
    elif _TRY_TYPES and isinstance(stmt, _TRY_TYPES):
        yield "body", stmt.body, False  # type: ignore[attr-defined]
        for k, handler in enumerate(stmt.handlers):  # type: ignore[attr-defined]
            yield f"handler{k}", handler.body, True
        if stmt.orelse:  # type: ignore[attr-defined]
            yield "orelse", stmt.orelse, True  # type: ignore[attr-defined]
        if stmt.finalbody:  # type: ignore[attr-defined]
            yield "finalbody", stmt.finalbody, False  # type: ignore[attr-defined]
    elif isinstance(stmt, ast.Match):
        for k, case in enumerate(stmt.cases):
            yield f"case{k}", case.body, True


def _target_names(target: ast.expr, out: list[ast.Name]) -> None:
    if isinstance(target, ast.Name):
        out.append(target)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _target_names(elt, out)
    elif isinstance(target, ast.Starred):
        _target_names(target.value, out)


def _match_captures(pattern: ast.AST) -> list[tuple[str, ast.AST]]:
    out: list[tuple[str, ast.AST]] = []
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            out.append((node.name, node))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            out.append((node.rest, node))
    return out


def _executed_walrus(stmt: ast.stmt) -> Iterator[ast.NamedExpr]:
    """Walrus expressions in ``stmt``'s own expressions that bind a module name.

    Nested statement bodies are separate statements; function, lambda and class
    bodies run later or in their own namespace; annotations are evaluated lazily.
    """
    todo: list[ast.AST] = []
    for field_name, value in ast.iter_fields(stmt):
        if field_name in ("body", "orelse", "finalbody", "handlers", "cases"):
            continue
        if field_name in ("annotation", "returns", "type_params"):
            continue
        if isinstance(value, ast.AST):
            todo.append(value)
        elif isinstance(value, list):
            todo.extend(v for v in value if isinstance(v, ast.AST))
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        todo = [*stmt.decorator_list, *stmt.args.defaults, *(d for d in stmt.args.kw_defaults if d is not None)]
    elif isinstance(stmt, ast.ClassDef):
        todo = [*stmt.decorator_list, *stmt.bases, *(k.value for k in stmt.keywords)]
    elif isinstance(stmt, ast.Match):
        todo = [stmt.subject]
    elif _TYPE_ALIAS_TYPES and isinstance(stmt, _TYPE_ALIAS_TYPES):
        todo = []
    while todo:
        node = todo.pop()
        if isinstance(node, ast.Lambda):
            todo.extend(node.args.defaults)
            todo.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            yield node
        if isinstance(node, ast.arg):
            continue
        todo.extend(ast.iter_child_nodes(node))


# ── Changes in place ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Mutations:
    at_import: Mapping[str, list[tuple[int, int]]]  # name -> sorted positions of import-time changes
    anytime: frozenset[str]  # names changed inside a function or lambda body
    receivers: tuple[tuple[str, tuple[str, ...]], ...]
    aliases: Mapping[str, frozenset[str]]  # name -> module names bound to the same object (itself included)


def _receiver(node: ast.expr) -> tuple[str, tuple[str, ...]] | None:
    """``(root name, attributes)`` of an expression like ``a.b[k].c``, or None."""
    attrs: list[str] = []
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            attrs.append(node.attr)
        else:
            attrs.clear()  # `a.b[k]` changes what `a.b` holds
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return node.id, tuple(reversed(attrs))


def _changed(node: ast.AST) -> list[ast.expr]:
    """The expressions ``node`` changes in place: the receiver of a mutating
    method call, the container of a subscript or attribute store or delete."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
        return [node.func.value]
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.value]
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node]  # `a.b = v` rebinds `a.b`, which changes `a`
    return []


def _set_attribute(node: ast.AST, shadowed: frozenset[str]) -> tuple[str, tuple[str, ...]] | None:
    """The receiver ``setattr(a.b, "c", v)`` or ``delattr(a.b, "c")`` changes:
    ``a.b.c``, or all of ``a.b`` when the attribute name is not a literal."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("setattr", "delattr")
        and node.func.id not in shadowed
        and len(node.args) >= 2
    ):
        return None
    found = _receiver(node.args[0])
    if found is None:
        return None
    name = node.args[1]
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return found[0], (*found[1], name.value)
    return found


def _module_aliases(tree: ast.Module) -> list[tuple[str, tuple[str, tuple[str, ...]]]]:
    """``(name, (root, attributes))`` for each module-level ``name = root.attr``
    (or ``name = root``): both then hold the same object, so a change in place
    through one is a change through the other."""
    out: list[tuple[str, tuple[str, tuple[str, ...]]]] = []
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTION_TYPES + (ast.ClassDef,)):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, (ast.Name, ast.Attribute)):
            source = _receiver(node.value)
            if source is None or any(isinstance(n, ast.Subscript) for n in ast.walk(node.value)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            out.extend((t.id, source) for t in targets if isinstance(t, ast.Name))
    return out


def _alias_classes(
    pairs: list[tuple[str, tuple[str, tuple[str, ...]]]],
) -> tuple[dict[str, frozenset[str]], dict[str, list[tuple[str, tuple[str, ...]]]]]:
    """Names bound to one another (``a = b``), grouped; and for each name, the
    attribute paths it was bound from (``a = m.x``)."""
    parent: dict[str, str] = {}

    def find(name: str) -> str:
        root = name
        while parent.get(root, root) != root:
            root = parent[root]
        parent[name] = root
        return root

    paths: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for name, (root, attrs) in pairs:
        if attrs:
            paths.setdefault(name, []).append((root, attrs))
        else:
            parent[find(name)] = find(root)
    groups: dict[str, set[str]] = {}
    for name in list(parent):
        groups.setdefault(find(name), set()).add(name)
    classes = {name: frozenset(group) for group in groups.values() for name in group if len(group) > 1}
    return classes, paths


def _expand_receivers(
    receivers: list[tuple[str, tuple[str, ...]]],
    classes: Mapping[str, frozenset[str]],
    paths: Mapping[str, list[tuple[str, tuple[str, ...]]]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Every receiver a change reaches through module-level aliases."""
    out: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    todo = list(receivers)
    while todo:
        found = todo.pop()
        if found in seen:
            continue
        seen.add(found)
        out.append(found)
        root, attrs = found
        for name in classes.get(root, (root,)):
            todo.append((name, attrs))
            todo.extend((base, (*path, *attrs)) for base, path in paths.get(name, ()))
    return out


def _function_locals(fn: ast.AST) -> tuple[set[str], set[str]]:
    """(local names, names declared ``global``) of one function or lambda scope."""
    names = _parameter_names(fn.args)  # type: ignore[attr-defined]
    declared: set[str] = set()
    body = fn.body if isinstance(fn.body, list) else [fn.body]  # type: ignore[attr-defined]
    todo: list[ast.AST] = list(body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A nested def or class binds its name here; its body is its own scope.
            names.add(node.name)
        elif not isinstance(node, ast.Lambda):
            _note_binding(node, names, declared)
            todo.extend(ast.iter_child_nodes(node))
    return names - declared, declared


def _parameter_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names.update(extra.arg for extra in (args.vararg, args.kwarg) if extra is not None)
    return names


def _note_binding(node: ast.AST, names: set[str], declared: set[str]) -> None:
    """Add the names ``node`` binds in its scope to ``names``, and the names it
    declares ``global`` or ``nonlocal`` to ``declared``."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        names.add(node.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        declared.update(node.names)


def _collect_mutations(tree: ast.Module) -> _Mutations:
    """Every in-place change of a module-level name. One pass over the tree;
    inside a function a change counts only when the name is not local there."""
    changes, anytime = _changes_in(tree)
    at_import: dict[str, list[tuple[int, int]]] = {}
    receivers: list[tuple[str, tuple[str, ...]]] = []
    classes, paths = _alias_classes(_module_aliases(tree))
    for found_receiver, deferred, pos in changes:
        for root, attrs in _expand_receivers([found_receiver], classes, paths):
            receivers.append((root, attrs))
            if deferred:
                anytime.add(root)
            else:
                at_import.setdefault(root, []).append(pos)
    for positions in at_import.values():
        positions.sort()
    return _Mutations(at_import, frozenset(anytime), tuple(dict.fromkeys(receivers)), classes)


def _changes_in(
    tree: ast.Module,
) -> tuple[list[tuple[tuple[str, tuple[str, ...]], bool, tuple[int, int]]], set[str]]:
    """``(receiver, inside a function body, position)`` of each in-place change,
    before aliases are followed; and the names a function rebinds through ``global``."""
    changes: list[tuple[tuple[str, tuple[str, ...]], bool, tuple[int, int]]] = []
    rebound: set[str] = set()
    # (node, local names of the enclosing functions, inside a function body)
    todo: list[tuple[ast.AST, frozenset[str], bool]] = [(tree, frozenset(), False)]
    while todo:
        node, shadowed, deferred = todo.pop()
        if isinstance(node, _FUNCTION_TYPES):
            todo.extend(_function_parts(node, shadowed, deferred, rebound))
            continue
        found_receivers = [_receiver(target) for target in _changed(node)]
        found_receivers.append(_set_attribute(node, shadowed))
        for found_receiver in found_receivers:
            if found_receiver is not None and found_receiver[0] not in shadowed:
                changes.append((found_receiver, deferred, _start(node) or (0, 0)))
        todo.extend((child, shadowed, deferred) for child in ast.iter_child_nodes(node))
    return changes, rebound


def _function_parts(
    node: ast.AST, shadowed: frozenset[str], deferred: bool, rebound: set[str]
) -> list[tuple[ast.AST, frozenset[str], bool]]:
    """The parts of a function to scan, with the names local there: decorators
    and defaults run where the ``def`` is, the body later. Names the body
    assigns through ``global`` are added to ``rebound``."""
    local, declared = _function_locals(node)
    inner = frozenset((shadowed | local) - declared)
    body = node.body if isinstance(node.body, list) else [node.body]  # type: ignore[attr-defined]
    if declared:
        for sub in body:
            for found in ast.walk(sub):
                if isinstance(found, ast.Name) and isinstance(found.ctx, ast.Store) and found.id in declared:
                    rebound.add(found.id)
    args = node.args  # type: ignore[attr-defined]
    outer = [*getattr(node, "decorator_list", ()), *args.defaults, *(d for d in args.kw_defaults if d is not None)]
    return [*((child, shadowed, deferred) for child in outer), *((child, inner, True) for child in body)]


# ── Sources ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CacheEntry:
    stamp: FileStamp
    content_hash: str | None  # content hash when the stamp was racy at caching time
    module: PyModule


class PySources:
    """Every module of one backend package, loaded with per-step diagnostics.

    ``load()`` may run again after files change: modules whose stamp (and, for
    racy stamps, content) is unchanged are reused, so a one-file edit re-parses
    one file. ``changed`` names the modules re-parsed by the last load.
    """

    def __init__(
        self,
        import_root: Path,
        package: str,
        repo_root: Path,
        *,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        self.import_root = Path(import_root)
        self.package = package
        self.repo_root = Path(repo_root)
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}
        self._scopes: dict[str, tuple[PyModule, ModuleScope]] = {}
        self.modules: dict[str, PyModule] = {}
        self.files: tuple[SourceFile, ...] = ()
        self.problems: tuple[Problem, ...] = ()
        self.changed: frozenset[str] = frozenset()
        self._known: frozenset[str] = frozenset()
        self._changed_elsewhere: dict[tuple[str, str], bool] = {}
        self._escaped_elsewhere: dict[tuple[str, str], bool] = {}

    @property
    def files_scanned(self) -> int:
        return len(self.files)

    def load(self, visible: AbstractSet[str] | None = None) -> PySources:
        """``visible``: the git-visible files (``RepoFiles.files``); only they
        are read, so ignored build output never is. None walks every file, for
        a package that is not in a git work tree."""
        problems: list[Problem] = []
        root = self.import_root.joinpath(*self.package.split("."))
        files = code_paths.iter_source_files(
            root, code_paths.PY_SUFFIXES, self.repo_root, problems, stage=STAGE, visible=visible
        )
        modules: dict[str, PyModule] = {}
        cache: dict[str, _CacheEntry] = {}
        changed: set[str] = set()
        for file in files:
            key = code_paths.cache_key(file.path)
            try:
                entry = self._reuse(key, file)
                if entry is None:
                    entry = self._parse(file, problems)
                    if entry is None:
                        continue
                    changed.add(entry.module.name)
            except Exception as exc:  # noqa: BLE001 - one module never stops the analysis
                problems.append(
                    diagnostic("analysis_failed", stage=STAGE, step="analyze module", file=file.rel, exc=exc)
                )
                continue
            cache[key] = entry
            modules[entry.module.name] = entry.module
        self._cache = cache
        self.modules = dict(sorted(modules.items()))
        self.files = tuple(files)
        self.problems = tuple(problems)
        self.changed = frozenset(changed)
        self._known = frozenset(self._module_prefixes(self.modules))
        self._scopes = {name: v for name, v in self._scopes.items() if self.modules.get(name) is v[0]}
        self._changed_elsewhere = {}
        self._escaped_elsewhere = {}
        return self

    @staticmethod
    def _module_prefixes(modules: Mapping[str, PyModule]) -> set[str]:
        known: set[str] = set()
        for name in modules:
            parts = name.split(".")
            for k in range(1, len(parts) + 1):
                known.add(".".join(parts[:k]))
        return known

    def _reuse(self, key: str, file: SourceFile) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None or entry.stamp != file.stamp:
            return None
        if entry.content_hash is not None:
            try:
                if code_paths.content_digest(file.path) != entry.content_hash:
                    return None
            except OSError:
                return None
        return entry

    def _parse(self, file: SourceFile, problems: list[Problem]) -> _CacheEntry | None:
        name = module_name_for(file.path, self.import_root)
        if name is None:
            return None
        # A racy stamp cannot tell a same-size edit apart, so the digest of the
        # very bytes parsed is kept to compare the next time.
        content_hash: str | None = None
        if code_paths.is_racy(file.stamp, self._clock()):
            text, content_hash, problem = code_paths.read_python_source_with_digest(file.path, file.rel, stage=STAGE)
        else:
            text, problem = code_paths.read_python_source(file.path, file.rel, stage=STAGE)
        if problem is not None:
            problems.append(problem)
            return None
        assert text is not None
        try:
            tree = ast.parse(text, filename=file.rel)
        except _PARSE_ERRORS as exc:
            problems.append(diagnostic("parse_failed", stage=STAGE, step="parse file", file=file.rel, exc=exc))
            return None
        module = PyModule(
            name=name,
            path=file.path,
            rel=file.rel,
            stamp=file.stamp,
            tree=tree,
            text=text,
            is_package=file.path.name == "__init__.py",
        )
        return _CacheEntry(stamp=file.stamp, content_hash=content_hash, module=module)

    # ── Lookups ──────────────────────────────────────────────────────────

    def module(self, name: str) -> PyModule | None:
        return self.modules.get(name)

    def has_module(self, name: str) -> bool:
        """True for an analyzed module, or a package that holds one (namespace packages too)."""
        return name in self._known

    def in_package(self, dotted: str) -> bool:
        """True when ``dotted`` names the analyzed package or something inside it."""
        return dotted == self.package or dotted.startswith(self.package + ".")

    def resolve_import(self, module: str, level: int, target: str | None) -> str | None:
        """Absolute module that ``from <level dots><target> import ...`` in ``module`` reads."""
        mod = self.modules.get(module)
        is_package = mod.is_package if mod is not None else False
        return resolve_relative(module, is_package, level, target)

    def scope(self, name: str) -> ModuleScope:
        mod = self.modules[name]
        cached = self._scopes.get(name)
        if cached is not None and cached[0] is mod:
            return cached[1]
        scope = ModuleScope(mod, lambda level, target, _m=name: self.resolve_import(_m, level, target))
        self._scopes[name] = (mod, scope)
        return scope

    def changed_elsewhere(self, module: str, name: str) -> bool:
        """Whether another module changes ``module.name`` in place, through
        ``from module import name`` or ``import module`` (``name.append(...)``,
        ``module.name[k] = v``, ``module.name = v``, ...)."""
        key = (module, name)
        cached = self._changed_elsewhere.get(key)
        if cached is not None:
            return cached
        names = self.scope(module).mutations().aliases.get(name, (name,)) if module in self.modules else (name,)
        found = any(self._changed_by_other(module, alias) for alias in names)
        self._changed_elsewhere[key] = found
        return found

    def escaped_elsewhere(self, module: str, name: str) -> bool:
        """Whether another module uses ``module.name`` anywhere but where its
        value is only read: through ``from module import name`` (under any
        name, also re-exported), ``import module`` or a star import."""
        key = (module, name)
        cached = self._escaped_elsewhere.get(key)
        if cached is None:
            targets = {f"{m}.{n}" for m, n in self._import_aliases(module, name)}
            cached = any(self._uses_any(other, targets) for other in self.modules)
            self._escaped_elsewhere[key] = cached
        return cached

    def _import_aliases(self, module: str, name: str) -> set[tuple[str, str]]:
        """``module.name`` and every ``(module, name)`` that imports it, directly or
        through other imports."""
        found = {(module, name)}
        while True:
            more = {
                (other, local)
                for other in self.modules
                for local, bindings in self.scope(other).bindings.items()
                for b in bindings
                if b.kind == "from_import"
                and (b.imported_from, b.imported_name) in found
                and (other, local) not in found
            }
            if not more:
                return found
            found |= more

    def _uses_any(self, module: str, targets: set[str]) -> bool:
        """Whether ``module`` has a use (``py_escapes``) of a dotted name in
        ``targets``, of something under one, or of a module above one."""
        scope = self.scope(module)
        for root, attrs in scope.escapes().refs:
            for base in self._import_bases(scope, root):
                dotted = ".".join((base, *attrs))
                if any(dotted == t or dotted.startswith(t + ".") or t.startswith(dotted + ".") for t in targets):
                    return True
        return False

    @staticmethod
    def _bound_import_bases(scope: ModuleScope, root: str) -> list[str]:
        """The dotted names the module's own imports of ``root`` stand for."""
        out: list[str] = []
        for binding in scope.bindings.get(root, ()):
            if binding.kind == "from_import" and binding.imported_from:
                out.append(f"{binding.imported_from}.{binding.imported_name}")
            elif binding.kind == "import" and binding.imported_from:
                out.append(binding.imported_from)
        return out

    @classmethod
    def _import_bases(cls, scope: ModuleScope, root: str) -> list[str]:
        """The dotted names an imported module-level name may stand for."""
        out = cls._bound_import_bases(scope, root)
        # A name the module does not bind may come from any of its star imports.
        if root not in scope.bindings:
            out.extend(f"{star.imported_from}.{root}" for star in scope.star_imports if star.imported_from)
        return out

    def _changed_by_other(self, module: str, name: str) -> bool:
        """Whether another module changes ``module.name`` in place through an
        import of it (the text check skips modules that never mention it)."""
        target = f"{module}.{name}"
        return any(
            self._changes_target(other, target)
            for other, mod in self.modules.items()
            if other != module and name in mod.text
        )

    def _changes_target(self, other: str, target: str) -> bool:
        scope = self.scope(other)
        for root, attrs in scope.mutation_receivers():
            for base in self._bound_import_bases(scope, root):
                dotted = ".".join((base, *attrs))
                if dotted == target or dotted.startswith(target + "."):
                    return True
        return False

    def text(self, module: str, node: ast.AST, limit: int = 200) -> str:
        mod = self.modules.get(module)
        if mod is None:
            try:
                return ast.unparse(node)[:limit]  # type: ignore[arg-type]
            except _UNPARSE_ERRORS:
                return f"<{type(node).__name__}>"
        return source_text(mod, node, limit)


def _module_and_parents(module: str) -> list[str]:
    """``a``, ``a.b``, ``a.b.c`` for ``a.b.c``: importing a module imports its parents first."""
    parts = module.split(".")
    return [".".join(parts[: k + 1]) for k in range(len(parts))]
