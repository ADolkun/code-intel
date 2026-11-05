"""Resolve symbol names to candidate definitions across Python and TypeScript.

Disambiguates same-name symbols by listing file:line, kind, scope, signature and
a ranking score for each candidate.

- **TypeScript/JavaScript** (the configured ``frontend.root``, every TS and JS suffix): declarations
  come from the TypeScript compiler through the helper's ``facts`` mode, cached
  per file by ``ts_facts.FactsCache``. Kinds, scopes and the component rule are
  documented in README "Symbols".
- **Python** (the configured ``backend.root``): module-level definitions (including bodies of
  ``if``/``try``/``with``/``for``/``while``/``match`` and module-level loop and
  walrus targets), class members, and functions and classes nested in functions.
  Function-local variables are not indexed: Python has no declaration keyword,
  so every assignment would become a "definition"; use text search for those.
  Each file's index is cached by ``SymbolIndex`` under the same stamp and
  racy-clean rule as ``FactsCache``.

CLI:
    code-intel dump symbols NAME [--side both] [--kind KIND] [--file-hint HINT] [--json]
    code-intel dump symbols --dump-ts --json     # every TS declaration (oracle comparison)
    code-intel dump symbols --dump-py --json     # every Python symbol
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
import threading
import time
import typing
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from code_intel.cli import Context
from code_intel.code_paths import (
    PY_SUFFIXES,
    TS_SUFFIXES,
    SourceFile,
    cache_key,
    content_digest,
    is_racy,
    is_test_path,
    iter_source_files,
    read_python_source,
    read_python_source_with_digest,
    split_source_lines,
)
from code_intel.config import Config
from code_intel.problems import Problem, diagnostic, partition
from code_intel.protocol_match import BaseRef, ClassRec, ClassTable, MemberRec, ProtocolMatch
from code_intel.py_sources import module_name_for, resolve_relative
from code_intel.repo_files import RepoFilesError, list_repo_files
from code_intel.result_types import SCHEMA_VERSION
from code_intel.ts_bridge import HelperError, ToolchainProblem, find_toolchain, frontend_not_found_problem
from code_intel.ts_facts import FactsCache, ToolchainUnavailable, TsDecl, facts_problems

Language = Literal["python", "typescript"]
SymbolKind = Literal[
    "function",
    "class",
    "component",
    "method",
    "const",
    "variable",
    "type_alias",
    "interface",
    "enum",
    "namespace",
]
SYMBOL_KINDS: tuple[str, ...] = typing.get_args(SymbolKind)
Scope = Literal["module", "namespace", "class", "object", "local"]
SCOPES: tuple[str, ...] = typing.get_args(Scope)
Side = Literal["backend", "frontend", "both"]
SIDES: tuple[str, ...] = typing.get_args(Side)

STAGE = "symbol resolve"

# ast.parse raises RecursionError or MemoryError on deeply nested expressions
# as well as SyntaxError (ValueError for NUL bytes).
_PARSE_ERRORS: tuple[type[BaseException], ...] = (SyntaxError, ValueError, RecursionError, MemoryError)
_HASH_ERRORS: tuple[type[Exception], ...] = (OSError,)


def _try_content_hash(path: Path) -> str | None:
    """The file's content hash, or None when it cannot be read."""
    try:
        return content_digest(path)
    except _HASH_ERRORS:
        return None


_UPPER_CONST_RE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")
_ENUM_BASES = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"})
# Subscripting one of these builds a type, so `X = Name[...]` is a type alias.
_GENERIC_ALIAS_BASES = frozenset(
    {
        "Annotated",
        "Optional",
        "Union",
        "Literal",
        "Callable",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "type",
        "List",
        "Dict",
        "Set",
        "FrozenSet",
        "Tuple",
        "Type",
        "Sequence",
        "Mapping",
        "MutableMapping",
        "Iterable",
        "Iterator",
        "Generator",
        "AsyncIterator",
        "AsyncGenerator",
        "Awaitable",
        "Coroutine",
        "Collection",
        "AbstractSet",
        "MutableSequence",
        "MutableSet",
        "ClassVar",
        "Final",
        "Required",
        "NotRequired",
        "ReadOnly",
        "TypeGuard",
        "TypeIs",
        "Concatenate",
        "Unpack",
    }
)
_TYPE_CONSTRUCTORS = frozenset({"TypeVar", "ParamSpec", "TypeVarTuple", "NewType", "TypeAliasType"})
_BUILTIN_TYPES = frozenset(
    {
        "int",
        "str",
        "float",
        "bool",
        "bytes",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "type",
        "object",
        "complex",
        "bytearray",
    }
)
_CONTROL_FLOW: tuple[type, ...] = (
    ast.If,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Match,
)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_INIT_METHODS = frozenset({"__init__", "__post_init__"})

# Ranking (README "Symbols"): where a file lives, then how deep the definition sits.
_SCOPE_PENALTY = {"object": 0.2, "local": 0.1, "class": 0.1}
_TEST_PENALTY = 0.3
_SCOPE_RANK = {scope: rank for rank, scope in enumerate(SCOPES)}


@dataclass(frozen=True)
class SymbolLayout:
    """Where the two sides live and which paths rank first, as ``config`` has them.

    ``primary_prefixes`` are repository-relative path prefixes; a file under the
    backend or frontend root but under none of them ranks second. A backend
    root of ``.`` is the whole repository.
    """

    backend_root: str
    frontend_root: str
    primary_prefixes: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Config) -> SymbolLayout:
        return cls(config.backend.root, config.frontend.root, config.symbols.primary_prefixes)

    @property
    def secondary_prefixes(self) -> tuple[str, ...]:
        return tuple("" if root == "." else f"{root}/" for root in (self.frontend_root, self.backend_root))


@dataclass
class SymbolCandidate:
    """One candidate definition for a symbol name.

    ``exported`` is TypeScript-only (an ``export`` modifier or a local
    ``export { … }``); Python candidates carry None.
    """

    name: str
    file: str
    start_line: int
    end_line: int | None
    kind: str
    signature: str
    language: Language
    score: float = field(default=0.5)
    scope: str = "module"
    container: str | None = None
    exported: bool | None = None
    is_test: bool = False
    # Python methods only. A call typed as a Protocol resolves to the Protocol's
    # method, so a method of a class that matches one lists those methods here.
    protocol_method: bool = False
    protocols: list[ProtocolMatch] = field(default_factory=list)


@dataclass(frozen=True)
class SymbolResult:
    candidates: list[SymbolCandidate]
    complete: bool
    problems: list[Problem]
    # Set when side="both" ran without the frontend toolchain.
    frontend_problem: ToolchainProblem | None = None


@dataclass(frozen=True)
class PySymbol:
    name: str
    kind: str
    line: int
    end_line: int | None
    column: int
    scope: str
    container: str | None
    signature: str
    # Class symbols only: each base as a dotted name (``typing.Protocol[T]`` is
    # ``typing.Protocol``), and the attributes assigned on ``self`` in
    # ``__init__`` and ``__post_init__``.
    bases: tuple[str, ...] = ()
    instance_attrs: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Import:
    """``level`` leading dots, ``module`` and, for ``from module import attr``, ``attr``."""

    level: int
    module: str | None
    attr: str | None


@dataclass
class _PyEntry:
    stamp: object  # code_paths.FileStamp
    racy: bool
    content_hash: str | None
    by_name: dict[str, list[PySymbol]]
    problem: Problem | None
    # Local name -> where it was imported from, for resolving class bases.
    imports: dict[str, _Import] = field(default_factory=dict)
    # Built on the first Protocol lookup; lives and dies with the file's entry.
    classes: list[ClassRec] | None = None


# ── Python ───────────────────────────────────────────────────────────────


def _base_name(node: ast.expr) -> str:
    """`Final`, `typing.Final`, `t.Protocol[T]` -> the last name part."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _dotted_name(node: ast.expr) -> str:
    """`m.Name`, `Name`, `m.Name[T]` -> the dotted path; "" for any other expression."""
    if isinstance(node, ast.Subscript):
        node = node.value
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    return ".".join([node.id, *reversed(parts)])


def _typeish(value: ast.expr) -> bool:
    """An operand that makes a `|` union a type: None, a builtin type or a generic subscript."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if isinstance(value, ast.Name) and value.id in _BUILTIN_TYPES:
        return True
    if isinstance(value, ast.Subscript) and _base_name(value) in _GENERIC_ALIAS_BASES:
        return True
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
        return _typeish(value.left) or _typeish(value.right)
    return False


def _assignment_kind(name: str, node: ast.Assign | ast.AnnAssign) -> str:
    if isinstance(node, ast.AnnAssign):
        annotation = _base_name(node.annotation)
        if annotation == "TypeAlias":
            return "type_alias"
        if annotation == "Final":
            return "const"
    value = node.value
    if isinstance(value, ast.Call) and _base_name(value.func) in _TYPE_CONSTRUCTORS:
        return "type_alias"
    if isinstance(value, ast.Subscript) and _base_name(value) in _GENERIC_ALIAS_BASES:
        return "type_alias"
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr) and _typeish(value):
        return "type_alias"
    # Python has no const; an UPPER_CASE name is one by convention.
    return "const" if _UPPER_CONST_RE.match(name) else "variable"


def _target_names(target: ast.expr) -> Iterator[ast.Name]:
    if isinstance(target, ast.Name):
        yield target
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _target_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _walrus_targets(node: ast.AST) -> Iterator[ast.NamedExpr]:
    """Walrus expressions in ``node`` that bind in the enclosing scope (not inside lambdas or defs)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.NamedExpr):
            yield current
        for child in ast.iter_child_nodes(current):
            if not isinstance(child, (ast.Lambda, *_DEFS)):
                stack.append(child)


def _control_flow_headers(node: ast.stmt) -> list[ast.AST]:
    """The expressions of a control-flow statement that run in its own scope (not its bodies)."""
    if isinstance(node, (ast.If, ast.While)):
        return [node.test]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.iter]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [item.context_expr for item in node.items]
    if isinstance(node, ast.Match):
        return [node.subject, *(case.guard for case in node.cases if case.guard is not None)]
    return []


def _control_flow_bodies(node: ast.stmt) -> list[list[ast.stmt]]:
    bodies = [getattr(node, "body", []) or [], getattr(node, "orelse", []) or [], getattr(node, "finalbody", []) or []]
    bodies.extend(handler.body for handler in getattr(node, "handlers", []) or [])
    bodies.extend(case.body for case in getattr(node, "cases", []) or [])
    return bodies


def _python_signature(lines: list[str], node: ast.AST) -> str:
    """The node's first source line from its column (the ``def``/``class`` line for decorated defs)."""
    lineno = getattr(node, "lineno", 0)
    if 0 < lineno <= len(lines):
        line = lines[lineno - 1]
        end = getattr(node, "end_col_offset", None) if getattr(node, "end_lineno", None) == lineno else None
        first = line[getattr(node, "col_offset", 0) : end].strip()
        if first:
            return first
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}(...)"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}(...)"
    return ""


def _column(lines: list[str], line: int, col_offset: int) -> int:
    """1-based column in code points; ``ast`` offsets count UTF-8 bytes."""
    if not 0 < line <= len(lines):
        return col_offset + 1
    text = lines[line - 1]
    if text.isascii():
        return col_offset + 1
    return len(text.encode("utf-8", "surrogatepass")[:col_offset].decode("utf-8", "replace")) + 1


def _instance_attributes(node: ast.ClassDef) -> tuple[str, ...]:
    """Attributes assigned on ``self`` in ``__init__`` and ``__post_init__`` (not in nested defs)."""
    found: dict[str, None] = {}
    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name not in _INIT_METHODS:
            continue
        if not method.args.args:
            continue
        receiver = method.args.args[0].arg
        stack: list[ast.AST] = [node for node in method.body if not isinstance(node, (ast.Lambda, *_DEFS))]
        while stack:
            current = stack.pop()
            if (
                isinstance(current, ast.Attribute)
                and isinstance(current.ctx, ast.Store)
                and isinstance(current.value, ast.Name)
                and current.value.id == receiver
            ):
                found[current.attr] = None
            stack.extend(
                child for child in ast.iter_child_nodes(current) if not isinstance(child, (ast.Lambda, *_DEFS))
            )
    return tuple(found)


def index_python_module(tree: ast.Module, lines: list[str]) -> list[PySymbol]:
    """Every indexed Python definition in one module, in source order of discovery."""
    indexer = _PyIndexer(lines)
    indexer.definitions(tree.body, "module", None)
    return indexer.out


class _PyIndexer:
    """One module's definitions, collected by a walk that tracks scope."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.out: list[PySymbol] = []
        # Enum classes seen so far, so a subclass of one is an enum too.
        self.enums: set[str] = set()

    def add(
        self,
        name: str,
        node: ast.AST,
        kind: str,
        scope: str,
        container: str | None,
        at: ast.AST | None = None,
        bases: tuple[str, ...] = (),
        instance_attrs: tuple[str, ...] = (),
    ) -> None:
        anchor = at if at is not None else node
        line = getattr(anchor, "lineno", 0)
        self.out.append(
            PySymbol(
                name=name,
                kind=kind,
                line=line,
                end_line=getattr(node, "end_lineno", None),
                column=_column(self.lines, line, getattr(anchor, "col_offset", 0)),
                scope=scope,
                container=container,
                signature=_python_signature(self.lines, node),
                bases=bases,
                instance_attrs=instance_attrs,
            )
        )

    def class_kind(self, node: ast.ClassDef) -> str:
        bases = {_base_name(base) for base in node.bases}
        if bases & _ENUM_BASES or bases & self.enums:
            self.enums.add(node.name)
            return "enum"
        if "Protocol" in bases:
            return "interface"
        return "class"

    def add_class(self, node: ast.ClassDef, scope: str, container: str | None) -> None:
        self.add(
            node.name,
            node,
            self.class_kind(node),
            scope,
            container,
            bases=tuple(filter(None, (_dotted_name(base) for base in node.bases))),
            instance_attrs=_instance_attributes(node),
        )
        qualname = f"{container}.{node.name}" if scope == "class" and container else node.name
        self.definitions(node.body, "class", qualname)

    def add_assignment(self, node: ast.Assign | ast.AnnAssign, scope: str, container: str | None) -> None:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                self.add(node.target.id, node, _assignment_kind(node.target.id, node), scope, container)
            return
        for target in node.targets:
            for name in _target_names(target):
                self.add(name.id, node, _assignment_kind(name.id, node), scope, container, at=name)

    def definitions(self, body: list[ast.stmt], scope: str, container: str | None) -> None:
        """Statements that run in a module or class body: their definitions
        belong to that scope, through any control-flow nesting."""
        for node in body:
            self.definition(node, scope, container)
            if scope == "module" and not isinstance(node, _DEFS):
                self.walrus_targets(node, scope, container)

    def definition(self, node: ast.stmt, scope: str, container: str | None) -> None:
        if isinstance(node, ast.ClassDef):
            self.add_class(node, scope, container)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.add(node.name, node, "method" if scope == "class" else "function", scope, container)
            self.nested(node.body)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            self.add_assignment(node, scope, container)
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            self.add(node.name.id, node, "type_alias", scope, container)
        elif isinstance(node, _CONTROL_FLOW):
            if isinstance(node, (ast.For, ast.AsyncFor)):
                for name in _target_names(node.target):
                    self.add(name.id, node, "variable", scope, container, at=name)
            for inner in _control_flow_bodies(node):
                self.definitions(inner, scope, container)

    def walrus_targets(self, node: ast.stmt, scope: str, container: str | None) -> None:
        roots = _control_flow_headers(node) if isinstance(node, _CONTROL_FLOW) else [node]
        for expr_root in roots:
            for walrus in _walrus_targets(expr_root):
                if isinstance(walrus.target, ast.Name):
                    self.add(walrus.target.id, walrus, "variable", scope, container, at=walrus.target)

    def nested(self, body: list[ast.stmt]) -> None:
        """A function body: only nested defs and classes are indexed (scope local)."""
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.add(node.name, node, "function", "local", None)
                self.nested(node.body)
            elif isinstance(node, ast.ClassDef):
                self.add_class(node, "local", None)
            elif isinstance(node, _CONTROL_FLOW):
                for inner in _control_flow_bodies(node):
                    self.nested(inner)


def _module_imports(tree: ast.Module) -> dict[str, _Import]:
    """Every name a module-level import binds, also under ``if``/``try`` (the last binding wins).

    Function bodies are not walked: it would cost a pass over every node of the
    backend, and a base class is named at module or class level.
    """
    out: dict[str, _Import] = {}
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop(0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b` binds `a`; `import a.b as m` binds `m` to a.b.
                module = alias.name if alias.asname else alias.name.split(".")[0]
                out[alias.asname or module] = _Import(0, module, None)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = _Import(node.level, node.module, alias.name)
        elif isinstance(node, _CONTROL_FLOW):
            for inner in _control_flow_bodies(node):
                stack.extend(inner)
    return out


def _resolve_base(path: str, module: str, is_package: bool, entry: _PyEntry) -> BaseRef:
    """What a base written as ``path`` in ``module`` names, from the file's own definitions and imports."""
    head, *rest = path.split(".")
    imported = entry.imports.get(head)
    if imported is None:
        if any(s.scope == "module" and s.kind in ("class", "interface", "enum") for s in entry.by_name.get(head, ())):
            return BaseRef(path, f"{module}:{path}")
        # A name the file neither defines nor imports is a builtin; one it binds some other way is unknown.
        return BaseRef(path, external=head not in entry.by_name)
    source_module = (
        resolve_relative(module, is_package, imported.level, imported.module) if imported.level else imported.module
    )
    if source_module is None:
        return BaseRef(path)
    if imported.attr is None:
        # `import a.b as m` / `import a`: the path goes on through submodules to the class.
        if not rest:
            return BaseRef(path, module=source_module)
        return BaseRef(path, f"{'.'.join([source_module, *rest[:-1]])}:{rest[-1]}", source_module)
    if not rest:
        return BaseRef(path, f"{source_module}:{imported.attr}", source_module)
    # `from a import b` where b is a submodule: b.Name
    return BaseRef(path, f"{'.'.join([source_module, imported.attr, *rest[:-1]])}:{rest[-1]}", source_module)


def _index_python_file(
    source: SourceFile, *, digest: bool = False
) -> tuple[dict[str, list[PySymbol]], Problem | None, str | None, dict[str, _Import]]:
    """``(symbols by name, problem, digest, imports)``; with ``digest``, the SHA-256 of
    exactly the bytes indexed, from the same read (None when the read failed)."""
    if digest:
        text, content_hash, problem = read_python_source_with_digest(source.path, source.rel, stage=STAGE)
    else:
        (text, problem), content_hash = read_python_source(source.path, source.rel, stage=STAGE), None
    if text is None:
        return {}, problem, content_hash, {}
    try:
        tree = ast.parse(text, filename=source.rel)
    except _PARSE_ERRORS as exc:
        problem = diagnostic("parse_failed", stage=STAGE, step="parse file", file=source.rel, exc=exc)
        return {}, problem, content_hash, {}
    by_name: dict[str, list[PySymbol]] = {}
    for symbol in index_python_module(tree, split_source_lines(text)):
        by_name.setdefault(symbol.name, []).append(symbol)
    return by_name, None, content_hash, _module_imports(tree)


# ── Ranking ──────────────────────────────────────────────────────────────


def file_hint_matches(rel_path: str, file_hint: str) -> bool:
    """True when ``file_hint`` is a substring of the repo-relative POSIX path, or
    a glob matching part of it. Case-sensitive on every platform, so a hint means
    the same thing on Windows and POSIX."""
    normalized = rel_path.replace("\\", "/")
    hint = file_hint.replace("\\", "/")
    return hint in normalized or fnmatch.fnmatchcase(normalized, f"*{hint}*")


def score_candidate(candidate: SymbolCandidate, file_hint: str | None, layout: SymbolLayout) -> float:
    if file_hint and file_hint_matches(candidate.file, file_hint):
        return 1.0
    if candidate.file.startswith(layout.primary_prefixes):
        score = 0.5
    elif candidate.file.startswith(layout.secondary_prefixes):
        score = 0.4
    else:
        score = 0.1
    if candidate.is_test:
        score -= _TEST_PENALTY
    score -= _SCOPE_PENALTY.get(candidate.scope, 0.0)
    return round(max(score, 0.0), 2)


def rank(candidates: list[SymbolCandidate], file_hint: str | None, layout: SymbolLayout) -> list[SymbolCandidate]:
    """Score every candidate, then sort by score, scope, path depth, file and line."""
    for candidate in candidates:
        candidate.score = score_candidate(candidate, file_hint, layout)
    return sorted(
        candidates,
        key=lambda c: (
            -c.score,
            _SCOPE_RANK.get(c.scope, len(SCOPES)),
            c.file.count("/"),
            c.file,
            c.start_line,
        ),
    )


def normalize_kind_filter(kind_filter: str | None) -> str | None:
    """None or "none" mean no filter; anything else must be a ``SymbolKind``."""
    if kind_filter is None or kind_filter == "none":
        return None
    if kind_filter not in SYMBOL_KINDS:
        raise ValueError(f"kind {kind_filter!r} is not one of {', '.join(SYMBOL_KINDS)} (or 'none')")
    return kind_filter


# ── Index ────────────────────────────────────────────────────────────────


class SymbolIndex:
    """Symbol lookup over the backend root (Python) and the frontend root (TS/JS).

    Owns the per-file Python index; TypeScript declarations come from the shared
    ``FactsCache``. Each query walks its roots, so new and deleted files are seen
    without any root-level cache.
    """

    def __init__(
        self, repo_root: Path, facts: FactsCache, layout: SymbolLayout, *, clock: Callable[[], int] = time.time_ns
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.facts = facts
        self.layout = layout
        self._clock = clock
        self._lock = threading.RLock()
        self._py: dict[str, _PyEntry] = {}

    @property
    def backend_root(self) -> Path:
        return self.repo_root / self.layout.backend_root

    @property
    def frontend_root(self) -> Path:
        return self.repo_root / self.layout.frontend_root

    def _visible_files(self) -> frozenset[str]:
        """The files git sees, so ignored build output never ranks. Raises ``RepoFilesError``."""
        return list_repo_files(self.repo_root).files

    def clear(self) -> None:
        with self._lock:
            self._py.clear()

    # Python ----------------------------------------------------------------

    def _python_entry(self, source: SourceFile) -> _PyEntry:
        key = cache_key(source.path)
        entry = self._py.get(key)
        if entry is not None and self._entry_still_valid(entry, source):
            return entry
        racy = is_racy(source.stamp, self._clock())
        by_name, problem, content_hash, imports = _index_python_file(source, digest=racy)
        entry = _PyEntry(source.stamp, racy, content_hash, by_name, problem, imports)
        # A read failure can clear without a stamp change (permissions), and a
        # racy entry without a hash cannot be validated later.
        if not (problem is not None and problem.step == "read file") and not (racy and content_hash is None):
            self._py[key] = entry
        return entry

    def _entry_still_valid(self, entry: _PyEntry, source: SourceFile) -> bool:
        """Same stamp, and for an entry stored inside the racy window, same
        content; an entry confirmed after the window drops its hash."""
        if entry.stamp != source.stamp:
            return False
        if not entry.racy:
            return True
        if _try_content_hash(source.path) != entry.content_hash:
            return False
        if not is_racy(source.stamp, self._clock()):
            entry.racy = False
            entry.content_hash = None
        return True

    def _python_files(self, problems: list[Problem]) -> list[tuple[SourceFile, _PyEntry]]:
        root = self.backend_root
        files = iter_source_files(
            root, PY_SUFFIXES, self.repo_root, problems, stage=STAGE, visible=self._visible_files()
        )
        entries = []
        for source in files:
            try:
                entry = self._python_entry(source)
            except Exception as exc:  # noqa: BLE001 - one file must not stop the index
                problems.append(
                    diagnostic("analysis_failed", stage=STAGE, step="index symbols", file=source.rel, exc=exc)
                )
                continue
            if entry.problem is not None:
                problems.append(entry.problem)
            entries.append((source, entry))
        seen = {cache_key(source.path) for source in files}
        prefix = cache_key(root) + os.sep
        for key in [k for k in self._py if k.startswith(prefix) and k not in seen]:
            del self._py[key]
        return entries

    def _class_records(self, source: SourceFile, entry: _PyEntry) -> list[ClassRec]:
        """The file's classes with their members, built once per cached entry from its symbols.

        A class-scope symbol belongs to the innermost class whose line range holds it,
        not to the class its ``container`` names: that is only a name, and a
        function-local class shares it with a module-level one.
        """
        if entry.classes is None:
            module = module_name_for(source.path, self.backend_root) or source.rel
            is_package = source.path.name == "__init__.py"
            symbols = [symbol for group in entry.by_name.values() for symbol in group]
            classes = sorted(
                (s for s in symbols if s.kind in ("class", "interface", "enum")),
                key=lambda s: (s.line, -(s.end_line or s.line)),
            )

            def innermost(symbol: PySymbol) -> PySymbol | None:
                inside = [c for c in classes if c is not symbol and c.line <= symbol.line <= (c.end_line or c.line)]
                return max(inside, key=lambda c: c.line, default=None)

            members: dict[int, dict[str, MemberRec]] = {}
            for symbol in symbols:
                if symbol.scope == "class" and (parent := innermost(symbol)) is not None:
                    members.setdefault(id(parent), {})[symbol.name] = MemberRec(symbol.line, symbol.kind)
            records: list[ClassRec] = []
            qualnames: dict[int, str] = {}
            for symbol in classes:
                parent = innermost(symbol) if symbol.scope == "class" else None
                qualname = (
                    f"{qualnames[id(parent)]}.{symbol.name}"
                    if parent is not None and id(parent) in qualnames
                    else symbol.name
                )
                qualnames[id(symbol)] = qualname
                own = dict(members.get(id(symbol), {}))
                own.update((attr, MemberRec(symbol.line, "variable")) for attr in symbol.instance_attrs)
                records.append(
                    ClassRec(
                        symbol.name,
                        qualname,
                        source.rel,
                        module,
                        symbol.line,
                        symbol.end_line or symbol.line,
                        tuple(_resolve_base(path, module, is_package, entry) for path in symbol.bases),
                        symbol.kind == "interface",
                        symbol.scope == "local",
                        own,
                    )
                )
            entry.classes = records
        return entry.classes

    def _annotate_protocols(
        self, candidates: list[SymbolCandidate], entries: list[tuple[SourceFile, _PyEntry]]
    ) -> None:
        """Set ``protocol_method`` and ``protocols`` on the Python method candidates."""
        methods = [c for c in candidates if c.kind == "method" and c.scope == "class"]
        if not methods:
            return
        table = ClassTable(rec for source, entry in entries for rec in self._class_records(source, entry))
        for candidate in methods:
            owner = table.owner_of(candidate.file, candidate.start_line)
            if owner is None:
                continue
            candidate.protocol_method = owner.protocol
            candidate.protocols = table.matching_protocols(owner, candidate.name)

    def python_candidates(self, name: str, kind_filter: str | None, problems: list[Problem]) -> list[SymbolCandidate]:
        out = []
        entries = self._python_files(problems)
        for source, entry in entries:
            for symbol in entry.by_name.get(name, ()):
                if kind_filter is not None and symbol.kind != kind_filter:
                    continue
                out.append(
                    SymbolCandidate(
                        name=symbol.name,
                        file=source.rel,
                        start_line=symbol.line,
                        end_line=symbol.end_line,
                        kind=symbol.kind,
                        signature=symbol.signature,
                        language="python",
                        scope=symbol.scope,
                        container=symbol.container,
                        exported=None,
                        is_test=is_test_path(source.rel),
                    )
                )
        self._annotate_protocols(out, entries)
        return out

    def python_symbols(self, problems: list[Problem]) -> list[tuple[str, PySymbol]]:
        """Every indexed Python symbol as ``(rel, symbol)``."""
        with self._lock:
            return [
                (source.rel, symbol)
                for source, entry in self._python_files(problems)
                for symbols in entry.by_name.values()
                for symbol in symbols
            ]

    # TypeScript ------------------------------------------------------------

    def _typescript_files(self, problems: list[Problem]) -> list[tuple[SourceFile, tuple[TsDecl, ...]]]:
        missing_root = frontend_not_found_problem(self.repo_root, self.layout.frontend_root)
        if missing_root is not None:
            raise ToolchainUnavailable(missing_root)
        root = self.frontend_root
        files = iter_source_files(
            root,
            TS_SUFFIXES,
            self.repo_root,
            problems,
            stage=STAGE,
            generated_parent=root,
            visible=self._visible_files(),
        )
        by_rel = self.facts.facts_for(files, root=root, suffixes=TS_SUFFIXES)
        out = []
        for source in files:
            file_facts = by_rel[source.rel]
            problems.extend(facts_problems(file_facts, stage=STAGE))
            out.append((source, file_facts.decls))
        return out

    def typescript_candidates(
        self, name: str, kind_filter: str | None, problems: list[Problem]
    ) -> list[SymbolCandidate]:
        out = []
        for source, decls in self._typescript_files(problems):
            for decl in decls:
                if decl.name != name or (kind_filter is not None and decl.kind != kind_filter):
                    continue
                out.append(
                    SymbolCandidate(
                        name=decl.name,
                        file=source.rel,
                        start_line=decl.line,
                        end_line=decl.end_line,
                        kind=decl.kind,
                        signature=decl.signature,
                        language="typescript",
                        scope=decl.scope,
                        container=decl.container,
                        exported=decl.exported,
                        is_test=is_test_path(source.rel),
                    )
                )
        return out

    def typescript_decls(self, problems: list[Problem]) -> list[tuple[str, TsDecl]]:
        """Every TypeScript declaration as ``(rel, decl)``."""
        return [(source.rel, decl) for source, decls in self._typescript_files(problems) for decl in decls]

    def _resolve_both(
        self, name: str, kind: str | None, candidates: list[SymbolCandidate], problems: list[Problem]
    ) -> ToolchainProblem | None:
        """Both sides into ``candidates``; the frontend's problem when it could not be searched."""
        frontend_problems: list[Problem] = []
        frontend_problem: ToolchainProblem | None = None
        # The helper is a separate process, so the Python index builds while it runs.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.typescript_candidates, name, kind, frontend_problems)
            candidates.extend(self.python_candidates(name, kind, problems))
            try:
                candidates.extend(future.result())
            except ToolchainUnavailable as exc:
                frontend_problem = exc.problem
            except HelperError as exc:
                frontend_problem = exc.problem()
        if frontend_problem is not None:
            frontend_problems.append(
                diagnostic(
                    "frontend_unavailable",
                    stage=STAGE,
                    step="run typescript helper (facts mode)",
                    file=frontend_problem.missing,
                    message=f"frontend symbols were not searched: {frontend_problem.message}",
                    missing=frontend_problem.missing,
                    fix=frontend_problem.fix,
                    problem=frontend_problem.code,
                )
            )
        problems.extend(frontend_problems)
        return frontend_problem

    # Query -----------------------------------------------------------------

    def resolve(
        self,
        name: str,
        *,
        side: str = "both",
        kind_filter: str | None = None,
        file_hint: str | None = None,
    ) -> SymbolResult:
        """Ranked candidates whose name equals ``name`` exactly.

        ``side`` is ``backend``, ``frontend`` or ``both``. With ``frontend`` a
        missing frontend root or toolchain raises ``ToolchainUnavailable``
        (code ``frontend_not_found`` or the probe's code) and a failed helper
        run ``HelperError``; with ``both`` any of them keeps the backend
        candidates, with ``complete`` False, a ``frontend_unavailable``
        diagnostic carrying ``problem`` (the code), ``missing`` and ``fix``, and
        ``frontend_problem`` set (code ``helper_failed`` for a failed helper run).
        """
        if side not in SIDES:
            raise ValueError(f"side {side!r} is not one of {', '.join(SIDES)}")
        kind = normalize_kind_filter(kind_filter)
        problems: list[Problem] = []
        candidates: list[SymbolCandidate] = []
        frontend_problem: ToolchainProblem | None = None
        with self._lock:
            if side == "backend":
                candidates.extend(self.python_candidates(name, kind, problems))
            elif side == "frontend":
                candidates.extend(self.typescript_candidates(name, kind, problems))
            else:
                frontend_problem = self._resolve_both(name, kind, candidates, problems)
        diagnostics, _ = partition(problems)
        return SymbolResult(
            candidates=rank(candidates, file_hint, self.layout),
            complete=not diagnostics,
            problems=problems,
            frontend_problem=frontend_problem,
        )


# ── CLI ──────────────────────────────────────────────────────────────────


def _envelope(problems: list[Problem], **payload: object) -> dict:
    diagnostics, limitations = partition(problems)
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": not diagnostics,
        "diagnostics": [p.as_dict() for p in diagnostics],
        "analysis_limitations": [p.as_dict() for p in limitations],
        **payload,
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("name", nargs="?", help="exact symbol name")
    target.add_argument("--dump-ts", action="store_true", help="every TypeScript declaration under the frontend root")
    target.add_argument("--dump-py", action="store_true", help="every Python symbol under the backend root")
    parser.add_argument("--side", choices=SIDES, default="both")
    parser.add_argument("--kind", choices=(*SYMBOL_KINDS, "none"))
    parser.add_argument("--file-hint")
    parser.add_argument("--json", action="store_true")


def _dump_body(problems: list[Problem], key: str, rows: list[dict]) -> dict:
    counts = Counter(row["kind"] for row in rows)
    return _envelope(problems, counts=dict(sorted(counts.items())), **{key: rows})


def _cli_body(args: argparse.Namespace, index: SymbolIndex) -> tuple[dict, list[Problem]]:
    """The JSON body for the CLI arguments and the problems found building it."""
    problems: list[Problem] = []
    if args.dump_ts:
        rows = [
            {"file": rel, **asdict(decl), "is_test": is_test_path(rel)}
            for rel, decl in index.typescript_decls(problems)
        ]
        return _dump_body(problems, "decls", rows), problems
    if args.dump_py:
        rows = [
            {"file": rel, **asdict(symbol), "is_test": is_test_path(rel)}
            for rel, symbol in index.python_symbols(problems)
        ]
        rows.sort(key=lambda r: (r["file"], r["line"], r["column"], r["name"]))
        return _dump_body(problems, "symbols", rows), problems
    result = index.resolve(args.name, side=args.side, kind_filter=args.kind, file_hint=args.file_hint)
    return _envelope(result.problems, candidates=[asdict(c) for c in result.candidates]), result.problems


def _print_text(body: dict, problems: list[Problem], *, dump: bool) -> None:
    for problem in problems:
        print(f"warning: {problem.message}", file=sys.stderr)
    if dump:
        for kind, count in body["counts"].items():
            print(f"{kind}: {count}")
        print(f"total: {sum(body['counts'].values())}")
        return
    for candidate in body["candidates"]:
        where = f"{candidate['file']}:{candidate['start_line']}-{candidate['end_line']}"
        print(f"{candidate['score']:.2f} {candidate['kind']} ({candidate['scope']}) {where}  {candidate['signature']}")


def run(args: argparse.Namespace, context: Context) -> int:
    repo_root = context.repo_root
    toolchain = find_toolchain(repo_root)
    index = SymbolIndex(repo_root, FactsCache(repo_root, lambda: toolchain), SymbolLayout.from_config(context.config))
    try:
        body, problems = _cli_body(args, index)
    except ToolchainUnavailable as exc:
        print(f"code-intel: frontend toolchain unavailable: {exc}", file=sys.stderr)
        return 2
    except HelperError as exc:
        problem = exc.problem()
        print(f"code-intel: {problem.message}. Fix: {problem.fix}", file=sys.stderr)
        return 2
    except RepoFilesError as exc:
        print(f"code-intel: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(body, ensure_ascii=False))
    else:
        _print_text(body, problems, dump=args.dump_ts or args.dump_py)
    return 0
