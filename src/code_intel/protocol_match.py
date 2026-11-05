#!/usr/bin/env python3
"""Which Protocols a concrete class matches, by member names.

Python language servers resolve a call on a variable typed as a
``typing.Protocol`` to the Protocol's method, not to the implementing class's
method, so a references or callers query on the concrete method misses the
production calls. ``symbol_resolve`` uses this module to name the
Protocol methods to query instead.

The match is structural but by names only, as runtime ``Protocol`` checks are:
no signature, annotation or return type is compared. Pure functions over
``ClassRec`` rows built from the symbol index (``symbol_resolve``); no parsing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MemberRec:
    line: int
    kind: str


@dataclass(frozen=True)
class BaseRef:
    """A base class as written, with what the file's own names and imports say it is.

    ``id`` is the ``module:qualname`` it names (None when the file's names do not
    say). ``module`` is the module it was imported from, ``external`` marks a name no
    import or definition in the file explains (a builtin such as ``Exception``).
    """

    name: str
    id: str | None = None
    module: str | None = None
    external: bool = False


@dataclass(frozen=True)
class ClassRec:
    """One class as the symbol index sees it.

    ``members`` are the names its body defines (methods, annotated or assigned
    class attributes, nested classes) plus attributes assigned on ``self`` in
    ``__init__`` and ``__post_init__``. ``local`` marks a class defined inside a
    function: its name cannot be reached from other code, so it is never found as a base.
    """

    name: str
    qualname: str
    file: str
    module: str
    line: int
    end_line: int
    bases: tuple[BaseRef, ...]
    protocol: bool
    local: bool = False
    members: dict[str, MemberRec] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.module}:{self.qualname}"


@dataclass(frozen=True)
class ProtocolMatch:
    """A Protocol a class matches; ``line`` is the Protocol's same-name method."""

    name: str
    id: str
    file: str
    line: int


# What every class gets from ``object``: a Protocol declaring one of these (``__init__``,
# ``__eq__``) asks nothing of the class. Any other dunder (``__enter__``, ``__iter__``) is
# required, as ``runtime_checkable`` requires it.
_OBJECT_MEMBERS = frozenset(dir(object))


class ClassTable:
    """Every class of the index, for base lookup and Protocol lookup by method name."""

    def __init__(self, records: Iterable[ClassRec]) -> None:
        self.records = list(records)
        self._by_name: dict[str, list[ClassRec]] = {}
        self._by_id: dict[str, ClassRec] = {}
        self._by_file: dict[str, list[ClassRec]] = {}
        self._roots = {rec.module.split(".")[0] for rec in self.records}
        self.protocols_by_method: dict[str, list[ClassRec]] = {}
        for rec in self.records:
            if not rec.local:
                self._by_name.setdefault(rec.name, []).append(rec)
                self._by_id.setdefault(rec.id, rec)
            self._by_file.setdefault(rec.file, []).append(rec)
            if rec.protocol:
                for name, member in rec.members.items():
                    if member.kind == "method":
                        self.protocols_by_method.setdefault(name, []).append(rec)

    def owner_of(self, file: str, line: int) -> ClassRec | None:
        """The innermost class whose body holds ``line``. A container name cannot tell
        two classes of one name apart (a function-local class, or the two arms of an
        ``if TYPE_CHECKING``), their line ranges can."""
        inside = [rec for rec in self._by_file.get(file, ()) if rec.line < line <= rec.end_line]
        return max(inside, key=lambda rec: rec.line, default=None)

    def _base(self, ref: BaseRef) -> ClassRec | None:
        """The class a base refers to: by the id its file's names give it; a base from outside
        the repository has no in-repo class; one the file's names cannot place (a package's
        re-export) is the one class of that name, when there is exactly one."""
        if ref.id is not None and (found := self._by_id.get(ref.id)) is not None:
            return found
        if ref.external or (ref.module is not None and ref.module.split(".")[0] not in self._roots):
            return None
        candidates = self._by_name.get(ref.name.rsplit(".", 1)[-1], ())
        return candidates[0] if len(candidates) == 1 else None

    def lineage(self, rec: ClassRec) -> list[ClassRec]:
        """``rec`` and the in-repo classes it inherits from, nearest first."""
        out: list[ClassRec] = []
        seen: set[tuple[str, int]] = set()
        pending = [rec]
        while pending:
            current = pending.pop(0)
            key = (current.file, current.line)
            if key in seen:
                continue
            seen.add(key)
            out.append(current)
            pending.extend(base for ref in current.bases if (base := self._base(ref)) is not None)
        return out

    def member_names(self, rec: ClassRec) -> set[str]:
        return {name for cls in self.lineage(rec) for name in cls.members}

    def matching_protocols(self, rec: ClassRec, method: str) -> list[ProtocolMatch]:
        """Protocols that declare ``method`` and that ``rec`` names as a base or defines every member of.

        Naming one means a base whose imports or definitions in its own file lead to the
        Protocol's id; a base that only shares the Protocol's short name does not.
        """
        if rec.protocol:
            return []
        lineage = self.lineage(rec)
        base_ids = {ref.id for cls in lineage for ref in cls.bases if ref.id is not None}
        have = {name for cls in lineage for name in cls.members}
        out = []
        for protocol in self.protocols_by_method.get(method, ()):
            required = {name for name in self.member_names(protocol) if name not in _OBJECT_MEMBERS}
            if protocol.id in base_ids or required <= have:
                out.append(ProtocolMatch(protocol.name, protocol.id, protocol.file, protocol.members[method].line))
        return sorted(out, key=lambda m: (m.file, m.line, m.id))
