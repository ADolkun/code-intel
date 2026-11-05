"""Response rendering for the docs check (``doc_check.py --json``) and
`doc_refs`, from the typed results in `doc_queries`.

The envelope, `Body` and the shared fragments come from `presenters`, so a
docs reply has the same banners and JSON shape as every other tool's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from code_intel import doc_queries, problems
from code_intel.presenters import Body, incomplete_banner, omitted_note
from code_intel.queries import Page

# ── Docs check ───────────────────────────────────────────────────────────


def _finding_line(finding: Any) -> str:
    return f"{finding.file}:{finding.line}: {finding.message}"


def doc_check_lines(result: doc_queries.DocCheck) -> tuple[list[str], list[str]]:
    """(finding lines, note lines). A finding line is ``path:line: message``;
    the notes are the incomplete banner with every diagnostic, then the
    limitations banner with every limitation. The CLI prints the two apart,
    so its standard output holds only findings."""
    findings = [_finding_line(f) for f in result.findings]
    notes: list[str] = []
    if not result.complete:
        notes.append(incomplete_banner(result.diagnostics))
        for p in result.diagnostics:
            loc = p.get("file") or "?"
            if p.get("line"):
                loc = f"{loc}:{p['line']}"
            fix = f" Fix: {p['fix']}" if p.get("fix") else ""
            notes.append(f"  [{p.get('kind')}] {loc}: {p.get('message')}{fix}")
    if result.limitations:
        notes.append(problems.limitations_banner(result.limitations))
    return findings, notes


def doc_check(result: doc_queries.DocCheck) -> Body:
    """The ``--json`` reply. Its text is only the finding lines: `envelope`
    adds the banners, and the CLI's text output comes from `doc_check_lines`."""
    payload = {
        "findings": [
            {"file": f.file, "line": f.line, "kind": f.kind, "status": f.status, "text": f.text, "message": f.message}
            for f in result.findings
        ],
        "finding_count": len(result.findings),
        "doc_count": result.doc_count,
        "reference_counts": result.reference_counts,
    }
    return Body("\n".join(_finding_line(f) for f in result.findings), payload)


# ── Docs lookup ──────────────────────────────────────────────────────────


def _doc_ref_line(row: Mapping[str, Any]) -> str:
    """``  docs/a.md:12  route  GET /x  ok``; a status other than ok carries its detail."""
    detail = f" ({row['detail']})" if row["detail"] and row["status"] != "ok" else ""
    ambiguous = "  [also defined outside it]" if row.get("ambiguous") else ""
    return f"  {row['file']}:{row['line']}  {row['kind']}  {row['text']}  {row['status']}{detail}{ambiguous}"


def _page_fields(name: str, rows: Page[Any] | None, *, plural: str | None = None) -> dict[str, Any]:
    key = plural or f"{name}s"
    if rows is None:
        return {key: None, f"{name}_count": None, f"{name}_omitted": None}
    return {key: rows.items, f"{name}_count": rows.total, f"{name}_omitted": rows.omitted}


def _rows_block(title: str, rows: Page[Any], noun: str, render: Any) -> list[str]:
    lines = [f"{title} ({rows.total}):"]
    lines.extend(render(row) for row in rows.items)
    if rows.omitted:
        lines.append(omitted_note(len(rows.items), rows.total, noun, "  "))
    return lines


def _definition_line(result: doc_queries.DocRefs, row: Mapping[str, Any]) -> str:
    if result.kind == "route":
        return f"Live route: {row['method']} {row['path']}  {row['handler']}()  [{row['file']}:{row['line']}]"
    container = f" in {row['container']}" if row.get("container") else ""
    return f"  {row['kind']:<10} {row['file']}:{row['line']}{container}"


def _doc_refs_definitions(result: doc_queries.DocRefs) -> list[str]:
    rows = result.definitions
    if rows is None:
        return []
    if not rows.total:
        if result.kind == "route":
            return [f"No live route matches {result.query_text}.", ""]
        return [f"No definition of {result.value} in the symbol index.", ""]
    if result.kind == "route":
        lines = [_definition_line(result, row) for row in rows.items]
        if rows.omitted:
            lines.append(omitted_note(len(rows.items), rows.total, "routes"))
        return [*lines, ""]
    block = _rows_block(f"Definitions of {result.value}", rows, "definitions", lambda r: _definition_line(result, r))
    return [*block, ""]


# How many defining owners the ambiguity note names before it counts the rest.
_OWNERS_SHOWN = 5


def _ambiguity_note(result: doc_queries.DocRefs) -> list[str]:
    """One line naming the owners a symbol query's definitions span, since
    its mentions may then be of different symbols."""
    if not result.ambiguous or not result.owners:
        return []
    shown = ", ".join(result.owners[:_OWNERS_SHOWN])
    rest = len(result.owners) - _OWNERS_SHOWN
    more = f" and {rest} more" if rest > 0 else ""
    return [
        f"Ambiguous: {result.value} is defined in {len(result.owners)} places ({shown}{more}), "
        "so these mentions may name different symbols.",
        "",
    ]


def _doc_refs_mentions(result: doc_queries.DocRefs) -> list[str]:
    rows = result.mentions
    assert rows is not None
    if not rows.total:
        return [f"No doc mentions {result.query_text}."]
    lines = [f"{rows.total} mention(s) of {result.query_text} in {result.doc_count} doc(s):"]
    lines.extend(_doc_ref_line(row) for row in rows.items)
    if rows.omitted:
        lines.append(omitted_note(len(rows.items), rows.total, "mentions", "  "))
    return lines


def _doc_refs_doc(result: doc_queries.DocRefs) -> list[str]:
    assert result.links is not None and result.references is not None and result.inbound is not None
    if result.in_scope:
        lines = [f"{result.value}: checks {', '.join(result.checks or []) or '(none)'}"]
    else:
        lines = [f"{result.value} is not a doc the check reads (outside its scope, or not in the repository)."]
    if result.reachable is None:
        lines.append("Reachability: outside the orphan check.")
    else:
        word = "reachable" if result.reachable else "NOT reachable"
        lines.append(f"Reachability: {word} from {result.reachability_root}.")
    lines.append("")
    lines += _rows_block("Links", result.links, "links", _doc_ref_line)
    lines.append("")
    lines += _rows_block("References", result.references, "references", _doc_ref_line)
    lines.append("")
    lines += _rows_block("Linked from", result.inbound, "links", lambda r: f"  {r['file']}:{r['line']}")
    return lines


def doc_refs(result: doc_queries.DocRefs) -> Body:
    """Every reply has the same keys whatever the query kind; a list the kind
    does not use is null, with its count and omitted."""
    if result.kind == "doc":
        lines = _doc_refs_doc(result)
    else:
        lines = [*_doc_refs_definitions(result), *_ambiguity_note(result), *_doc_refs_mentions(result)]
    payload: dict[str, Any] = {
        "query": {"kind": result.kind, "value": result.value, "method": result.method},
        **_page_fields("definition", result.definitions),
        **_page_fields("mention", result.mentions),
        "doc_count": result.doc_count,
        **_page_fields("link", result.links),
        **_page_fields("reference", result.references),
        **_page_fields("inbound", result.inbound, plural="inbound"),
        "reachable": result.reachable,
        "checks": result.checks,
        "in_scope": result.in_scope,
        "ambiguous": result.ambiguous,
        "owners": result.owners,
    }
    pages = (result.definitions, result.mentions, result.links, result.references, result.inbound)
    payload["omitted"] = sum(p.omitted for p in pages if p is not None)
    # Most of a lookup's limitations are the docs check's own, which
    # status does not list.
    return Body("\n".join(lines), payload, limitations_in_status=False)
