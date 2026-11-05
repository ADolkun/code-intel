"""Response rendering for the MCP tools.

Every reply goes through `envelope`, the one place that stamps the schema
version, completeness, diagnostics, analysis limitations and the stale-code
warning onto a result and emits it as text or JSON. Errors are bodies too
(`error_body`), so they get the same banner and JSON shape. The per-tool
functions build the text and the JSON payload side by side from one typed
query result, so the two formats cannot drift apart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from code_intel import problems, queries
from code_intel.result_types import ERROR_FIELDS, SCHEMA_VERSION, ErrorCode, library_text, route_where

STALE_CODE_WARNING = (
    "⚠ code-intel changed since this server started (its code, or the version pinned in "
    ".code-intel/requirements.txt); restart the code-intel MCP server in your client to load it. "
    "These results come from the old code."
)

# Leads a reply when a loaded module file was modified so close to its load
# that the loaded bytes are unknown: neither stale nor current can be claimed.
UNCONFIRMED_CODE_WARNING = (
    "⚠ Cannot confirm that the analyzer code this server loaded matches the files on disk; restart the "
    "code-intel MCP server in your client to be sure."
)

OUTPUT_FORMATS = ("text", "json")


@dataclass(frozen=True)
class Body:
    """One tool result, rendered both ways before the envelope is applied.

    ``is_error`` bodies carry no completeness: nothing was analyzed.
    ``limitations_in_status`` is False for a result whose limitations
    `status` does not list (the docs check's own), so the
    limitations line points only at the JSON.
    """

    text: str
    payload: dict[str, Any]
    is_error: bool = False
    limitations_in_status: bool = True


# ── Errors ───────────────────────────────────────────────────────────────


def error_body(code: ErrorCode, message: str, **fields: Any) -> Body:
    """The one error shape: text is the message, JSON is
    ``{"schema_version": 2, "error": code, "message": message, **fields}``.

    ``fields`` must be the ones ``result_types.ERROR_FIELDS`` lists for ``code``,
    so every producer of an error code emits the same keys.
    """
    if code not in ERROR_FIELDS:
        raise ValueError(f"error code {code!r} is not in result_types.ERROR_FIELDS")
    spec = ERROR_FIELDS[code]
    allowed = {name.rstrip("?") for name in spec}
    required = {name for name in spec if not name.endswith("?")}
    unknown = set(fields) - allowed
    missing = required - set(fields)
    if unknown or missing:
        raise ValueError(f"error body {code!r}: unexpected fields {sorted(unknown)}, missing fields {sorted(missing)}")
    payload = {"schema_version": SCHEMA_VERSION, "error": code, "message": message, **fields}
    return Body(message, payload, is_error=True)


def invalid_argument(argument: str, value: Any, allowed: Sequence[str], reason: str) -> Body:
    shown = ", ".join(repr(a) for a in allowed)
    text = f"Invalid {argument} {value!r}: {reason}."
    if allowed:
        text = f"{text} Allowed: {shown}."
    # `allowed` is present only for an argument with a fixed set of values.
    extra = {"allowed": list(allowed)} if allowed else {}
    return error_body("invalid_argument", text, argument=argument, value=value, **extra)


def unknown_arguments(tool: str, unknown: Sequence[str], value: Any, allowed: Sequence[str]) -> Body:
    """An argument name the tool does not declare; ``argument`` and ``value``
    are the first unknown one."""
    names = ", ".join(repr(name) for name in unknown)
    noun = "argument" if len(unknown) == 1 else "arguments"
    text = f"Unknown {noun} {names} for {tool}. Allowed: {', '.join(repr(a) for a in allowed)}."
    return error_body("invalid_argument", text, argument=unknown[0], value=value, allowed=list(allowed))


def runtime_refusal(running: str, required: str) -> Body:
    message = (
        f"Python {running} is older than the backend's {required}; DI, routes and backend symbols "
        f"are refused, because the backend's PEP 758 `except A, B:` clauses do not parse on it and "
        f"the results would silently miss those files. Restart the code-intel MCP server with Python "
        f">={required}. JSX usage, frontend symbols and status keep working."
    )
    return error_body("runtime_incompatible", message, running=running, required=required)


def frontend_unavailable(message: str, missing: str | None, fix: str, problem: str) -> Body:
    """``problem`` is the cause code: a toolchain probe code, ``helper_failed`` or
    ``frontend_not_found``. A message that already ends in the fix is not given it twice."""
    text = f"Frontend analysis unavailable: {problems.strip_fix(message, fix)}. Fix: {fix}."
    return error_body("frontend_unavailable", text, missing=missing, fix=fix, problem=problem)


# ── Envelope ─────────────────────────────────────────────────────────────


def json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def limitations_note(limitations: Sequence[Mapping], *, in_status: bool = True) -> str:
    """One leading line; the full list is in the JSON, and in `status`
    when ``in_status``."""
    kinds = ", ".join(sorted({str(lim.get("kind") or "?") for lim in limitations}))
    where = "status or output_format='json'" if in_status else "output_format='json'"
    return f"ⓘ {len(limitations)} analysis limitation(s) ({kinds}); details: {where}"


def envelope(
    body: Body,
    output_format: str,
    *,
    stale: bool,
    unconfirmed: Sequence[Mapping[str, str]] = (),
    analysis: Mapping[str, Any] | None = None,
    banners: bool = True,
) -> str:
    """Format a tool response.

    Success JSON always carries ``schema_version``, ``complete``, ``diagnostics``,
    ``analysis_limitations``, ``analyzer_code_stale`` and
    ``analyzer_code_unconfirmed``. ``analysis`` supplies the first three
    (``complete`` may be None when nothing was checked); without it the body is
    complete and has no problems. ``stale`` is a confirmed change to the loaded
    code; ``unconfirmed`` lists ``{"file", "reason"}`` for loaded files that
    cannot be compared, and without a confirmed change makes
    ``analyzer_code_stale`` null. Text leads with the stale or the unconfirmed
    warning, then the step-grouped incomplete banner, then the limitations line.
    ``banners=False`` is for bodies that render their own (``status``).

    Error bodies get the code flags and warning only.
    """
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"output_format {output_format!r} is not one of {OUTPUT_FORMATS}")
    text = body.text
    payload = dict(body.payload)
    if not body.is_error:
        analysis = analysis or {}
        complete = analysis.get("complete", True)
        diagnostics = queries.problem_dicts(analysis.get("diagnostics") or [])
        limitations = queries.problem_dicts(analysis.get("analysis_limitations") or [])
        payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "complete": complete,
                "diagnostics": diagnostics,
                "analysis_limitations": limitations,
            }
        )
        if banners:
            if limitations:
                text = f"{limitations_note(limitations, in_status=body.limitations_in_status)}\n\n{text}"
            if complete is False:
                text = f"{incomplete_banner(diagnostics)}\n\n{text}"
    payload["analyzer_code_stale"] = True if stale else (None if unconfirmed else False)
    payload["analyzer_code_unconfirmed"] = [dict(item) for item in unconfirmed]
    if stale:
        text = f"{STALE_CODE_WARNING}\n\n{text}"
    elif unconfirmed:
        text = f"{unconfirmed_code_warning(unconfirmed)}\n\n{text}"
    if output_format == "text":
        return text
    return json_text(payload)


def unconfirmed_code_warning(unconfirmed: Sequence[Mapping[str, str]]) -> str:
    """`UNCONFIRMED_CODE_WARNING` followed by each file and why it cannot be compared."""
    files = "; ".join(f"{item['file']} ({item['reason']})" for item in unconfirmed)
    return f"{UNCONFIRMED_CODE_WARNING} Unconfirmed: {files}."


# ── Shared fragments ─────────────────────────────────────────────────────


_FRONTEND_NOT_ANALYZED = "the frontend was not analyzed, so frontend-derived results are n/a."
FRONTEND_NOT_ANALYZED_BANNER = f"⚠ INCOMPLETE — {_FRONTEND_NOT_ANALYZED}"


def incomplete_banner(diagnostics: Sequence[Mapping]) -> str:
    """`problems.incomplete_banner`, except for a frontend that was not analyzed:
    its counts are n/a, not undercounts, so it gets its own sentence."""
    frontend = [d for d in diagnostics if d.get("kind") == "frontend_unavailable"]
    if not frontend:
        return problems.incomplete_banner(diagnostics)
    others = [d for d in diagnostics if d.get("kind") != "frontend_unavailable"]
    if not others:
        return FRONTEND_NOT_ANALYZED_BANNER
    return f"{problems.incomplete_banner(others)} Also, {_FRONTEND_NOT_ANALYZED}"


def omitted_note(shown: int, total: int, noun: str, indent: str = "", *, limit_for_all: int | None = None) -> str:
    """``limit_for_all`` is the limit that shows every row when it differs from
    ``total``, as when one limit is shared by several groups."""
    needed = total if limit_for_all is None else limit_for_all
    return f"{indent}... showing {shown} of {total} {noun}; pass limit={needed} (or null) to see all"


def call_site(file: str, call_line: int, def_line: int | None) -> str:
    """`[file def D · call C]` for a frontend call: a bare `file:C` reads as the
    function's location, but C is the API call inside it."""
    if def_line is None or def_line == call_line:
        return f"[{file}:{call_line}]"
    return f"[{file} def {def_line} · call {call_line}]"


def problems_text(items: Sequence[Mapping]) -> list[str]:
    """One line per diagnostic, naming its file and failed step (it is in the message)."""
    lines = []
    for p in items[:5]:
        loc = p.get("file") or "?"
        if p.get("line"):
            loc = f"{loc}:{p['line']}"
        lines.append(f"  [{p.get('kind')}] {loc}: {p.get('message')}")
    if len(items) > 5:
        lines.append(f"  ... and {len(items) - 5} more; see output_format='json'")
    return lines


def format_line_range(start: int, end: int | None) -> str:
    if end is not None and end != start:
        return f"{start}-{end}"
    return str(start)


def _target_where(info: Mapping[str, Any]) -> str:
    """Where a dependency target is defined: its file and line, or the installed code that defines it."""
    if info.get("file"):
        return f"[{info['file']}:{info['line']}]"
    if info.get("library"):
        return f"[{library_text(info['library'])}]"
    return "[unresolved]"


def _with_alias(display: str, alias: str | None) -> str:
    return f"{display} [as {alias}]" if alias else display


# ── DI ───────────────────────────────────────────────────────────────────


def di_deps(result: queries.DependencyListing | queries.DiRouteSelection) -> Body:
    if isinstance(result, queries.DependencyListing):
        return _dependency_listing(result)
    return _di_routes(result)


def _dependency_listing(result: queries.DependencyListing) -> Body:
    functions = result.functions
    lines = [f"Dependency targets ({functions.total}):", ""]
    for row in functions.items:
        kind = "" if row["kind"] == "function" else f" ({row['kind']})"
        ident = f"  {row['id']}" if row["label"] != row["name"] else ""
        aliases = f"  [as {', '.join(row['aliases'])}]" if row["aliases"] else ""
        sub = ", ".join(row["sub_dep_labels"]) or "(leaf)"
        loc = _target_where(row)
        bound = f"  = {row['bound_to']}" if row.get("bound_to") else ""
        lines.append(f"  {row['name']}{kind}  {loc}{ident}{aliases}  -> {sub}{bound}")
    if functions.omitted:
        lines.append(omitted_note(len(functions.items), functions.total, "targets", "  "))
    lines.append("")
    lines.append(f"Routes: {result.route_count} total")
    payload = {
        "dependency_function_count": functions.total,
        "route_count": result.route_count,
        "dependency_functions": functions.items,
        "omitted": functions.omitted,
    }
    return Body("\n".join(lines), payload)


_DEP_SECTIONS = (
    ("app", "App deps:     "),
    ("router", "Router deps:  "),
    ("route decorator", "Route deps:   "),
    ("direct", "Direct deps:  "),
)


def _given(**arguments: str | None) -> str:
    """``name=value`` for each argument the caller gave, in order."""
    return ", ".join(f"{name}={value}" for name, value in arguments.items() if value is not None)


def _di_routes(result: queries.DiRouteSelection) -> Body:
    q = result.query
    routes = result.routes
    if not routes.total:
        given = _given(handler=q.handler, path=q.path, method=q.method)
        text = f"No route found matching {given} (match={q.match})."
        if result.hints:
            text = f"{text} {' '.join(result.hints)}"
        payload = {"matches": [], "match_count": 0, "omitted": 0, "hints": result.hints, "query": q.as_dict()}
        return Body(text, payload)

    lines = []
    for r in routes.items:
        lines.append(f"{r['method']} {r['path']}  →  {r['handler']}()  [{route_where(r)}]")
        for via, label in _DEP_SECTIONS:
            shown = [_with_alias(d["display"], d.get("alias")) for d in r["deps"] if d["via"] == via]
            lines.append(f"  {label}{', '.join(shown) or '(none)'}")
        lines.append(f"  All deps:     {', '.join(r['all_dep_labels']) or '(none)'}")
        lines.append("")
    if routes.omitted:
        lines.append(omitted_note(len(routes.items), routes.total, "routes"))
    if result.hints:
        lines.append(" ".join(result.hints))
    payload = {
        "matches": routes.items,
        "match_count": routes.total,
        "omitted": routes.omitted,
        "hints": result.hints,
        "query": q.as_dict(),
    }
    return Body("\n".join(lines).rstrip("\n"), payload)


def di_reverse(result: queries.DependencyUsage | queries.UnknownDependency) -> Body:
    if isinstance(result, queries.UnknownDependency):
        return _unknown_dependency(result)
    lines = _di_reverse_preamble(result)
    groups_payload = []
    for group in result.groups:
        lines += _dependency_group_lines(group, result.route_count)
        groups_payload.append(
            {
                "dependency": group.target,
                "routes": group.routes,
                "route_count": group.route_count,
                "via_counts": group.via_counts,
                "omitted": group.omitted,
            }
        )
    payload = {
        "dep_function": result.dep_function,
        "matched_on": result.matched_on,
        "groups": groups_payload,
        "route_count": result.route_count,
        "omitted": sum(g.omitted for g in result.groups),
        "suggestions": [],
    }
    return Body("\n".join(lines).rstrip("\n"), payload)


def _unknown_dependency(result: queries.UnknownDependency) -> Body:
    text = f"Unknown dependency '{result.dep_function}': no dependency target has that id, name or alias."
    if result.suggestions:
        text = f"{text} Did you mean {', '.join(result.suggestions)}?"
    else:
        text = f"{text} {result.known_count} dependency targets exist; call di_deps() with no arguments to list them."
    payload = {
        "dep_function": result.dep_function,
        "matched_on": None,
        "groups": [],
        "route_count": 0,
        "omitted": 0,
        "suggestions": result.suggestions,
    }
    return Body(text, payload)


def _di_reverse_preamble(result: queries.DependencyUsage) -> list[str]:
    """How the query matched, when that is not simply the exact id or name given."""
    lines: list[str] = []
    if result.query_name != result.dep_function:
        lines.append(f"Matched on {result.matched_on} '{result.query_name}'; factory arguments are not filtered.")
    elif result.matched_on == "alias":
        lines.append(f"Matched on alias '{result.query_name}'.")
    if len(result.groups) > 1:
        lines.append(
            f"'{result.query_name}' matches {len(result.groups)} dependency targets; routes are listed per definition."
        )
    return [*lines, ""] if lines else lines


def _dependency_group_lines(group: queries.DependencyGroup, limit_for_all: int) -> list[str]:
    info = group.target
    loc = _target_where(info)
    lines = [f"{info['name']}  {loc}  {info['id']}", f"  Kind: {info['kind']}"]
    if info.get("bound_to"):
        lines.append(f"  Bound to: {info['bound_to']}")
    if info["aliases"]:
        lines.append(f"  Aliases: {', '.join(info['aliases'])}")
    lines.append(f"  Sub-deps: {', '.join(group.sub_dep_labels) or '(none)'}")
    lines.append(f"  Transitive: {', '.join(group.transitive_labels) or '(none)'}")
    breakdown = ", ".join(f"{n} {label}" for label, n in sorted(group.via_counts.items()))
    lines.append(f"  Routes using it ({group.route_count}{': ' + breakdown if breakdown else ''}):")
    for r in group.routes:
        lines.append(f"    {r['method']} {r['path']}  →  {r['handler']}()  [{route_where(r)}]  ({_via_text(r, info)})")
    if group.omitted:
        lines.append(omitted_note(len(group.routes), group.route_count, "routes", "    ", limit_for_all=limit_for_all))
    lines.append("")
    return lines


def _via_text(row: Mapping[str, Any], info: Mapping[str, Any]) -> str:
    """The via label, plus the dependency as the route declared it when that differs from its name."""
    shown = _with_alias(row["via_dep"], row["via_alias"]) if row["via_dep"] else None
    return f"{row['via']}: {shown}" if shown and shown != info["name"] else row["via"]


# ── Routes ───────────────────────────────────────────────────────────────


def route_match(result: queries.RouteMatchResult) -> Body:
    if isinstance(result, queries.RouteSummary):
        return _route_summary(result)
    if isinstance(result, queries.RouteMatches):
        return _route_matches(result)
    if isinstance(result, queries.RouteOrphans):
        return _route_orphans(result)
    if isinstance(result, queries.RouteFrontendMissing):
        return _route_frontend_missing(result)
    return _route_no_match(result)


_NOT_ANALYZED = "n/a (frontend not analyzed)"
# Gap labels a note shows before summarizing the rest; the JSON lists all.
_GAPS_SHOWN = 5


def _gap_label(gap: Mapping) -> str:
    where = gap.get("file")
    if where and gap.get("line") is not None:
        where = f"{where}:{gap['line']}"
    return f"{where} ({gap.get('kind')})" if where else str(gap.get("kind"))


def _gap_list(gaps: Sequence[Mapping]) -> str:
    labels = list(dict.fromkeys(_gap_label(g) for g in gaps))
    shown = ", ".join(labels[:_GAPS_SHOWN])
    rest = len(labels) - _GAPS_SHOWN
    return f"{shown} and {rest} more (see output_format='json')" if rest > 0 else shown


# One line per kind of effect behind a figure marked as a bound.
_EFFECT_NOTES = {
    queries.CALLS_MISSED: "Frontend calls may be missing from the scan",
    queries.VARIANTS_DROPPED: "Some URL variants of a frontend call were dropped",
    queries.CALLS_MISREAD: "A frontend call's URL may be misread",
    queries.ROUTES_MISSED: "Backend routes may be missing from the analysis",
    queries.ROUTES_MISPLACED: "Backend routes sit at a path with a part the analysis could not evaluate",
    queries.ROUTES_CHANGED: "Backend routes may be added, moved or removed in ways the analysis does not model",
    queries.CALLS_CAUGHT: "A mount the analysis misses or misplaces may catch requests for routes after it",
}


def _effect_notes(gaps: queries.OrphanGaps, figures: Sequence[str], indent: str = "") -> list[str]:
    return [f"{indent}⚠ {_EFFECT_NOTES[effect]} ({_gap_list(rows)})." for effect, rows in gaps.effects(figures).items()]


# Per orphan list: (an entry, what it lacks).
_ORPHAN_WORDS = {"backend_only": ("route", "caller"), "frontend_only": ("call", "route")}


def _orphan_notes(gaps: queries.OrphanGaps, figure: str, count: int) -> list[str]:
    """Why an orphan list may hold an entry that is no orphan in the app, or
    lack one that is."""
    entry, lacks = _ORPHAN_WORDS[figure]
    lines = []
    if gaps.confirmed(figure, count) is False:
        lines.append(
            f"  ⚠ Not confirmed: a {entry} here may have a {lacks} ({_gap_list(gaps.rows(figure, queries.DOWN))})."
        )
    if gaps.bound(figure, count) in _INCOMPLETE:
        lines.append(
            f"  ⚠ May be incomplete: a {entry} with no {lacks} may be missing here "
            f"({_gap_list(gaps.rows(figure, queries.UP))})."
        )
    return lines


def _missing_notes(gaps: queries.OrphanGaps, figure: str, entry: str) -> list[str]:
    """For a reply that found nothing: the findings that may hide what it looked for."""
    if gaps.bound(figure, 0) not in _INCOMPLETE:
        return []
    return [f"⚠ May be incomplete: a {entry} may be missing here ({_gap_list(gaps.rows(figure, queries.UP))})."]


def _orphan_gap_fields(gaps: queries.OrphanGaps, backend_count: int | None, frontend_count: int | None) -> dict:
    """The JSON fields that qualify each side's orphan claim; a side not
    reported is null."""
    return {
        "backend_only_confirmed": gaps.confirmed("backend_only", backend_count),
        "backend_only_gaps": None if backend_count is None else gaps.rows("backend_only"),
        "backend_only_count_bound": gaps.bound("backend_only", backend_count),
        "frontend_only_confirmed": gaps.confirmed("frontend_only", frontend_count),
        "frontend_only_gaps": None if frontend_count is None else gaps.rows("frontend_only"),
        "frontend_only_count_bound": gaps.bound("frontend_only", frontend_count),
    }


# Bounds under which a list can be missing entries.
_INCOMPLETE = ("at_least", "unbounded")
_BOUND_WORDS = queries.BOUND_WORDS


def _orphan_heading(title: str, count: int, confirmed: bool | None, bound: str | None) -> str:
    flags = ["not confirmed"] if confirmed is False else []
    if bound in _INCOMPLETE:
        flags.append("may be incomplete")
    return f"{title}{', ' + ' and '.join(flags) if flags else ''} ({count}):"


def _count(value: Any) -> str:
    return _NOT_ANALYZED if value is None else str(value)


def _figure(value: Any, bound: str | None, extra: str | None = None) -> str:
    """A summary figure with what it bounds; the findings behind it are noted below."""
    words = [w for w in (extra, _BOUND_WORDS.get(bound or "")) if w]
    if bound in _BOUND_WORDS:
        words.append("see below")
    return f"{value} ({'; '.join(words)})" if words else str(value)


def _library_routes(summary: Mapping[str, Any]) -> str:
    """``8 (3 matched)``: the routes installed code adds, which neither coverage nor backend-only counts."""
    matched = summary.get("matched_library_routes")
    return f"{summary['library_routes']}" + ("" if matched is None else f" ({matched} matched)")


def _route_summary(result: queries.RouteSummary) -> Body:
    s = result.summary
    coverage = s.get("coverage_pct")
    gaps = result.gaps
    bounds = gaps.summary_bounds(s)
    if coverage is not None:
        coverage_line = _figure(f"{coverage}%", bounds["coverage_pct"])
    else:
        coverage_line = _NOT_ANALYZED if result.frontend_problem else "n/a (analysis incomplete)"
    skipped = s["test_calls_skipped"]
    calls = _figure(
        _count(s["frontend_calls"]),
        bounds["frontend_calls"],
        None if skipped is None else f"test-file calls skipped: {skipped}",
    )
    text = (
        f"Route matching summary:\n"
        f"  Backend routes:  {_figure(s['backend_routes'], bounds['backend_routes'])}\n"
        f"  Frontend calls:  {calls}\n"
        f"  Matched routes:  {_figure(_count(s['matched_routes']), bounds['matched_routes'])}\n"
        f"  Matched calls:   {_figure(_count(s['matched_calls']), bounds['matched_calls'])}\n"
        f"  Match rows:      {_figure(_count(s['match_rows']), bounds['match_rows'])}\n"
        f"  Backend-only:    {_figure(_count(s['backend_only']), bounds['backend_only'])}\n"
        f"  Frontend-only:   {_figure(_count(s['frontend_only']), bounds['frontend_only'])}\n"
        f"  Coverage:        {coverage_line}"
    )
    if s.get("library_routes"):
        text = f"{text}\n  Library routes:  {_library_routes(s)}"
    if result.frontend_problem:
        text = f"{text}\n  Frontend:        not analyzed: {result.frontend_problem}"
    bounded = [f for f in queries.SUMMARY_FIGURES if bounds[f] in _BOUND_WORDS]
    text = "\n".join([text, *_effect_notes(gaps, bounded, "  ")])
    payload = {
        "summary": s,
        "summary_bounds": bounds,
        "frontend_problem": result.frontend_problem,
        **_orphan_gap_fields(gaps, s["backend_only"], s["frontend_only"]),
    }
    return Body(text, payload)


# Every route_match reply to a query has these keys, whichever branch answered;
# a key that is not part of the answer is null ([] for `matches` and `hints`).
_ROUTE_QUERY_KEYS: dict[str, Any] = {
    "matches": [],
    "match_count": 0,
    "omitted": 0,
    "hints": [],
    "backend_only": None,
    "backend_only_count": None,
    "backend_omitted": None,
    "frontend_only": None,
    "frontend_only_count": None,
    "frontend_omitted": None,
    "backend_routes": None,
    "backend_route_count": None,
    "frontend_problem": None,
    "match_count_bound": None,
    "backend_route_count_bound": None,
    "backend_only_confirmed": None,
    "backend_only_gaps": None,
    "backend_only_count_bound": None,
    "frontend_only_confirmed": None,
    "frontend_only_gaps": None,
    "frontend_only_count_bound": None,
}


def _route_query_payload(query: queries.RouteQuery, **values: Any) -> dict[str, Any]:
    unknown = set(values) - set(_ROUTE_QUERY_KEYS)
    if unknown:
        raise ValueError(f"route_match payload keys {sorted(unknown)} are not in _ROUTE_QUERY_KEYS")
    return {**_ROUTE_QUERY_KEYS, **values, "query": query.as_dict()}


def _frontend_line(f: Mapping) -> str:
    client = f" via {f['client']}" if f.get("client") else ""
    return f"{f.get('function') or '(module)'}()  {call_site(f['file'], f['line'], f.get('def_line'))}{client}"


def _route_matches(result: queries.RouteMatches) -> Body:
    matches = result.matches
    lines = []
    for m in matches.items:
        b, f = m["backend"], m["frontend"]
        possible = f"  (possible: {m['reason']})" if m["confidence"] == "possible" else ""
        lines.append(
            f"{b['method']} {b['path']}{possible}\n"
            f"  Backend:  {b['handler']}()  [{route_where(b)}]\n"
            f"  Frontend: {f['method']} {f['raw_path']}  {_frontend_line(f)}"
        )
    if matches.omitted:
        lines.append(omitted_note(len(matches.items), matches.total, "matches"))
    payload = _route_query_payload(
        result.query,
        matches=matches.items,
        match_count=matches.total,
        match_count_bound=result.gaps.bound("match_rows", matches.total),
        omitted=matches.omitted,
    )
    return Body("\n".join(lines), payload)


def _route_frontend_missing(result: queries.RouteFrontendMissing) -> Body:
    """No orphan claim: an unanalyzed frontend proves nothing about consumers."""
    backend = result.backend
    lines = [
        f"Frontend not analyzed: {result.frontend_problem}",
        "No frontend consumer can be confirmed or ruled out until the frontend is analyzed.",
    ]
    if result.query.frontend_func is not None:
        lines.append(f"frontend_func={result.query.frontend_func!r} cannot be looked up without the frontend.")
    if backend.total:
        lines.append(f"\nBackend routes that fit the query ({backend.total}):")
        for r in backend.items:
            lines.append(f"  {r['method']} {r['path']}  →  {r['handler']}()  [{route_where(r)}]")
        if backend.omitted:
            lines.append(omitted_note(len(backend.items), backend.total, "routes", "  "))
    elif result.query.handler is not None or result.query.path is not None:
        lines.append("\nNo backend route fits the query.")
        lines.extend(_missing_notes(result.gaps, "backend_routes", "route"))
    payload = _route_query_payload(
        result.query,
        match_count=None,
        backend_routes=backend.items,
        backend_route_count=backend.total,
        backend_route_count_bound=result.gaps.bound("backend_routes", backend.total),
        omitted=backend.omitted,
        frontend_problem=result.frontend_problem,
    )
    return Body("\n".join(lines), payload)


def _route_orphans(result: queries.RouteOrphans) -> Body:
    backend, frontend, q = result.backend, result.frontend, result.query
    # A side is only consulted for a filter that names it; the other reports nothing.
    gap_fields = _orphan_gap_fields(
        result.gaps,
        backend.total if q.handler is not None or q.path is not None else None,
        frontend.total if q.frontend_func is not None or q.path is not None else None,
    )
    lines = [
        "No matched pair found. Unmatched entries that fit the query:",
        *_missing_notes(result.gaps, "match_rows", "match"),
    ]
    if backend.total:
        confirmed, bound = gap_fields["backend_only_confirmed"], gap_fields["backend_only_count_bound"]
        title = "Backend routes with no frontend consumer" + ("" if confirmed else " found")
        lines.append("\n" + _orphan_heading(title, backend.total, confirmed, bound))
        lines.extend(_orphan_notes(result.gaps, "backend_only", backend.total))
        for r in backend.items:
            lines.append(f"  {r['method']} {r['path']}  →  {r['handler']}()  [{route_where(r)}]")
        if backend.omitted:
            lines.append(omitted_note(len(backend.items), backend.total, "routes", "  "))
    if frontend.total:
        confirmed, bound = gap_fields["frontend_only_confirmed"], gap_fields["frontend_only_count_bound"]
        title = "Frontend calls with no backend route" + ("" if confirmed else " found")
        lines.append("\n" + _orphan_heading(title, frontend.total, confirmed, bound))
        lines.extend(_orphan_notes(result.gaps, "frontend_only", frontend.total))
        for c in frontend.items:
            lines.append(f"  {c['method']} {c['path']}  ←  {_frontend_line(c)}")
        if frontend.omitted:
            lines.append(omitted_note(len(frontend.items), frontend.total, "calls", "  "))
    # The same keys as route_unmatched, so one reader handles both.
    payload = _route_query_payload(
        result.query,
        backend_only=backend.items,
        backend_only_count=backend.total,
        backend_omitted=backend.omitted,
        frontend_only=frontend.items,
        frontend_only_count=frontend.total,
        frontend_omitted=frontend.omitted,
        omitted=backend.omitted + frontend.omitted,
        match_count_bound=result.gaps.bound("match_rows", 0),
        **gap_fields,
    )
    return Body("\n".join(lines), payload)


def _route_no_match(result: queries.RouteNoMatch) -> Body:
    q = result.query
    given = _given(path=q.path, handler=q.handler, frontend_func=q.frontend_func)
    text = f"No match found for {given} (match={q.match})."
    if result.hints:
        text = f"{text} {' '.join(result.hints)}"
    text = "\n".join([text, *_missing_notes(result.gaps, "match_rows", "match")])
    return Body(text, _route_query_payload(q, hints=result.hints, match_count_bound=result.gaps.bound("match_rows", 0)))


_UNMATCHED_KEYS = (
    "backend_only",
    "backend_only_count",
    "backend_omitted",
    "frontend_only",
    "frontend_only_count",
    "frontend_omitted",
    "backend_only_confirmed",
    "backend_only_gaps",
    "backend_only_count_bound",
    "frontend_only_confirmed",
    "frontend_only_gaps",
    "frontend_only_count_bound",
)


def route_unmatched(result: queries.UnmatchedRoutes) -> Body:
    lines = []
    # Both sides' keys are in every reply; the side not asked for is null.
    payload: dict[str, Any] = {"side": result.side, **dict.fromkeys(_UNMATCHED_KEYS)}
    payload.update(
        _orphan_gap_fields(
            result.gaps,
            None if result.backend is None else result.backend.total,
            None if result.frontend is None else result.frontend.total,
        )
    )
    if result.backend is not None:
        bo = result.backend
        confirmed, bound = payload["backend_only_confirmed"], payload["backend_only_count_bound"]
        lines.append(_orphan_heading("Backend-only routes", bo.total, confirmed, bound))
        lines.extend(_orphan_notes(result.gaps, "backend_only", bo.total))
        for r in bo.items:
            lines.append(f"  {r['method']:<6} {r['path']:<50} {r['handler']}()  [{route_where(r)}]")
        if bo.omitted:
            lines.append(omitted_note(len(bo.items), bo.total, "routes", "  "))
        lines.append("")
        payload.update({"backend_only": bo.items, "backend_only_count": bo.total, "backend_omitted": bo.omitted})
    if result.frontend is not None:
        fo = result.frontend
        confirmed, bound = payload["frontend_only_confirmed"], payload["frontend_only_count_bound"]
        lines.append(_orphan_heading("Frontend-only calls", fo.total, confirmed, bound))
        lines.extend(_orphan_notes(result.gaps, "frontend_only", fo.total))
        for c in fo.items:
            lines.append(f"  {c['method']:<6} {c['path']:<50} {_frontend_line(c)}")
        if fo.omitted:
            lines.append(omitted_note(len(fo.items), fo.total, "calls", "  "))
        lines.append("")
        payload.update({"frontend_only": fo.items, "frontend_only_count": fo.total, "frontend_omitted": fo.omitted})
    payload["omitted"] = sum(p.omitted for p in (result.backend, result.frontend) if p is not None)
    return Body("\n".join(lines).rstrip("\n"), payload)


# ── JSX and symbols ──────────────────────────────────────────────────────


def _jsx_usage_row(u: Any) -> dict[str, Any]:
    return {
        "file": u.file,
        "line": u.line,
        "column": u.column,
        "snippet": u.snippet,
        "parent_function": u.parent_function,
        "tag_kind": u.tag_kind,
        "is_test": u.is_test,
    }


def jsx_usage(result: queries.JsxUsageResult) -> Body:
    usages = result.usages
    filters = []
    if not result.include_self_close:
        filters.append("self-closing tags excluded")
    filters.append("test files included" if result.include_tests else "test files excluded")
    scope = f" ({', '.join(filters)})"
    if not usages.total:
        lines = [f"No JSX usages of <{result.component}> found under {result.root}/{scope}."]
    else:
        lines = [
            f"Found {usages.total} JSX usages of <{result.component}> across {result.file_count} file(s){scope}:",
            "",
        ]
        for u in usages.items:
            parent = f" in {u.parent_function}" if u.parent_function else ""
            test = " [test]" if u.is_test else ""
            lines.append(f"  {u.file}:{u.line}:{u.column}  {u.tag_kind}{parent}{test}")
            lines.append(f"    {u.snippet}")
        if usages.omitted:
            lines.append(omitted_note(len(usages.items), usages.total, "usages", "  "))
    diagnostics = [p for p in result.problems if p.get("category")]
    if diagnostics:
        lines.append("")
        lines.append(f"⚠ {len(diagnostics)} file problem(s); results may be partial:")
        lines.extend(problems_text(diagnostics))
    payload = {
        "component": result.component,
        "usage_count": usages.total,
        "file_count": result.file_count,
        "usages": [_jsx_usage_row(u) for u in usages.items],
        "omitted": usages.omitted,
        "root": result.root,
        "include_self_close": result.include_self_close,
        "include_tests": result.include_tests,
    }
    return Body("\n".join(lines), payload)


def _symbol_row(row: queries.SymbolRow) -> dict[str, Any]:
    c = row.candidate
    return {
        "name": c.name,
        "file": c.file,
        "line": c.start_line,
        "end_line": c.end_line,
        "kind": c.kind,
        "scope": getattr(c, "scope", None),
        "container": getattr(c, "container", None),
        "exported": getattr(c, "exported", None),
        "is_test": getattr(c, "is_test", None),
        "signature": c.signature,
        "language": c.language,
        "score": c.score,
        "file_hint_match": row.file_hint_match,
        "protocol_method": bool(getattr(c, "protocol_method", False)),
        "protocols": [
            {"name": m.name, "id": m.id, "file": m.file, "line": m.line} for m in getattr(c, "protocols", None) or ()
        ],
    }


def _protocol_method(match: Any, method: str) -> str:
    """`Protocol.method`, from the Protocol's id (`module:Qual.Name`)."""
    return f"{match.id.partition(':')[2]}.{method}"


def symbol_resolution(result: queries.SymbolResolution) -> Body:
    rows = result.candidates
    payload: dict[str, Any] = {
        "name": result.name,
        "side": result.side,
        "kind": result.kind,
        "file_hint": result.file_hint,
        "candidate_count": rows.total,
        "candidates": [_symbol_row(row) for row in rows.items],
        "omitted": rows.omitted,
        "frontend_problem": result.frontend_problem,
    }
    tail: list[str] = []
    diagnostics = [p for p in result.problems if p.get("category")]
    if result.frontend_problem:
        tail.append(f"⚠ Frontend symbols missing: {result.frontend_problem}")
        # The line above already states the frontend problem; list only the others.
        diagnostics = [p for p in diagnostics if p.get("kind") != "frontend_unavailable"]
    if diagnostics:
        tail.append(f"⚠ {len(diagnostics)} file problem(s); results may be partial:")
        tail.extend(problems_text(diagnostics))

    if not rows.total:
        text = f"No candidates found for '{result.name}' (side={result.side}, kind={result.kind or 'any'})."
        return Body("\n".join([text, *tail]), payload)

    lines = [f"{rows.total} candidate(s) for '{result.name}':", ""]
    for idx, row in enumerate(rows.items, start=1):
        c = row.candidate
        line_range = format_line_range(c.start_line, c.end_line)
        note = " (file_hint match)" if row.file_hint_match else ""
        container = getattr(c, "container", None)
        protocol_note = " (Protocol)" if getattr(c, "protocol_method", False) else ""
        where = f" in {container}{protocol_note}" if container else ""
        lines.append(f"  {idx}. {c.kind:<10} {c.file}:{line_range}{where}")
        lines.append(f"     {c.signature}")
        if protocols := getattr(c, "protocols", None):
            resolves_to = ", ".join(f"{_protocol_method(m, c.name)} ({m.file}:{m.line})" for m in protocols)
            lines.append(f"     calls typed as a Protocol it matches (by member names) resolve to: {resolves_to}")
        lines.append(f"     score {c.score:.2f}{note}")
        lines.append("")
    if rows.omitted:
        lines.append(omitted_note(len(rows.items), rows.total, "candidates", "  "))
    lines.extend(tail)
    return Body("\n".join(lines).rstrip("\n"), payload)


# ── Status and refresh ───────────────────────────────────────────────────


# What fails while the last helper run in a mode crashed, keyed by helper mode.
_HELPER_MODE_IMPACT = {
    "facts": "JSX usage and frontend symbols fail until a facts-mode helper run succeeds.",
    "routes": "Route matching fails until a routes-mode helper run succeeds.",
}


def _problem_detail(problem: Mapping[str, Any]) -> str:
    return f"{problems.strip_fix(str(problem.get('message')), problem.get('fix'))}. Fix: {problem.get('fix')}."


def _toolchain_text(t: Mapping[str, Any]) -> str:
    ts = t["typescript"]
    found = f"node {t['node']['version']}, typescript {ts['version']} ({ts['dir']})"
    if t["state"] == "ok":
        return f"Frontend toolchain: {found}: ok"
    if t["state"] == "helper_failed":
        # Each mode serves different tools and recovers on its own, so each
        # outstanding failure is listed with what it breaks.
        parts = [
            f"the last TypeScript helper run in {mode} mode failed: {_problem_detail(failure)} {_HELPER_MODE_IMPACT[mode]}"
            for mode, failure in t["helper_failures"].items()
        ]
        return f"Frontend toolchain: {found}, but " + " Also, ".join(parts)
    detail = _problem_detail(t["problem"] or {})
    return (
        f"Frontend toolchain: unavailable: {detail} "
        f"DI tools are unaffected; JSX usage, frontend symbols and route matching need it."
    )


def status(s: Any) -> Body:
    """`status` body; ``s`` is an `analysis_runtime.StatusSnapshot`."""
    lines: list[str] = []
    if s.complete is False:
        lines.append(_status_incomplete_banner(s))
    lines += [
        "code-intel status:",
        f"  Repo root:      {s.repo_root}",
        f"  Config:         {s.config.get('path')}",
        f"  Git SHA:        {s.git_sha or '(unknown)'}",
        f"  Python:         {s.python}",
        f"  Complete:       {_tri(s.complete)}",
        f"  DI cache:       {_cache_text(s.cache['di'])}",
        f"  Routes cache:   {_cache_text(s.cache['routes'])}",
        f"  {_toolchain_text(s.toolchain)}",
    ]
    if s.fastapi:
        lines.append(f"  Include model:  {s.fastapi['include_model']} ({s.fastapi['summary']})")
    if s.library_capture is not None:
        lines.append(f"  Library capture: {_library_capture_text(s.library_capture)}")
    if s.config.get("error"):
        lines.append(f"  Config error:   {s.config['error']}")
    if s.runtime_compatible is False:
        lines.append(
            f"  Python {s.python_running} is older than the backend's {s.python_required}; "
            f"DI, routes and backend symbols are refused. Restart the server with Python >={s.python_required}."
        )
    lines += [f"  Toolchain warning: {warning}" for warning in s.toolchain.get("warnings") or []]
    for name, info in s.analyzers.items():
        error = f" last_error={info['last_error']}" if info["last_error"] else ""
        runtime_ms = "" if info["last_runtime_ms"] is None else f" last_runtime_ms={info['last_runtime_ms']}"
        lines.append(f"  {name}: {info['state']}{runtime_ms}{error}")
    if s.diagnostics:
        lines.append(f"  Diagnostics ({len(s.diagnostics)}):")
        lines.extend("  " + line for line in problems_text(s.diagnostics))
    if s.limitations:
        lines.extend("  " + line for line in problems.limitations_banner(s.limitations).splitlines())
    lines += _status_counts_lines(s)
    return Body("\n".join(lines), _status_payload(s))


def _library_capture_text(block: Mapping[str, Any]) -> str:
    if not block["present"]:
        return f"{block['file']} does not exist; run code-intel capture"
    entries = block["entries"]
    applied = block["applied"]
    counts = (
        f"{entries['includes']} includes ({entries['routes']} routes), {entries['dependencies']} dependencies"
        if applied is None
        else f"{applied['includes']} includes ({applied['routes']} routes), "
        f"{applied['dependencies']} dependencies applied"
    )
    versions = ", ".join(f"{name} {version}" for name, version in block["distributions"].items())
    stale = [*block["problems"], *block["stale"]]
    suffix = f"; stale: {len(stale)} (run code-intel capture)" if stale else ""
    return f"{block['file']}: {counts}; {versions or 'no distributions'}{suffix}"


def _status_payload(s: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo_root": s.repo_root,
        "tool_dir": s.tool_dir,
        "git_sha": s.git_sha,
        "runtime": {
            "python": s.python,
            "executable": s.executable,
            "runtime_compatible": s.runtime_compatible,
            "python_running": s.python_running,
            "python_required": s.python_required,
        },
        "analyzers": s.analyzers,
        "cache": s.cache,
        "toolchain": s.toolchain,
        "fastapi": s.fastapi,
        "library_capture": s.library_capture,
        "config": s.config,
        "counts": s.counts,
    }
    if s.counts_error is not None:
        payload["counts_error"] = s.counts_error
    if s.counts_errors:
        payload["counts_errors"] = s.counts_errors
    return payload


def _status_counts_lines(s: Any) -> list[str]:
    if s.counts_error is not None:
        return [f"  Counts: not loaded: {s.counts_error['message']}"]
    if s.counts is None:
        return []
    lines = ["  Counts:"]
    di, routes = s.counts.get("di"), s.counts.get("routes")
    if di is not None:
        lines.append(
            f"    DI: {di['routes']} routes ({di['http']} HTTP, {di['websocket']} WebSocket), "
            f"{di['dependency_functions']} dependency targets, {di['files_scanned']} files"
        )
    if routes is not None:
        lines.append(
            f"    Routes: {routes['backend_routes']} backend routes, {routes['frontend_calls']} frontend calls, "
            f"{routes['matched_routes']} matched routes, {routes['matched_calls']} matched calls, "
            f"{routes['backend_only']} backend-only, {routes['frontend_only']} frontend-only"
        )
    lines += [f"    {name}: failed: {error['message']}" for name, error in (s.counts_errors or {}).items()]
    return lines


def _tri(value: bool | None) -> str:
    return "unknown (an analysis has not run on the current sources)" if value is None else str(value).lower()


def _cache_text(info: Mapping[str, Any]) -> str:
    if not info["loaded"]:
        return "not loaded"
    if info["fresh"]:
        return "fresh"
    return "loaded; its sources changed moments ago, so the next call checks them again"


def _status_incomplete_banner(s: Any) -> str:
    if s.diagnostics:
        return incomplete_banner(s.diagnostics)
    failed = [name for name, info in s.analyzers.items() if info["state"] == "failed"]
    if failed:
        noun = "their results are" if len(failed) > 1 else "its results are"
        details = "; ".join(f"{name} failed ({s.analyzers[name]['last_error']})" for name in failed)
        return f"⚠ INCOMPLETE — {details}; {noun} missing until a rerun succeeds."
    return "⚠ INCOMPLETE — a loaded analysis reported an incomplete run."


def refresh(result: Any) -> Body:
    """``result`` is an `analysis_runtime.RefreshResult`. Without an analyzed
    frontend the matched counts would read as "nothing matched", so they are
    null and the text names the problem instead."""
    routes = dict(result.routes_summary)
    frontend_problem = result.frontend_problem
    if frontend_problem is None:
        routes_line = (
            f"{routes['matched_calls']}/{routes['frontend_calls']} frontend calls matched, "
            f"{routes['matched_routes']}/{routes['backend_routes']} backend routes matched"
        )
        problem_text = None
    else:
        problem_text = queries.frontend_problem_text(frontend_problem.as_diagnostic_fields())
        routes.update(dict.fromkeys(queries.FRONTEND_SUMMARY_KEYS))
        routes_line = f"{routes['backend_routes']} backend routes; frontend not analyzed: {problem_text}"
    text = (
        f"Caches dropped and rebuilt.\n"
        f"  DI: {result.dependency_function_count} dependency targets, {result.di_route_count} routes\n"
        f"  Routes: {routes_line}"
    )
    payload = {
        "status": "refreshed",
        "dependency_function_count": result.dependency_function_count,
        "di_route_count": result.di_route_count,
        "routes_summary": routes,
        "frontend_problem": problem_text,
    }
    return Body(text, payload)
