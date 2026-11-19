"""The targets a Makefile defines, for the docs check's make references.

The reader takes target names from rule lines, from every branch of every
conditional, since a union cannot make a defined target look missing. Anything
that can define a target the reader cannot see (an include, a pattern or
suffix rule, ``.DEFAULT``, ``define`` or ``$(eval)``, ``vpath``, a target
name it cannot expand) makes the set incomplete: ``MakeTargets.opaque`` says
why, and a target absent from it is then not proven missing.

A target name built from variables is expanded when every variable it uses was
set exactly once, outside a conditional and before the rule, from literal text
and such variables. The environment does not count (the docs check assumes a
clean one); a command line that sets one of those variables is the caller's
concern, through ``MakeTargets.variables``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from stack_intel.doc_rules import STAGE
from stack_intel.problems import Problem, diagnostic

# GNU make's lookup order when no -f is given.
MAKEFILE_NAMES: tuple[str, ...] = ("GNUmakefile", "makefile", "Makefile")

_SPECIAL_TARGETS = frozenset(
    """.PHONY .SUFFIXES .PRECIOUS .INTERMEDIATE .NOTINTERMEDIATE .SECONDARY .SECONDEXPANSION
    .DELETE_ON_ERROR .IGNORE .LOW_RESOLUTION_TIME .SILENT .EXPORT_ALL_VARIABLES .NOTPARALLEL .ONESHELL
    .POSIX .WAIT""".split()
)
_SUFFIX_RULE_RE = re.compile(r"(?:\.[^.\s/%]+){1,2}")
_CONDITIONALS = frozenset({"ifeq", "ifneq", "ifdef", "ifndef"})
_INCLUDES = frozenset({"include", "-include", "sinclude"})
_MODIFIERS = frozenset({"export", "override", "private", "unexport"})
_REFERENCE_RE = re.compile(r"\$(?:\(([A-Za-z0-9_.-]+)\)|\{([A-Za-z0-9_.-]+)\})")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_DEPTH = 20


@dataclass(frozen=True)
class MakeTargets:
    """``targets``: every name a rule line (or a special target's
    prerequisite list) defines. ``opaque``: why the set may be incomplete,
    None when it is complete. ``variables``: the variables the expanded target
    names were built from."""

    targets: frozenset[str]
    opaque: str | None = None
    variables: frozenset[str] = frozenset()


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


def _split_rule(line: str) -> tuple[str, str, int] | None:
    """``("assign", name+op, value_start)`` or ``("rule", targets, rest_start)``
    by the first ``:`` or assignment operator outside ``$(...)``, None for neither."""
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
                for op in (":=", "::=", ":::="):
                    if line.startswith(op, index):
                        return "assign", line[:index], index + len(op)
                return "rule", line[:index], index + (2 if line.startswith("::", index) else 1)
            if char == "=":
                return "assign", line[:index], index + 1
            if char in "?+!" and line[index + 1 : index + 2] == "=":
                return "assign", line[: index + 1], index + 2
        index += 1
    return None


class _Variables:
    """The variables known so far, for expanding target names: a name maps to
    its text (recursive ones expand on use), or None when its value is not
    known (set twice, appended to, set in a conditional or by a shell)."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, bool] | None] = {}

    def assign(self, name: str, op: str, value: str, conditional: bool) -> None:
        if op == "?=" and name in self.values:
            return
        if conditional or op in ("+=", "!=") or name in self.values:
            self.values[name] = None
        elif op == "=" or op == "?=":
            self.values[name] = (value, True)
        else:
            expanded = self.expand(value, set())
            self.values[name] = (expanded, False) if expanded is not None else None

    def expand(self, text: str, used: set[str], depth: int = 0) -> str | None:
        """``text`` with every variable reference replaced, None when one is not known."""
        if depth > _MAX_DEPTH:
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
            out.append(text[position : match.start()])
            out.append(value)
            position = match.end()
        out.append(text[position:])
        result = "".join(out)
        return None if "$" in result else result


class _Reader:
    def __init__(self) -> None:
        self.targets: set[str] = set()
        self.used: set[str] = set()
        self.opaque: str | None = None
        self.variables = _Variables()
        self.depth = 0
        self.in_define = False

    def unknowable(self, line: int, reason: str) -> None:
        if self.opaque is None:
            self.opaque = f"has {reason} (line {line})"

    def read(self, text: str) -> MakeTargets:
        for number, raw in _logical_lines(text):
            self.line(number, raw)
        return MakeTargets(frozenset(self.targets), self.opaque, frozenset(self.used))

    def line(self, number: int, raw: str) -> None:
        if self.in_define:
            if raw.strip().split()[:1] == ["endef"]:
                self.in_define = False
            return
        if raw.startswith("\t"):
            return
        line = _strip_comment(raw).strip()
        words = line.split()
        if not words:
            return
        while words and words[0] in _MODIFIERS:
            words = words[1:]
        first = words[0] if words else ""
        if first in _CONDITIONALS:
            self.depth += 1
            return
        if first == "else":
            return
        if first == "endif":
            self.depth = max(0, self.depth - 1)
            return
        if first in _INCLUDES:
            self.unknowable(number, first)
            return
        if first == "define":
            self.in_define = True
            self.unknowable(number, "define")
            return
        if first in ("vpath", "load", "-load"):
            self.unknowable(number, first)
            return
        if "$(eval" in line or "${eval" in line:
            self.unknowable(number, "$(eval)")
        split = _split_rule(" ".join(words))
        if split is None:
            return
        kind, head, rest = split
        body = " ".join(words)[rest:]
        if kind == "assign":
            self.assign(number, head, body)
        else:
            self.rule(number, head, body)

    def assign(self, number: int, head: str, value: str) -> None:
        op = "="
        if head.endswith(("?", "+", "!")):
            op = head[-1] + "="
            head = head[:-1]
        elif head.endswith(":"):
            op = ":="
        name = head.strip()
        if _VARIABLE_NAME_RE.fullmatch(name) is None:
            self.unknowable(number, "a variable whose name is computed")
            return
        if name == "VPATH":
            self.unknowable(number, "VPATH")
        self.variables.assign(name, op, value.strip(), self.depth > 0)

    def rule(self, number: int, head: str, rest: str) -> None:
        names = self.expand(number, head)
        prerequisites = rest.split(";", 1)[0]
        if names is None:
            return
        if (split := _split_rule(prerequisites)) is not None and split[0] == "rule" and "%" in prerequisites:
            self.unknowable(number, "a static pattern rule")
        for name in names:
            if "%" in name:
                self.unknowable(number, "a pattern rule")
            elif name == ".DEFAULT":
                self.unknowable(number, "a .DEFAULT rule")
            elif name in _SPECIAL_TARGETS:
                self.targets.add(name)
                listed = self.expand(number, prerequisites.replace("|", " ")) if "=" not in prerequisites else []
                self.targets.update(listed or ())
            elif _SUFFIX_RULE_RE.fullmatch(name):
                self.unknowable(number, f"an old-style suffix rule {name}")
            else:
                self.targets.add(name)

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
    return _Reader().read(text)


def make_targets_from_bytes(rel: str, data: bytes, *, stage: str = STAGE) -> tuple[MakeTargets | None, list[Problem]]:
    """The targets of the Makefile ``rel``, or None with a diagnostic when it is not UTF-8."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [diagnostic("decode_failed", stage=stage, step="decode file", file=rel, exc=exc)]
    return read_make_targets(text), []
