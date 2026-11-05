"""``code-intel summary``: the counts and coverage of this repository's analysis, as Markdown.

One section per analyzer, each rendered on its own: a section that fails
prints a ``Failed`` row naming the analyzer and the error in its place, so the
other sections are kept (and the exit code is 1). A section that cannot run
for a missing frontend toolchain prints an ``Unavailable`` row with its fix,
and one that does not apply to the config prints ``Skipped``.

The output is meant for a job summary::

    code-intel summary >> "$GITHUB_STEP_SUMMARY"
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from code_intel import code_paths, problems, queries
from code_intel.analysis_runtime import AnalysisFailed, AnalysisRuntime, FrontendFailed
from code_intel.config import ConfigFile
from code_intel.ts_bridge import HelperError, ToolchainUnavailable, incomplete_toolchain_problem

if TYPE_CHECKING:
    from code_intel.cli import Context

Rows = list[tuple[str, object]]


class Skipped(Exception):
    """A section that does not apply to this config."""


def by_kind(counter: Counter[str]) -> str:
    return ", ".join(f"{kind} {n}" for kind, n in sorted(counter.items())) or "none"


def milliseconds(started: float) -> str:
    return f"{round((time.perf_counter() - started) * 1000)} ms"


def problem_kinds(rows: Any) -> str:
    return by_kind(Counter(row["kind"] for row in rows))


def calls_by_kind(frontend_calls: Sequence[Mapping[str, Any]]) -> Counter[str]:
    """Distinct non-test calls per kind. ``frontend_calls`` has one row per URL
    variant, so counting rows would not add up to ``summary.frontend_calls``."""
    kinds = {(c["file"], c["line"], c["column"]): c["kind"] for c in frontend_calls if not c["is_test"]}
    return Counter(kinds.values())


def backend_section(runtime: AnalysisRuntime) -> Rows:
    started = time.perf_counter()
    di = runtime.get_di()
    elapsed = milliseconds(started)
    s = di["summary"]
    app = di["app"]
    return [
        ("App", f"{app['id']} (FastAPI {app['fastapi_version'] or 'unknown'}, include model {app['include_model']})"),
        ("Routes", f"{s['routes']} ({s['http']} HTTP, {s['websocket']} WebSocket)"),
        ("Unmounted routes", len(di["unmounted_routes"])),
        ("Dependency targets", s["dependency_functions"]),
        *library_capture_rows(di["library"]),
        ("Backend files scanned", s["files_scanned"]),
        ("Diagnostics", problem_kinds(di["diagnostics"])),
        ("Limitations", problem_kinds(di["analysis_limitations"])),
        ("Cold DI analysis", elapsed),
    ]


def library_capture_rows(library: Mapping[str, Any] | None) -> Rows:
    if library is None:
        return []
    applied = (
        f"{library['includes']} includes ({library['routes']} routes), {library['dependencies']} dependencies applied"
    )
    return [("Library capture", f"{library['file']}: {applied}")]


def library_rows(summary: Mapping[str, Any], *, matched: bool) -> Rows:
    """The routes installed code adds, counted apart from the repository's (none: no row)."""
    if not summary["library_routes"]:
        return []
    shown = (
        f"{summary['library_routes']} ({summary['matched_library_routes']} matched)"
        if matched
        else summary["library_routes"]
    )
    return [("Library routes", shown)]


def routes_section(runtime: AnalysisRuntime) -> Rows:
    started = time.perf_counter()
    routes = runtime.get_routes()
    elapsed = milliseconds(started)
    s = routes["summary"]
    frontend_problem = runtime.routes_frontend_problem()
    if frontend_problem is not None:
        # Without the frontend every backend route would count as backend-only.
        return [
            ("Backend routes", s["backend_routes"]),
            *library_rows(s, matched=False),
            (
                "Frontend",
                f"not analyzed ({frontend_problem.code}): {frontend_problem.message}. Fix: {frontend_problem.fix}",
            ),
            ("Diagnostics", problem_kinds(routes["diagnostics"])),
            ("Routes analysis after DI", elapsed),
        ]
    bounds = queries.orphan_gaps(routes).summary_bounds(s)

    def bounded(key: str, text: object = None) -> str:
        shown = s[key] if text is None else text
        bound = bounds[key]
        return f"{shown} ({queries.BOUND_WORDS[bound]})" if bound in queries.BOUND_WORDS else str(shown)

    coverage = bounded("coverage_pct", f"{s['coverage_pct']}%") if s["coverage_pct"] is not None else "n/a (incomplete)"
    return [
        ("Backend routes", bounded("backend_routes")),
        *library_rows(s, matched=True),
        ("Frontend calls", f"{bounded('frontend_calls')} ({by_kind(calls_by_kind(routes['frontend_calls']))})"),
        ("Test-file calls skipped", s["test_calls_skipped"]),
        (
            "Matched routes / calls / rows",
            f"{bounded('matched_routes')} / {bounded('matched_calls')} / {bounded('match_rows')}",
        ),
        ("Confidence", by_kind(Counter(m["confidence"] for m in routes["matches"]))),
        ("Backend-only", bounded("backend_only")),
        ("Frontend-only", bounded("frontend_only")),
        ("Coverage", coverage),
        ("Diagnostics", problem_kinds(routes["diagnostics"])),
        ("Limitations", problem_kinds(routes["analysis_limitations"])),
        ("Routes analysis after DI", elapsed),
    ]


def frontend_facts_section(runtime: AnalysisRuntime) -> Rows:
    toolchain = runtime.toolchain()
    if not toolchain.available:
        raise ToolchainUnavailable(incomplete_toolchain_problem(toolchain))
    repo_root = runtime.repo_root
    frontend_root = runtime.config("summary").frontend.root
    walk: list[problems.Problem] = []
    files = code_paths.iter_source_files(
        repo_root / frontend_root,
        code_paths.TS_SUFFIXES,
        repo_root,
        walk,
        generated_parent=repo_root / frontend_root,
        visible=runtime.analyzers.list_repo_files(repo_root).files,
    )
    facts = runtime.facts_cache()
    started = time.perf_counter()
    by_rel = facts.facts_for(files)
    elapsed = milliseconds(started)
    elements = [rel for rel, f in by_rel.items() for _ in f.elements]
    tests = sum(1 for rel in elements if code_paths.is_test_path(rel))
    decls = Counter(d.kind for f in by_rel.values() for d in f.decls)
    failed = Counter(f.status for f in by_rel.values() if f.status != "ok")
    return [
        ("Frontend files", len(files)),
        ("JSX elements", f"{len(elements)} ({len(elements) - tests} non-test, {tests} test)"),
        ("TS declarations", f"{sum(decls.values())} ({by_kind(decls)})"),
        ("Files not analyzed", by_kind(failed)),
        ("Files with syntax errors", sum(1 for f in by_rel.values() if f.syntax_errors)),
        ("Walk problems", len(walk)),
        ("Facts helper, cold", f"{elapsed} (typescript {toolchain.typescript_version}, node {toolchain.node_version})"),
    ]


def python_symbols_section(runtime: AnalysisRuntime) -> Rows:
    found: list[problems.Problem] = []
    started = time.perf_counter()
    symbols = runtime.symbol_index().python_symbols(found)
    elapsed = milliseconds(started)
    kinds = Counter(symbol.kind for _, symbol in symbols)
    diagnostics, limitations = problems.partition(found)
    return [
        ("Python symbols", f"{sum(kinds.values())} ({by_kind(kinds)})"),
        ("Diagnostics", problem_kinds(p.as_dict() for p in diagnostics)),
        ("Limitations", problem_kinds(p.as_dict() for p in limitations)),
        ("Python index, cold", elapsed),
    ]


def docs_section(runtime: AnalysisRuntime) -> Rows:
    from code_intel import doc_queries

    if runtime.config("summary").docs is None:
        raise Skipped("the config has no [docs] table")
    started = time.perf_counter()
    report = runtime.get_doc_report().report
    result = doc_queries.doc_check(report, runtime.get_agent_findings())
    elapsed = milliseconds(started)
    kinds = Counter({kind: sum(by_status.values()) for kind, by_status in result.reference_counts.items()})
    return [
        ("Docs", result.doc_count),
        ("References", f"{result.reference_count} ({by_kind(kinds)}); {result.checked_count} checked"),
        ("Findings", len(result.findings)),
        ("Diagnostics", problem_kinds(result.diagnostics)),
        ("Limitations", problem_kinds(result.limitations)),
        ("Docs report, cold", elapsed),
    ]


# (title, analyzer, section, needs the backend interpreter floor)
SECTIONS: tuple[tuple[str, str, Callable[[AnalysisRuntime], Rows], bool], ...] = (
    ("Backend routes and dependencies", "di_map", backend_section, True),
    ("Frontend calls and matching", "route_map", routes_section, True),
    ("JSX and TypeScript declarations", "ts_facts", frontend_facts_section, False),
    ("Python symbols", "symbol_resolve", python_symbols_section, True),
    ("Docs", "doc_refs", docs_section, True),
)


def render(title: str, rows: Rows) -> str:
    lines = [f"### {title}", "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {str(value).replace('|', '/')} |" for name, value in rows]
    return "\n".join(lines)


def section_rows(runtime: AnalysisRuntime, analyzer: str, section: Callable[[AnalysisRuntime], Rows]) -> Rows:
    try:
        return section(runtime)
    except Skipped as exc:
        return [("Skipped", str(exc))]
    except AnalysisFailed as exc:
        return [("Failed", exc.message.splitlines()[0])]
    except ToolchainUnavailable as exc:
        return [("Unavailable", f"{exc.problem.message}. Fix: {exc.problem.fix}")]
    except FrontendFailed as exc:
        return [("Unavailable", f"{exc.problem.message}. Fix: {exc.problem.fix}")]
    except HelperError as exc:
        problem = exc.problem()
        return [("Unavailable", f"{problem.message}. Fix: {problem.fix}")]
    except Exception as exc:  # noqa: BLE001 - one broken section must not cost the summary of the others
        return [("Failed", f"{analyzer}: {type(exc).__name__}: {exc}")]


def summarize(runtime: AnalysisRuntime) -> tuple[str, bool]:
    """The Markdown and whether every section rendered without a ``Failed`` row."""
    running, required = runtime.runtime_versions()
    parts = ["## Code-intel analysis\n"]
    failed = False
    for title, analyzer, section, needs_backend_floor in SECTIONS:
        if needs_backend_floor and not runtime.runtime_compatible():
            rows: Rows = [("Skipped", f"Python {running} is older than the backend's {required}")]
        else:
            rows = section_rows(runtime, analyzer, section)
        failed = failed or any(name == "Failed" for name, _ in rows)
        parts.append(render(title, rows) + "\n")
    return "\n".join(parts), failed


def add_arguments(parser: argparse.ArgumentParser) -> None:
    del parser


def run(args: argparse.Namespace, context: Context) -> int:
    runtime = AnalysisRuntime(context.repo_root, ConfigFile.locate(context.repo_root, args.config), [])
    text, failed = summarize(runtime)
    print(text)
    return 1 if failed else 0
