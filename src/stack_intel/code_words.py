"""Identifier words that occur as code, per file, for resolving symbol mentions in docs.

A name a doc puts in a code span passes the symbol check when some tracked code
file really uses it. "Really" is the point: Python is read with ``ast``, so a
name that survives only in a comment or docstring (the drift the check exists
to catch, such as a removed helper still described in a docstring) does not
count, while names inside ordinary string constants do (settings keys, dotted
import paths). Other code files have no parser here and contribute every
identifier-shaped word.

Dotted texts (``HeartbeatConfig.pg_max_retry_seconds``) are kept as wholes,
every contiguous window of two or more parts of each attribute chain, so a
dotted mention can match as written instead of part by part.

The other side of a dotted mention is what a Python module binds at top
level: each file's ``CodeWords.top``, joined across files (star imports
followed) by ``module_bindings`` so ``module.Name`` reaches them, and
``package_bindings_from_bytes`` for a package's ``__init__.py`` alone.

Functions are pure over ``(rel, text)`` or ``(rel, bytes)``: no git, no
filesystem, so the runtime can cache each file's words by its stamp.
"""

from __future__ import annotations

import ast
import builtins
import io
import posixpath
import re
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import NamedTuple

from stack_intel.doc_rules import STAGE
from stack_intel.problems import Problem, diagnostic

# Code files whose identifiers count. Lockfiles are left out: they list package
# names, not code, and would make almost any word resolve.
CODE_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".cjs",
    ".mjs",
    ".sql",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".sh",
    ".proto",
    ".css",
    ".ini",
    ".cfg",
)
CODE_FILE_NAMES = frozenset({".env.example"})
CODE_FILE_PREFIXES: tuple[str, ...] = ("Dockerfile",)
LOCKFILE_NAMES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock"}
)

BUILTIN_WORDS: frozenset[str] = frozenset(dir(builtins))

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOTTED_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
# ast.parse raises RecursionError or MemoryError on deeply nested expressions
# as well as SyntaxError (ValueError for NUL bytes).
_PARSE_ERRORS: tuple[type[BaseException], ...] = (SyntaxError, ValueError, RecursionError, MemoryError)
# A bad or unknown coding cookie raises SyntaxError from detect_encoding,
# LookupError for an unknown codec, UnicodeDecodeError for rejected bytes.
_DECODE_ERRORS: tuple[type[Exception], ...] = (SyntaxError, LookupError, UnicodeDecodeError)
# Node fields that hold identifiers: Name.id, Attribute.attr, arg.arg,
# keyword.arg, alias.name/asname, ImportFrom.module, the name of a def, class,
# except handler, match capture or type parameter, Global/Nonlocal.names, and
# MatchMapping.rest / MatchClass.kwd_attrs.
_IDENTIFIER_FIELDS: tuple[str, ...] = ("id", "attr", "arg", "name", "asname", "module", "names", "rest", "kwd_attrs")
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class CodeWords:
    """``words``: single identifiers. ``dotted``: dotted texts of two or more
    parts. ``top``: what a Python file binds at top level; None for a file
    that is not Python or does not parse, and for a union of files."""

    words: frozenset[str]
    dotted: frozenset[str]
    top: TopLevel | None = None


EMPTY = CodeWords(frozenset(), frozenset())


def is_code_file(rel: str) -> bool:
    """Whether a repo-relative path's identifiers feed the word table."""
    name = posixpath.basename(rel)
    if name in LOCKFILE_NAMES:
        return False
    return name in CODE_FILE_NAMES or name.startswith(CODE_FILE_PREFIXES) or name.endswith(CODE_SUFFIXES)


def is_python_file(rel: str) -> bool:
    return rel.endswith(".py")


def _windows(parts: Sequence[str]) -> Iterable[str]:
    """Every contiguous run of two or more parts, joined by dots."""
    for start in range(len(parts) - 1):
        for end in range(start + 2, len(parts) + 1):
            yield ".".join(parts[start:end])


def _add_text(text: str, words: set[str], dotted: set[str]) -> None:
    words.update(_WORD_RE.findall(text))
    for match in _DOTTED_RE.findall(text):
        dotted.update(_windows(match.split(".")))


def plain_words(text: str) -> CodeWords:
    """Every identifier-shaped word and dotted text, for files without a parser here."""
    words: set[str] = set()
    dotted: set[str] = set()
    _add_text(text, words, dotted)
    return CodeWords(frozenset(words), frozenset(dotted))


def _chain(node: ast.Attribute) -> list[str]:
    """``a.b.c`` → ``["a", "b", "c"]``; a chain on a call or subscript stops
    there (``f().b.c`` → ``["b", "c"]``)."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    parts.reverse()
    return parts


def _add_identifier(value: str, words: set[str], dotted: set[str]) -> None:
    # Import names (`a.b.c`) and ImportFrom.module arrive dotted.
    parts = value.split(".")
    words.update(part for part in parts if part)
    if len(parts) > 1:
        dotted.update(_windows(parts))


# Per node class: (its identifier fields, its fields that hold child nodes).
_NODE_FIELDS: dict[type, tuple[tuple[str, ...], tuple[str, ...]]] = {}


def _node_fields(cls: type) -> tuple[tuple[str, ...], tuple[str, ...]]:
    fields = _NODE_FIELDS.get(cls)
    if fields is None:
        names: tuple[str, ...] = getattr(cls, "_fields", ())
        fields = (tuple(n for n in _IDENTIFIER_FIELDS if n in names), names)
        _NODE_FIELDS[cls] = fields
    return fields


def python_tree_words(tree: ast.AST) -> CodeWords:
    """Identifiers of a parsed Python module (see the module docstring).

    One walk over the tree, parents before children, so a docstring and an
    inner link of an attribute chain are known before they are reached; the
    load/store context nodes, which name nothing, are not visited. This is
    the docs check's cold-run hot spot (every tracked Python file)."""
    words: set[str] = set()
    dotted: set[str] = set()
    docstrings: set[int] = set()
    # Inner links of an attribute chain are covered by the outermost one.
    inner_attrs: set[int] = set()
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        identifier_fields, child_fields = _node_fields(type(node))
        for name in identifier_fields:
            value = getattr(node, name, None)
            if isinstance(value, str):
                _add_identifier(value, words, dotted)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        _add_identifier(item, words, dotted)
        if isinstance(node, ast.Attribute):
            inner_attrs.add(id(node.value))
            if id(node) not in inner_attrs:
                parts = _chain(node)
                if len(parts) > 1:
                    dotted.update(_windows(parts))
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name != "*":
                    dotted.update(_windows([*node.module.split("."), alias.name]))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in docstrings:
                _add_text(node.value, words, dotted)
        elif isinstance(node, _DOCSTRING_OWNERS) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
        for name in child_fields:
            value = getattr(node, name, None)
            if isinstance(value, ast.AST):
                if not isinstance(value, ast.expr_context):
                    stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, ast.AST))
    return CodeWords(frozenset(words), frozenset(dotted))


def python_words(rel: str, text: str, *, stage: str = STAGE) -> tuple[CodeWords, list[Problem]]:
    """Words of Python source. A file that does not parse yields a
    ``parse_failed`` diagnostic and falls back to ``plain_words``, which also
    reads comments and docstrings, so the result may be too lenient for that
    file; the diagnostic marks the check incomplete."""
    try:
        tree = ast.parse(text, filename=rel)
    except _PARSE_ERRORS as exc:
        return plain_words(text), [diagnostic("parse_failed", stage=stage, step="parse file", file=rel, exc=exc)]
    words = python_tree_words(tree)
    return CodeWords(words.words, words.dotted, top_level(tree)), []


def code_words(rel: str, text: str, *, stage: str = STAGE) -> tuple[CodeWords, list[Problem]]:
    """Words of one code file: ``ast`` for Python, plain words otherwise."""
    if is_python_file(rel):
        return python_words(rel, text, stage=stage)
    return plain_words(text), []


def code_words_from_bytes(rel: str, data: bytes, *, stage: str = STAGE) -> tuple[CodeWords, list[Problem]]:
    """``code_words`` on raw bytes. Python is decoded the way the interpreter
    does (PEP 263, BOM) and a failure is a ``decode_failed`` diagnostic. Other
    files are read as UTF-8 with undecodable bytes replaced: identifier words
    are ASCII, so a stray byte cannot hide one."""
    if not is_python_file(rel):
        return plain_words(data.decode("utf-8", "replace")), []
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        text = data.decode(encoding)
    except _DECODE_ERRORS as exc:
        return EMPTY, [diagnostic("decode_failed", stage=stage, step="decode file", file=rel, exc=exc)]
    return python_words(rel, text, stage=stage)


def _bound_targets(target: ast.expr) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_targets(element.value if isinstance(element, ast.Starred) else element)


@dataclass(frozen=True)
class TopLevel:
    """What a Python module binds at top level (``top_level``). ``names``:
    imports (``import a.b`` binds ``a``, an ``as`` name replaces the
    original), defs, classes, assignments and the names a literal ``__all__``
    lists. ``all``: those ``__all__`` names, None without an ``__all__``.
    ``all_dynamic``: ``__all__`` is also built some way that is not a literal
    list, so what a star import of the module takes is unknown. ``stars``: the
    ``(level, module)`` of each star import, which ``module_bindings``
    follows. ``getattr``: a module ``__getattr__`` may serve any other name."""

    names: frozenset[str] = frozenset()
    all: frozenset[str] | None = None
    all_dynamic: bool = False
    stars: tuple[tuple[int, str], ...] = ()
    getattr: bool = False


@dataclass
class _Scan:
    names: set[str] = field(default_factory=set)
    all: set[str] | None = None
    all_dynamic: bool = False
    stars: list[tuple[int, str]] = field(default_factory=list)
    getattr: bool = False

    def add_all(self, value: ast.expr | None) -> None:
        if self.all is None:
            self.all = set()
        if not isinstance(value, (ast.List, ast.Tuple)):
            self.all_dynamic = True
            return
        for element in value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                self.all.add(element.value)
            else:
                self.all_dynamic = True


def _is_all(target: ast.expr) -> bool:
    return isinstance(target, ast.Name) and target.id == "__all__"


def _scan(body: Sequence[ast.stmt], scan: _Scan) -> None:
    names = scan.names
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_is_all(target) for target in targets) and node.value is not None:
                scan.add_all(node.value)
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
            if any(alias.name == "*" for alias in node.names):
                scan.stars.append((node.level, node.module or ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            scan.getattr = scan.getattr or node.name == "__getattr__"
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_bound_targets(target))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names.update(_bound_targets(node.target))
        elif isinstance(node, ast.Expr) and isinstance(call := node.value, ast.Call):
            # `__all__.append(...)`, `__all__.extend(...)`
            if isinstance(call.func, ast.Attribute) and _is_all(call.func.value):
                scan.all_dynamic = True
        elif isinstance(node, ast.If):
            _scan(node.body, scan)
            _scan(node.orelse, scan)
        elif isinstance(node, (ast.Try, ast.TryStar)):
            for part in (node.body, *(handler.body for handler in node.handlers), node.orelse, node.finalbody):
                _scan(part, scan)


def top_level(tree: ast.Module) -> TopLevel:
    """``TopLevel`` of a parsed module, also under a top-level ``if``
    (``TYPE_CHECKING``) or ``try``."""
    scan = _Scan()
    _scan(tree.body, scan)
    names = scan.names | (scan.all or set())
    return TopLevel(
        frozenset(names),
        None if scan.all is None else frozenset(scan.all),
        scan.all_dynamic,
        tuple(scan.stars),
        scan.getattr,
    )


def package_bindings_from_bytes(rel: str, data: bytes, *, stage: str = STAGE) -> tuple[frozenset[str], list[Problem]]:
    """``TopLevel.names`` of a package's ``__init__.py``; star imports are not
    followed. A file that cannot be decoded or parsed binds nothing, with a
    diagnostic."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        text = data.decode(encoding)
    except _DECODE_ERRORS as exc:
        return frozenset(), [diagnostic("decode_failed", stage=stage, step="decode file", file=rel, exc=exc)]
    try:
        tree = ast.parse(text, filename=rel)
    except _PARSE_ERRORS as exc:
        return frozenset(), [diagnostic("parse_failed", stage=stage, step="parse file", file=rel, exc=exc)]
    return top_level(tree).names, []


class BoundModule(NamedTuple):
    """A Python module's file, the names it binds at top level with its star
    imports followed, and whether it may bind names those do not show
    (``open``): a module ``__getattr__``, or a star import of a module that
    is not tracked, is ambiguous or builds its ``__all__`` at run time."""

    file: str
    names: frozenset[str]
    open: bool


ModuleBindings = Mapping[tuple[str, ...], tuple[BoundModule, ...]]
"""Every trailing run of a Python module's dotted path (``errors``,
``pkg.errors``, ``src.pkg.errors`` for ``src/pkg/errors.py``) → the modules
it ends."""
NO_MODULES: ModuleBindings = MappingProxyType({})


def _module_key(file: str) -> tuple[str, ...]:
    parts = posixpath.splitext(file)[0].split("/")
    return tuple(parts[:-1] if parts[-1] == "__init__" else parts)


IMPORT_ROOTS: tuple[str, ...] = ("", "src")


def _star_targets(tops: Mapping[str, TopLevel], roots: Sequence[str]) -> dict[str, list[str | None]]:
    """Each file's star-import targets in order, None for one that is not a
    single tracked module. A relative import resolves from the file's
    package; an absolute one only from an import root (``roots``), so a
    tracked ``lib/enum.py`` does not stand in for the standard library's
    ``enum``."""
    exact: dict[tuple[str, ...], str] = {}
    # A package's `__init__.py` first: the import system picks the package
    # over a same-named `.py` beside it.
    for file in sorted(tops, key=lambda f: (not f.endswith("/__init__.py"), f)):
        exact.setdefault(_module_key(file), file)
    root_parts = [tuple(root.strip("/").split("/")) if root.strip("/") else () for root in dict.fromkeys(roots)]
    out: dict[str, list[str | None]] = {}
    for file, top in tops.items():
        targets: list[str | None] = []
        for level, module in top.stars:
            wanted = tuple(module.split(".")) if module else ()
            if level == 0:
                found = {exact[key] for root in root_parts if (key := (*root, *wanted)) in exact}
                targets.append(found.pop() if len(found) == 1 else None)
                continue
            package = posixpath.dirname(file).split("/") if posixpath.dirname(file) else []
            if level - 1 > len(package):
                targets.append(None)
                continue
            targets.append(exact.get((*package[: len(package) - (level - 1)], *wanted)))
        out[file] = targets
    return out


def _components(nodes: Sequence[str], edges: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components, each after every component it
    reaches; iterative, so a long import chain does not exhaust the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            successors = edges[node]
            if position < len(successors):
                work.append((node, position + 1))
                successor = successors[position]
                if successor not in index:
                    work.append((successor, 0))
                elif successor in on_stack:
                    low[node] = min(low[node], index[successor])
                continue
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                out.append(component)
    return out


def _resolved(tops: Mapping[str, TopLevel], roots: Sequence[str]) -> dict[str, tuple[frozenset[str], bool]]:
    """Each file's names with its star imports followed, and whether it is
    open (``BoundModule.open``). Modules that star-import each other share
    what they export, settled by iterating to a fixed point; the work is
    linear in modules and imports otherwise."""
    targets = _star_targets(tops, roots)
    files = sorted(tops)
    edges = {file: sorted({t for t in targets[file] if t is not None}) for file in files}
    done: dict[str, tuple[frozenset[str], bool]] = {}

    def exports(target: str, names: frozenset[str]) -> frozenset[str]:
        source = tops[target]
        if source.all is not None:
            return source.all
        return frozenset(n for n in names if not n.startswith("_"))

    for component in _components(files, edges):
        names = {file: set(tops[file].names) for file in component}
        is_open = {
            file: tops[file].getattr
            or None in targets[file]
            or any(t is not None and tops[t].all_dynamic for t in targets[file])
            for file in component
        }
        changed = True
        while changed:
            changed = False
            for file in component:
                for target in edges[file]:
                    if target in done:
                        target_names, target_open = done[target]
                    else:
                        target_names, target_open = frozenset(names[target]), is_open[target]
                    added = exports(target, target_names) - names[file]
                    if added or (target_open and not is_open[file]):
                        names[file] |= added
                        is_open[file] = is_open[file] or target_open
                        changed = True
        for file in component:
            done[file] = (frozenset(names[file]), is_open[file])
    return done


def module_bindings(tops: Mapping[str, TopLevel], roots: Sequence[str] = IMPORT_ROOTS) -> ModuleBindings:
    """``ModuleBindings`` from each tracked Python file's ``TopLevel``; a
    package's ``__init__.py`` stands for its directory. ``roots``: the
    directories an absolute import resolves from."""
    resolved = _resolved(tops, roots)
    index: dict[tuple[str, ...], list[BoundModule]] = {}
    for file in sorted(tops):
        names, is_open = resolved[file]
        key = _module_key(file)
        for start in range(len(key)):
            index.setdefault(key[start:], []).append(BoundModule(file, names, is_open))
    return MappingProxyType({key: tuple(value) for key, value in index.items()})


def bound_by_module(parts: Sequence[str], modules: ModuleBindings) -> str | None:
    """The first file of a module whose dotted path ends with ``parts[:-1]``
    and that binds ``parts[-1]`` at top level: ``pkg.errors.AuthState`` is
    bound by ``pkg/errors.py`` when it imports ``AuthState``, wherever that
    class is defined."""
    return next((m.file for m in modules.get(tuple(parts[:-1]), ()) if parts[-1] in m.names), None)


def open_module(parts: Sequence[str], modules: ModuleBindings) -> str | None:
    """The first file of a module whose dotted path ends with ``parts[:-1]``
    and that may bind names it does not show (``BoundModule.open``)."""
    return next((m.file for m in modules.get(tuple(parts[:-1]), ()) if m.open), None)
