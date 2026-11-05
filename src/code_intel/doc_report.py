"""Results of the docs check: references, per-doc entries, the report and
its findings, plus the input rows (DI routes, symbol-table entries) the
resolver works on.

Kept apart from ``doc_refs`` so the resolver stays readable; ``doc_refs``
re-exports everything a caller needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from code_intel.doc_rules import BROKEN_STATUSES, NO_BINDINGS, DocRefsConfig, backend_route_segments
from code_intel.markdown_facts import Heading
from code_intel.problems import Problem, partition
from code_intel.result_types import library_text

# ── Results ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reference:
    """One thing a doc names.

    ``kind``: link, path, route or symbol. ``text``: as written (an ignore
    entry matches it exactly). ``context``: the block it sits in (``prose``,
    ``heading``, ``table``, ``html``, or ``fence`` for a line of a code block;
    a code span carries its block's context). ``target``: the repository path, live route
    (``METHOD /path``) or name it resolved to, or for a missing path the path
    it was looked for at, so a lookup still finds docs naming a deleted file.
    ``checked``: the check's verdict depends on it (False for skipped,
    not-checked and Tier B references). ``defined_in``: ``file:line`` of the
    definitions it resolved to: owner-matched symbol-index entries for
    symbols, handlers for routes, the page file for frontend routes.
    ``route``: ``(canonical method or None, path as written)`` for routes.
    """

    doc: str
    line: int
    kind: str
    text: str
    context: str
    target: str | None
    status: str
    detail: str | None
    checked: bool
    defined_in: tuple[str, ...] = ()
    route: tuple[str | None, str] | None = None

    @property
    def broken(self) -> bool:
        return self.status in BROKEN_STATUSES


@dataclass(frozen=True)
class DocEntry:
    """One scope doc: the checks its rule allows, its headings and references."""

    doc: str
    checks: tuple[str, ...]
    rule_reason: str | None
    headings: tuple[Heading, ...]
    references: tuple[Reference, ...]

    @property
    def outbound(self) -> tuple[Reference, ...]:
        """The doc's links (external ones included, as ``skipped``)."""
        return tuple(ref for ref in self.references if ref.kind == "link")


@dataclass(frozen=True)
class ConfigFinding:
    file: str
    line: int
    kind: str
    text: str
    message: str


class Finding(NamedTuple):
    file: str
    line: int
    kind: str
    status: str
    text: str
    message: str


@dataclass(frozen=True)
class RouteRow:
    method: str
    path: str
    kind: str
    handler: str
    file: str
    line: int
    segments: tuple[str, ...] = ()
    library: str = ""  # where installed code defines the handler; ``file``/``line`` are then the include that mounts it

    @property
    def display(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class SymbolRow:
    name: str
    kind: str
    container: str | None
    file: str
    line: int
    language: str
    scope: str = "module"

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass(frozen=True)
class DocReport:
    """Everything the check and the lookup tool need.

    ``inbound`` maps a Markdown file to the ``(doc, line)`` of scope-doc links
    reaching it (a directory link reaches its README/index). ``reachable``:
    every Markdown file reached from the reachability root. ``problems`` holds
    diagnostics (``complete`` turns False) and limitations apart.
    """

    docs: Mapping[str, DocEntry]
    inbound: Mapping[str, tuple[tuple[str, int], ...]]
    orphans: tuple[str, ...]
    reachable: frozenset[str]
    config_findings: tuple[ConfigFinding, ...]
    routes: tuple[RouteRow, ...]
    route_limitations: tuple[Mapping[str, Any], ...]
    symbols: tuple[SymbolRow, ...]
    problems: tuple[Problem, ...]
    config: DocRefsConfig
    # Package directory → the names its `__init__.py` binds, as the check
    # used them, so the lookup qualifies dotted names the same way.
    package_bindings: Mapping[str, frozenset[str]] = NO_BINDINGS

    @property
    def diagnostics(self) -> list[Problem]:
        return partition(self.problems)[0]

    @property
    def limitations(self) -> list[Problem]:
        return partition(self.problems)[1]

    @property
    def complete(self) -> bool:
        return not self.diagnostics

    def references(self) -> Iterator[Reference]:
        for entry in self.docs.values():
            yield from entry.references

    def reference_counts(self) -> dict[str, dict[str, int]]:
        """``{kind: {status: count}}``."""
        counts: dict[str, dict[str, int]] = {}
        for ref in self.references():
            by_status = counts.setdefault(ref.kind, {})
            by_status[ref.status] = by_status.get(ref.status, 0) + 1
        return counts

    def findings(self) -> list[Finding]:
        """Every failure as ``(file, line, kind, status, text, message)``,
        sorted by file, line, kind and text."""
        out = [
            Finding(ref.doc, ref.line, ref.kind, ref.status, ref.text, reference_message(ref))
            for ref in self.references()
            if ref.broken
        ]
        if self.config.reachability is not None:
            root = self.config.reachability.root
            out.extend(
                Finding(doc, 1, "orphan", "orphaned", doc, f"orphaned doc: not reachable from {root}")
                for doc in self.orphans
            )
        out.extend(Finding(f.file, f.line, f.kind, "unused", f.text, f.message) for f in self.config_findings)
        return sorted(out, key=lambda f: (f.file, f.line, f.kind, f.text))


_MESSAGE_HEADS: Mapping[tuple[str, str], str] = {
    ("link", "missing"): "broken link",
    ("link", "anchor_missing"): "broken anchor",
    ("path", "missing"): "stale path",
    ("route", "missing"): "route not found",
    ("route", "weak_param"): "route not found",
    ("route", "ambiguous"): "route not specific",
    ("route", "method_differs"): "route method differs",
    ("route", "unmounted"): "route not mounted",
    ("symbol", "missing"): "symbol not found",
}


def reference_message(ref: Reference) -> str:
    """``broken link: ./x.md (docs/x.md does not exist)`` and the like."""
    head = _MESSAGE_HEADS.get((ref.kind, ref.status), f"{ref.kind} {ref.status}")
    return f"{head}: {ref.text} ({ref.detail})" if ref.detail else f"{head}: {ref.text}"


# ── Input rows ───────────────────────────────────────────────────────────


def route_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[RouteRow, ...]:
    """DI route rows (``build_di`` ``routes``/``unmounted_routes``) → RouteRow."""
    return tuple(
        RouteRow(
            method=str(row["method"]),
            path=str(row["path"]),
            kind=str(row.get("kind", "")),
            handler=str(row.get("handler", "")),
            file=str(row.get("file", "")),
            line=int(row.get("line") or 0),
            segments=backend_route_segments(str(row["path"])),
            library=library_text(row["library"]) if row.get("library") else "",
        )
        for row in rows
    )


def symbol_rows(symbols: Iterable[Any]) -> tuple[SymbolRow, ...]:
    """Accepts ``{name, kind, container, file, line, language}`` mappings and
    the ``(rel, PySymbol)`` / ``(rel, TsDecl)`` pairs that
    ``SymbolIndex.python_symbols`` and ``typescript_decls`` return."""
    out: list[SymbolRow] = []
    for item in symbols:
        if isinstance(item, Mapping):
            out.append(
                SymbolRow(
                    name=str(item["name"]),
                    kind=str(item.get("kind", "")),
                    container=item.get("container"),
                    file=str(item["file"]),
                    line=int(item.get("line") or 0),
                    language=str(item.get("language", "python" if str(item["file"]).endswith(".py") else "typescript")),
                    scope=str(item.get("scope", "module")),
                )
            )
            continue
        rel, symbol = item
        out.append(
            SymbolRow(
                name=symbol.name,
                kind=symbol.kind,
                container=symbol.container,
                file=rel,
                line=symbol.line,
                language="python" if rel.endswith(".py") else "typescript",
                scope=getattr(symbol, "scope", "module"),
            )
        )
    return tuple(out)
