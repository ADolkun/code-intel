"""The targets a Makefile defines, for the docs check's make references.

The reader takes target names from rule lines, from every branch of every
conditional, since a union cannot make a defined target look missing. Anything
that can define a target the reader cannot see makes the set incomplete, and
``MakeTargets.opaque`` says why: an include, a pattern or suffix rule,
``.DEFAULT``, ``define`` or ``$(eval)``, ``vpath``, or a target name it
cannot expand. A target absent from an incomplete set is not proven missing.

Whether a tab-indented line is a recipe depends on whether a rule is open,
which the reader does not track. So the checks that guard against hidden
definitions (``include``, ``$(eval)``, ``define`` and how often a variable is
assigned) look at every line, tab-indented or not.

A target name built from variables is expanded only when each variable it uses
is assigned exactly once in the whole file, outside a conditional, before the
rule, from literal text and such variables, and only up to ``_MAX_EXPANSION``
characters and references of expanded text in all: past that the file is opaque.
``MakeTargets.variables`` tells the caller that names depend on variables,
which a command line or the environment may set.

Of the special targets only ``.PHONY`` makes its prerequisites goals make
accepts without a rule. A target-specific variable line (``t: X = 1``)
defines nothing unless a rule names the target too.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

# GNU make's lookup order when no -f is given.
MAKEFILE_NAMES: tuple[str, ...] = ("GNUmakefile", "makefile", "Makefile")

_SPECIAL_TARGETS = frozenset(
    """.PHONY .SUFFIXES .PRECIOUS .INTERMEDIATE .NOTINTERMEDIATE .SECONDARY .SECONDEXPANSION
    .DELETE_ON_ERROR .IGNORE .LOW_RESOLUTION_TIME .SILENT .EXPORT_ALL_VARIABLES .NOTPARALLEL .ONESHELL
    .POSIX .WAIT""".split()
)
# GNU make 4.4's default .SUFFIXES list.
_DEFAULT_SUFFIXES = frozenset(
    """.out .a .ln .o .c .cc .C .cpp .p .f .F .m .r .y .l .ym .yl .s .S .mod .sym .def .h .info .dvi
    .tex .texinfo .texi .txinfo .w .ch .web .sh .elc .el""".split()
)
_SUFFIX_RULE_RE = re.compile(r"(\.[^.\s/%]+)(\.[^.\s/%]+)?")
_CONDITIONALS = frozenset({"ifeq", "ifneq", "ifdef", "ifndef"})
_INCLUDES = frozenset({"include", "-include", "sinclude"})
_MODIFIERS = frozenset({"export", "override", "private", "unexport"})
_REFERENCE_RE = re.compile(r"\$(?:\(([A-Za-z0-9_.-]+)\)|\{([A-Za-z0-9_.-]+)\})")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_DEPTH = 20
_MAX_EXPANSION = 100_000


@dataclass(frozen=True)
class MakeTargets:
    """``targets``: every name a rule line (or a special target's
    prerequisite list) defines, without a leading ``./``. ``opaque``: why the
    set may be incomplete, None when it is complete. ``variables``: the
    variables the expanded target names were built from."""

    targets: frozenset[str]
    opaque: str | None = None
    variables: frozenset[str] = frozenset()


def strip_dot_slash(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """``(first line number, line)`` with backslash-newline continuations joined."""
    pending: list[str] = []
    start = 1
    for number, raw in enumerate(text.splitlines(), start=1):
        if not pending:
            start = number
        if raw.endswith("\\") and not raw.endswith("\\\\"):
            pending.append(raw[:-1])
            continue
        pending.append(raw)
        yield start, " ".join(part if index == 0 else part.lstrip() for index, part in enumerate(pending))
        pending = []
    if pending:
        yield start, " ".join(pending)


def _strip_comment(line: str) -> str:
    index = 0
    while (index := line.find("#", index)) != -1:
        if index == 0 or line[index - 1] != "\\":
            return line[:index]
        index += 1
    return line


@dataclass(frozen=True)
class _Split:
    """A line split at its first ``:`` or assignment operator outside ``$(...)``:
    an assignment (``head`` is the variable, ``op`` the operator) or a rule
    (``head`` the targets, ``op`` ``:`` or ``::``); ``rest`` follows the operator."""

    kind: Literal["assign", "rule"]
    head: str
    op: str
    rest: str


def _split(line: str) -> _Split | None:
    depth = 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == "$" and line[index + 1 : index + 2] in ("(", "{"):
            depth += 1
            index += 2
            continue
        if depth and char in ")}":
            depth -= 1
        elif not depth:
            if char == ":":
                for op in (":::=", "::=", ":="):
                    if line.startswith(op, index):
                        return _Split("assign", line[:index], op, line[index + len(op) :])
                op = "::" if line.startswith("::", index) else ":"
                return _Split("rule", line[:index], op, line[index + len(op) :])
            if char == "=":
                return _Split("assign", line[:index], "=", line[index + 1 :])
            if char in "?+!" and line[index + 1 : index + 2] == "=":
                return _Split("assign", line[:index], char + "=", line[index + 2 :])
        index += 1
    return None


def _words(raw: str) -> list[str]:
    words = _strip_comment(raw).split()
    while words and words[0] in _MODIFIERS:
        words = words[1:]
    return words


class _Variables:
    """The variables known so far, for expanding target names: a name maps to
    its text (a recursive one expands on use), or None when its value is not
    known: assigned more than once in the file, appended to, assigned in a
    conditional or by a shell. ``assignments`` counts every assignment line
    of the file, tab-indented ones included."""

    def __init__(self, assignments: dict[str, int]) -> None:
        self.values: dict[str, tuple[str, bool] | None] = {}
        self.assignments = assignments
        self.budget = _MAX_EXPANSION

    def assign(self, name: str, op: str, value: str, conditional: bool) -> None:
        if conditional or op in ("+=", "!=") or name in self.values or self.assignments.get(name, 0) > 1:
            self.values[name] = None
        elif op in ("=", "?="):
            self.values[name] = (value, True)
        else:
            expanded = self.expand(value, set())
            self.values[name] = (expanded, False) if expanded is not None else None

    def expand(self, text: str, used: set[str], depth: int = 0) -> str | None:
        """``text`` with every variable reference replaced, None when one is
        not known or the expansion budget is spent."""
        if depth > _MAX_DEPTH or self.budget < 0:
            return None
        out: list[str] = []
        position = 0
        for match in _REFERENCE_RE.finditer(text):
            name = match.group(1) or match.group(2)
            entry = self.values.get(name)
            if entry is None:
                return None
            value, recursive = entry
            if recursive:
                expanded = self.expand(value, used, depth + 1)
                if expanded is None:
                    return None
                value = expanded
            used.add(name)
            # One per reference too, so empty values still spend the budget.
            self.budget -= len(value) + 1
            if self.budget < 0:
                return None
            out.append(text[position : match.start()])
            out.append(value)
            position = match.end()
        out.append(text[position:])
        result = "".join(out)
        return None if "$" in result else result


class _Reader:
    def __init__(self, lines: list[tuple[int, str]]) -> None:
        self.targets: set[str] = set()
        self.used: set[str] = set()
        self.opaque: str | None = None
        self.depth = 0
        self.in_define = False
        assignments: dict[str, int] = {}
        self.suffixes: set[str] = set(_DEFAULT_SUFFIXES)
        self.any_suffix = False
        for number, raw in lines:
            self._scan(number, raw, assignments)
        self.variables = _Variables(assignments)

    def unknowable(self, line: int, reason: str) -> None:
        if self.opaque is None:
            self.opaque = f"has {reason} (line {line})"

    def _scan(self, number: int, raw: str, assignments: dict[str, int]) -> None:
        """What every line counts for, recipe or not: hidden definitions,
        assignment counts and ``.SUFFIXES`` additions."""
        words = _words(raw)
        if not words:
            return
        if words[0] in _INCLUDES:
            self.unknowable(number, words[0])
        if words[0] == "define":
            self.unknowable(number, "define")
        if "$(eval" in raw or "${eval" in raw:
            self.unknowable(number, "$(eval)")
        split = _split(" ".join(words))
        if split is None:
            return
        if split.kind == "assign":
            name = split.head.strip()
            assignments[name] = assignments.get(name, 0) + 1
        elif split.head.split() == [".SUFFIXES"]:
            listed = split.rest.split(";", 1)[0].split()
            self.any_suffix = self.any_suffix or any("$" in word for word in listed)
            self.suffixes.update(listed)

    def read(self, lines: list[tuple[int, str]]) -> MakeTargets:
        for number, raw in lines:
            self.line(number, raw)
        return self.finish()

    def finish(self) -> MakeTargets:
        if self.variables.budget < 0:
            self.opaque = "has variables too large to expand"
        return MakeTargets(frozenset(self.targets), self.opaque, frozenset(self.used))

    def line(self, number: int, raw: str) -> None:
        if self.in_define:
            if raw.strip().split()[:1] == ["endef"]:
                self.in_define = False
            return
        if raw.startswith("\t"):
            return
        words = _words(raw)
        first = words[0] if words else ""
        if first in _CONDITIONALS:
            self.depth += 1
            return
        if first == "endif":
            self.depth = max(0, self.depth - 1)
            return
        if first == "define":
            self.in_define = True
            return
        if first in ("else", *_INCLUDES):
            return
        if first in ("vpath", "load", "-load"):
            self.unknowable(number, first)
            return
        split = _split(" ".join(words))
        if split is None:
            return
        if split.kind == "assign":
            self.assign(number, split)
        else:
            self.rule(number, split.head, split.rest)

    def assign(self, number: int, split: _Split) -> None:
        name = split.head.strip()
        if _VARIABLE_NAME_RE.fullmatch(name) is None:
            self.unknowable(number, "a variable whose name is computed")
            return
        if name == "VPATH":
            self.unknowable(number, "VPATH")
        self.variables.assign(name, split.op, split.rest.strip(), self.depth > 0)

    def rule(self, number: int, head: str, rest: str) -> None:
        names = self.expand(number, head)
        prerequisites = rest.split(";", 1)[0]
        if names is None:
            return
        split = _split(prerequisites)
        if split is not None and split.kind == "assign":
            return
        if split is not None and split.kind == "rule" and "%" in prerequisites:
            self.unknowable(number, "a static pattern rule")
        for name in names:
            if "%" in name:
                self.unknowable(number, "a pattern rule")
            elif name == ".DEFAULT":
                self.unknowable(number, "a .DEFAULT rule")
            elif name in _SPECIAL_TARGETS:
                self.targets.add(name)
                if name == ".PHONY":
                    listed = self.expand(number, prerequisites.replace("|", " "))
                    self.targets.update(strip_dot_slash(item) for item in listed or ())
            elif self.is_suffix_rule(name):
                self.unknowable(number, f"an old-style suffix rule {name}")
            else:
                self.targets.add(strip_dot_slash(name))

    def is_suffix_rule(self, name: str) -> bool:
        match = _SUFFIX_RULE_RE.fullmatch(name)
        if match is None:
            return False
        return self.any_suffix or all(part in self.suffixes for part in match.groups() if part is not None)

    def expand(self, number: int, text: str) -> list[str] | None:
        if "$" not in text:
            return text.split()
        used: set[str] = set()
        expanded = self.variables.expand(text, used)
        if expanded is None:
            self.unknowable(number, "a target name built from variables it cannot expand")
            return None
        self.used |= used
        return expanded.split()


def read_make_targets(text: str) -> MakeTargets:
    lines = list(_logical_lines(text))
    return _Reader(lines).read(lines)


def make_targets_from_bytes(data: bytes) -> MakeTargets:
    """The targets of a makefile. make reads bytes, so a byte that is not
    UTF-8 (a Latin-1 comment) is kept as it is rather than failing the file."""
    return read_make_targets(data.decode("utf-8", "surrogateescape"))
