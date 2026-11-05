"""Query semantics for the docs check and `doc_refs`: pure functions
over a `doc_refs.DocReport`.

Nothing here reads a file, runs the analysis or formats text, as in `queries`.
`doc_refs` is never imported, because it needs markdown-it-py and the server
must start without it: a report reaches these functions already built, and its
types are read by attribute. Path, route and owner rules come from `doc_rules`,
the same ones the check resolves references with, so a lookup and the check
agree on what a mention names.

Keep this module importable on an older interpreter (no PEP 695/758 syntax), like
the rest of the server.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from code_intel import doc_rules
from code_intel.code_paths import is_under
from code_intel.queries import Page, page, problem_dicts

# ── Docs check ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DocCheck:
    """The docs check's verdict over a `doc_refs.DocReport`.

    ``findings`` are `doc_report.Finding` rows in the report's order (file,
    line, kind, text). ``reference_count`` counts every reference the docs
    name and ``checked_count`` the ones whose status decides the verdict
    (skipped, not-checked and Tier B references are not). ``limitations``
    are the report's own plus the DI route gaps the runtime passed in
    (`queries.BACKEND_ROUTE_GAP_KINDS`): a "route not found" finding may be a
    route the analysis cannot see."""

    findings: list[Any]
    doc_count: int
    reference_counts: dict[str, dict[str, int]]
    reference_count: int
    checked_count: int
    complete: bool
    diagnostics: list[dict]
    limitations: list[dict]

    @property
    def analysis(self) -> dict[str, Any]:
        return {"complete": self.complete, "diagnostics": self.diagnostics, "analysis_limitations": self.limitations}


def _report_problems(report: Any, extra: Iterable[Any] = ()) -> tuple[list[dict], list[dict]]:
    """(diagnostics, limitations) of ``report``, with ``extra`` problems sorted
    into the two by their ``category``. ``report.route_limitations`` holds only
    route gaps: the runtime filters the DI limitations before the report is
    built, so the check's route hint and this list agree."""
    added = problem_dicts(extra)
    diagnostics = [*problem_dicts(report.diagnostics), *(p for p in added if p.get("category"))]
    limitations = [
        *problem_dicts(report.limitations),
        *(p for p in added if not p.get("category")),
        *(dict(lim) for lim in report.route_limitations),
    ]
    return diagnostics, limitations


def doc_check(report: Any, extra_findings: Iterable[Any] = ()) -> DocCheck:
    """``report`` is a `doc_refs.DocReport`; ``extra_findings`` (the agent
    files') are merged into its findings in the same order."""
    diagnostics, limitations = _report_problems(report)
    references = list(report.references())
    findings = sorted([*report.findings(), *extra_findings], key=lambda f: (f.file, f.line, f.kind, f.text))
    return DocCheck(
        findings=findings,
        doc_count=len(report.docs),
        reference_counts=report.reference_counts(),
        reference_count=len(references),
        checked_count=sum(1 for ref in references if ref.checked),
        complete=not diagnostics,
        diagnostics=diagnostics,
        limitations=limitations,
    )


# ── Docs lookup ──────────────────────────────────────────────────────────

# A symbol query: an identifier or a dotted name (`$` for JavaScript names).
_DOC_SYMBOL_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
# Route statuses whose `defined_in` is the code the mention names: the path
# matched a live route (any method), an unmounted one, or a frontend page.
# Prefix, suffix and weak-parameter fits name no single route, so they would
# tie a doc to every handler under a path.
_ROUTE_DEFINED_STATUSES = frozenset({"ok", "method_differs", "unmounted", "frontend"})


def parse_doc_route(text: str) -> tuple[str | None, str] | None:
    """``"METHOD /path"`` or ``"/path"`` as the docs check reads a route in a
    doc, so a query and a mention normalize alike; None when it is neither."""
    return doc_rules.parse_route_text(text)


def is_doc_symbol(text: str) -> bool:
    return _DOC_SYMBOL_RE.fullmatch(text) is not None


def doc_lookup_needs_frontend(*, file: str | None, symbol: str | None, frontend_root: str) -> bool:
    """Whether the answer depends on frontend definition locations: a symbol
    query, or a file query under ``frontend_root`` (its symbols resolve to it
    only through those locations)."""
    return symbol is not None or (file is not None and is_under(file, frontend_root))


@dataclass(frozen=True)
class DocRefs:
    """`doc_refs` over a `doc_refs.DocReport`.

    ``kind`` is the query kind (file, route, symbol or doc) and ``value`` the
    query as normalized (``method`` only for routes). Lists a kind does not
    use are None: ``definitions`` for route and symbol queries, ``mentions``
    and ``doc_count`` for file, route and symbol queries, ``links``,
    ``references``, ``inbound``, ``checks``, ``in_scope`` and ``reachable``
    for doc queries (``reachable`` is also None for a doc outside the orphan
    check). ``ambiguous`` (symbol queries only) is true when the definitions
    span more than one owner or file, so the mentions may be of different
    symbols; ``owners`` then names each (``Owner (file)``, or the file of a
    module-level one)."""

    kind: str
    value: str
    method: str | None
    complete: bool
    diagnostics: list[dict]
    limitations: list[dict]
    definitions: Page[dict] | None = None
    mentions: Page[dict] | None = None
    doc_count: int | None = None
    links: Page[dict] | None = None
    references: Page[dict] | None = None
    inbound: Page[dict] | None = None
    reachable: bool | None = None
    reachability_root: str | None = None
    checks: list[str] | None = None
    in_scope: bool | None = None
    ambiguous: bool | None = None
    owners: list[str] | None = None

    @property
    def analysis(self) -> dict[str, Any]:
        return {"complete": self.complete, "diagnostics": self.diagnostics, "analysis_limitations": self.limitations}

    @property
    def query_text(self) -> str:
        return f"{self.method} {self.value}" if self.method else self.value


def _location_file(location: str) -> str:
    return location.rsplit(":", 1)[0]


def _ref_row(ref: Any, message: Any, ambiguous: bool | None = None) -> dict[str, Any]:
    return {
        "file": ref.doc,
        "line": ref.line,
        "kind": ref.kind,
        "text": ref.text,
        "target": ref.target,
        "status": ref.status,
        "detail": ref.detail,
        "message": message(ref) if ref.status in doc_rules.BROKEN_STATUSES else None,
        "checked": ref.checked,
        "defined_in": list(ref.defined_in),
        "ambiguous": ambiguous,
    }


def _ref_order(ref: Any) -> tuple:
    return (ref.doc, ref.line, ref.kind, ref.text)


def _path_variants(path: str, prefixes: Iterable[str]) -> list[tuple[str, ...]]:
    """``path`` as segments, as written and with a configured route prefix
    added or omitted, the variants the check tries for a mention."""
    return [doc_rules.mention_segments(variant) for variant in doc_rules.route_path_variants(path, prefixes)]


def _same_route_path(a: str, b: str, prefixes: Iterable[str]) -> bool:
    """Either path fills the other, parameter names ignored (``{id}`` and
    ``{appointment_id}`` agree; ``/appointments/42`` fits ``{id}``). A
    ``.../tail`` mention ``a`` agrees with a query that ends that way."""
    tail = doc_rules.route_tail(a)
    if tail is not None:
        segments = doc_rules.mention_segments(tail)
        return any(_same_route_end(segments, y) for y in _path_variants(b, prefixes))
    return any(
        doc_rules.match_segments(x, y) == "exact" or doc_rules.match_segments(y, x) == "exact"
        for x in _path_variants(a, prefixes)
        for y in _path_variants(b, prefixes)
    )


def _same_route_end(tail: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """The tail fills the end of ``path``, or that end fills the tail."""
    return any(
        doc_rules.match_segments(tail, path[start:]) == "exact"
        or doc_rules.match_segments(path[start:], tail) == "exact"
        for start in range(len(path))
    )


def _file_mention(ref: Any, path: str, nonlocal_defs: frozenset[tuple[str, str]]) -> tuple[bool, bool | None]:
    """(whether ``ref`` mentions the file or directory ``path``, ambiguous).
    A symbol counts only through a definition outside a function body: a
    local variable or parameter (``id``, ``value``) is not what a doc names."""
    if ref.kind in ("link", "path"):
        # An in-page anchor link targets its own doc; it does not mention it.
        own_anchor = ref.kind == "link" and ref.target == ref.doc
        return ref.target is not None and not own_anchor and is_under(ref.target, path), None
    if ref.kind == "route":
        if ref.status not in _ROUTE_DEFINED_STATUSES:
            return False, None
        return any(is_under(_location_file(loc), path) for loc in ref.defined_in), None
    name = (ref.target or "").rsplit(".", 1)[-1]
    files = [_location_file(loc) for loc in ref.defined_in if (loc, name) in nonlocal_defs]
    if not any(is_under(f, path) for f in files):
        return False, None
    return True, any(not is_under(f, path) for f in files)


def _symbol_matches(ref: Any, query: str, locations: frozenset[str]) -> bool:
    """A bare query matches the whole name or its first or last part
    (``Appointment`` finds ``Appointment.status``; ``status`` finds it too). A
    dotted query names one attribute of one owner, so it matches that text, a
    name that goes on from it (``AuditAction.X.value``), or a longer dotted
    name ending in it. With definitions, that longer name must resolve to one
    of them (``models.Appointment.status``), never another owner's ``status``;
    without any, owners can't be told apart, so every such name matches."""
    name = ref.target
    if not name:
        return False
    if "." not in query:
        return name == query or name.startswith(query + ".") or name.endswith("." + query)
    if name == query or name.startswith(query + "."):
        return True
    if not locations:
        return name.endswith("." + query)
    return (
        "." in name
        and name.rsplit(".", 1)[-1] == query.rsplit(".", 1)[-1]
        and any(loc in locations for loc in ref.defined_in)
    )


def _definition_owners(definitions: Iterable[Mapping[str, Any]]) -> list[str]:
    """Each distinct defining owner: ``Owner (file)``, or the file alone for a
    module-level definition."""
    owners = {(d["container"], d["file"]) for d in definitions}
    return [
        f"{container} ({file})" if container else file
        for container, file in sorted(owners, key=lambda o: (o[1], o[0] or ""))
    ]


def _symbol_definitions(
    symbols: Iterable[Any], query: str, bindings: Mapping[str, frozenset[str]] = doc_rules.NO_BINDINGS
) -> list[dict]:
    """Symbol-table rows for ``query``; a dotted ``Owner.attr`` needs a row
    for ``attr`` whose container and module fit the qualifiers as the check
    resolves them (`doc_rules.qualifiers_match`, with the report's package
    ``bindings``)."""
    parts = query.split(".")
    if len(parts) == 1:
        rows = [s for s in symbols if s.name == query]
    else:
        rows = [
            s
            for s in symbols
            if s.name == parts[-1]
            and doc_rules.qualifiers_match(
                s.container, s.file, parts[:-1], name=s.name, scope=s.scope, bindings=bindings
            )
        ]
    rows.sort(key=lambda s: (s.file, s.line, s.name))
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "container": s.container,
            "file": s.file,
            "line": s.line,
            "language": s.language,
            "scope": s.scope,
        }
        for s in rows
    ]


def _route_definitions(routes: Iterable[Any], method: str | None, path: str, prefixes: Iterable[str]) -> list[dict]:
    variants = _path_variants(path, prefixes)
    rows = [
        r
        for r in routes
        if doc_rules.methods_compatible(method, r.method)
        and any(doc_rules.match_segments(v, r.segments) == "exact" for v in variants)
    ]
    rows.sort(key=lambda r: (r.path, r.method, r.file, r.line))
    return [
        {"method": r.method, "path": r.path, "kind": r.kind, "handler": r.handler, "file": r.file, "line": r.line}
        for r in rows
    ]


def doc_refs_lookup(
    report: Any,
    *,
    file: str | None = None,
    route: str | None = None,
    symbol: str | None = None,
    doc: str | None = None,
    limit: int | None,
    frontend_problems: Iterable[Any] = (),
) -> DocRefs:
    """``report`` is a `doc_refs.DocReport`. Exactly one of ``file``
    (repository-relative, need not exist), ``route`` (``METHOD /path`` or
    ``/path``), ``symbol`` or ``doc`` is given; the tool validates them first.
    ``frontend_problems`` join the analysis of a lookup that needs frontend
    definitions."""
    # The report exists, so its module (and markdown-it-py) is already loaded.
    from code_intel.doc_report import reference_message

    given = [
        name
        for name, value in (("file", file), ("route", route), ("symbol", symbol), ("doc", doc))
        if value is not None
    ]
    if len(given) != 1:
        raise ValueError(f"give exactly one of file, route, symbol or doc, got {given or 'none'}")
    diagnostics, limitations = _report_problems(report, frontend_problems)
    analysis = {"complete": not diagnostics, "diagnostics": diagnostics, "limitations": limitations}

    if doc is not None:
        return _doc_lookup(report, doc, limit, reference_message, analysis)

    method: str | None = None
    definitions: list[dict] | None = None
    owners: list[str] | None = None
    rows: list[dict] = []
    refs = sorted(report.references(), key=_ref_order)
    if file is not None:
        value = file
        nonlocal_defs = frozenset((s.location, s.name) for s in report.symbols if s.scope != "local")
        for ref in refs:
            hit, ambiguous = _file_mention(ref, file, nonlocal_defs)
            if hit:
                rows.append(_ref_row(ref, reference_message, ambiguous))
    elif route is not None:
        parsed = parse_doc_route(route)
        if parsed is None:
            raise ValueError(f"route {route!r} is not 'METHOD /path' or '/path'")
        method, value = parsed
        prefixes = report.config.route_prefixes
        for ref in refs:
            if ref.kind != "route" or ref.route is None:
                continue
            ref_method, ref_path = ref.route
            if method is not None and ref_method is not None and ref_method != method:
                continue
            if _same_route_path(ref_path, value, prefixes):
                rows.append(_ref_row(ref, reference_message))
        definitions = _route_definitions(report.routes, method, value, prefixes)
    else:
        assert symbol is not None
        value = symbol
        definitions = _symbol_definitions(report.symbols, symbol, report.package_bindings)
        locations = frozenset(f"{d['file']}:{d['line']}" for d in definitions)
        rows = [
            _ref_row(ref, reference_message)
            for ref in refs
            if ref.kind == "symbol" and _symbol_matches(ref, symbol, locations)
        ]
        owners = _definition_owners(definitions)

    return DocRefs(
        kind=given[0],
        value=value,
        method=method,
        complete=analysis["complete"],
        diagnostics=diagnostics,
        limitations=limitations,
        definitions=None if definitions is None else page(definitions, limit),
        mentions=page(rows, limit),
        doc_count=len({row["file"] for row in rows}),
        ambiguous=None if owners is None else len(owners) > 1,
        owners=owners,
    )


def _doc_lookup(report: Any, doc: str, limit: int | None, message: Any, analysis: Mapping[str, Any]) -> DocRefs:
    entry = report.docs.get(doc)
    references = list(entry.references) if entry is not None else []
    links = [_ref_row(ref, message) for ref in references if ref.kind == "link"]
    code = [_ref_row(ref, message) for ref in references if ref.kind != "link"]
    inbound = [{"file": source, "line": line} for source, line in report.inbound.get(doc, ())]
    config = report.config
    return DocRefs(
        kind="doc",
        value=doc,
        method=None,
        complete=analysis["complete"],
        diagnostics=analysis["diagnostics"],
        limitations=analysis["limitations"],
        links=page(links, limit),
        references=page(code, limit),
        inbound=page(inbound, limit),
        reachable=(doc in report.reachable) if config.in_orphan_scope(doc) else None,
        reachability_root=None if config.reachability is None else config.reachability.root,
        checks=list(entry.checks) if entry is not None else None,
        in_scope=entry is not None,
    )
