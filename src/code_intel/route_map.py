"""Frontend ↔ backend route map.

Backend routes come from the FastAPI router graph (``BackendModel.routes``, in
Starlette dispatch order). Frontend calls come from the TypeScript helper's
``routes`` mode, which resolves API clients, URL builders and constants with the
TypeScript checker. ``route_matching`` pairs them.

Usage:
    code-intel dump routes              # human-readable output
    code-intel dump routes --json       # routes JSON v2
    code-intel dump routes --unmatched  # only unmatched routes and calls

Exit codes: 0 complete, 2 incomplete (diagnostics present), 1 refused or failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_intel import include_model as include_models
from code_intel import queries, route_matching, ts_bridge
from code_intel.backend_model import BackendModel, LiveRoute
from code_intel.cli import Context
from code_intel.code_paths import TS_SUFFIXES, SourceFile, is_test_path, iter_source_files, rel_posix
from code_intel.config import BackendConfig, Config, FrontendConfig
from code_intel.problems import (
    Problem,
    diagnostic,
    incomplete_banner,
    limitation,
    limitations_banner,
    partition,
    runtime_refusal,
)
from code_intel.repo_files import RepoFilesError, list_repo_files
from code_intel.result_types import (
    SCHEMA_VERSION,
    BackendRouteRow,
    FrontendCallRow,
    MatchBackendSide,
    MatchFrontendSide,
    MatchRow,
    RoutesResult,
    library_row,
    route_where,
)

STAGE = "frontend routes"
WEBSOCKET_MARKER = "/ws/"
MAX_URL_VARIANTS = 16
MAX_EVAL_DEPTH = 32

# Unresolved reasons about the URL value (unresolved_frontend_path); the rest
# are about the call's shape (unparsed_frontend_callsite).
_URL_REASONS = frozenset({"url_not_static", "url_not_path"})
# Helper `files_failed` steps -> diagnostic kind; an entry's own `kind`
# (parse_failed: the parser threw and the file was left out) wins.
_FAILED_STEP_KINDS = {
    "read file": "read_failed",
    "read tsconfig": "read_failed",
    "decode file": "decode_failed",
    "parse file": "typescript_syntax_error",
}
_FAILED_KINDS = frozenset({"parse_failed"})
_UNRESOLVED_TEXT = {
    "url_not_static": (
        "evaluate url",
        "the URL does not evaluate to a path (it starts with a value the analyzer cannot read)",
    ),
    "url_not_path": ("evaluate url", "the URL evaluates to text that does not start with /"),
    "client_reference_not_called": ("read call", "the client is referenced without being called"),
    "spread_arguments": ("read call", "the URL or init argument comes from a spread"),
    "options_not_object": ("read call", "the options argument is not an object literal with a url property"),
    "client_not_resolved": (
        "resolve client",
        "no declaration was found for it, so whether it is an API client is unknown",
    ),
}


def _unresolved_message(rel: str, item: dict, why: str, reason: str) -> str:
    where = f"{rel}:{item.get('line')}"
    text = item.get("text", "")
    if reason == "client_not_resolved":
        # Not a failure: the callee's name matches a client and nothing proves otherwise.
        return f"{where}: could not resolve callee {item.get('client')}: {why} ({reason}): {text}"
    return f"{where}: {item.get('client')} call not matched: {why} ({reason}): {text}"


# ── Frontend scan ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrontendScan:
    """The helper's ``routes`` response, with paths made repo-relative and every
    helper problem converted to a registered diagnostic or limitation."""

    calls: list[dict]  # RoutesCallJson with `file` repo-relative
    problems: list[Problem]
    files_scanned: int
    typescript_version: str | None
    timings_ms: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontendUnavailable:
    """The frontend could not be analyzed at all; the backend side is still usable."""

    message: str  # names the missing path or the failed operation
    missing: str | None
    fix: str
    problems: list[Problem] = field(default_factory=list)
    # The ts_bridge.ProblemCode behind it ("helper_failed" for a crashed helper),
    # or "frontend_not_found"; the diagnostic carries it as ``problem``.
    code: str | None = None

    def diagnostic(self) -> Problem:
        # The fix travels in its own field; renderers add it once.
        return diagnostic(
            "frontend_unavailable",
            stage=STAGE,
            step="analyze frontend",
            file=self.missing,
            message=self.message,
            missing=self.missing,
            fix=self.fix,
            problem=self.code,
        )

    @classmethod
    def from_problem(cls, problem: ts_bridge.ToolchainProblem, problems: list[Problem]) -> FrontendUnavailable:
        return cls(problem.message, problem.missing, problem.fix, problems, problem.code)


def routes_request(repo_root: Path, frontend: FrontendConfig, files: Sequence[SourceFile]) -> dict:
    """The ``routes`` helper request for these files (``protocol`` and the TS dir are added by ``run_helper``).

    URL builders are looked for in the modules of the configured clients: a
    client module exports the functions that name the paths its clients call.
    """
    builder_modules = dict.fromkeys(spec.module for spec in frontend.clients if spec.module is not None)
    return {
        "repo_root": str(repo_root),
        "frontend_root": str(repo_root / frontend.root),
        "tsconfig": str(repo_root / frontend.tsconfig),
        "files": [{"path": str(f.path), "is_test": is_test_path(f.rel)} for f in files],
        "clients": [spec.as_request() for spec in frontend.clients],
        "builder_modules": list(builder_modules),
        "websocket_marker": WEBSOCKET_MARKER,
        "limits": {"max_url_variants": MAX_URL_VARIANTS, "max_eval_depth": MAX_EVAL_DEPTH},
    }


def scan_frontend(
    repo_root: Path,
    frontend: FrontendConfig,
    toolchain: ts_bridge.Toolchain,
    files: Sequence[SourceFile] | None = None,
    *,
    run_helper: Callable[..., dict] = ts_bridge.run_helper,
) -> FrontendScan | FrontendUnavailable:
    """Run the ``routes`` helper over the frontend.

    ``files`` defaults to every git-visible ``TS_SUFFIXES`` file under the
    frontend root (``RepoFilesError`` when git cannot list the repository). A
    missing frontend root, an unavailable toolchain or a failed helper run
    returns ``FrontendUnavailable`` instead of raising, so callers can still
    report the backend.
    """
    frontend_root = repo_root / frontend.root
    missing_root = ts_bridge.frontend_not_found_problem(repo_root, frontend.root)
    if missing_root is not None:
        return FrontendUnavailable.from_problem(missing_root, [])
    walk_problems: list[Problem] = []
    if files is None:
        files = iter_source_files(
            frontend_root,
            TS_SUFFIXES,
            repo_root,
            walk_problems,
            stage=STAGE,
            generated_parent=frontend_root,
            visible=list_repo_files(repo_root).files,
        )
    if not toolchain.available:
        return FrontendUnavailable.from_problem(ts_bridge.incomplete_toolchain_problem(toolchain), walk_problems)
    try:
        response = run_helper(
            toolchain, "routes", routes_request(repo_root, frontend, files), timeout_s=ts_bridge.ROUTES_TIMEOUT_S
        )
    except ts_bridge.ToolchainUnavailable as exc:
        return FrontendUnavailable.from_problem(exc.problem, walk_problems)
    except ts_bridge.HelperError as exc:
        return FrontendUnavailable.from_problem(exc.problem(), walk_problems)
    return _scan_from_response(response, repo_root, len(files), walk_problems)


def _rel(path: str, repo_root: Path) -> str:
    return rel_posix(Path(path), repo_root)


def _relative_message(message: str, abs_path: str, rel: str) -> str:
    return message.replace(abs_path, rel) if abs_path else message


def _scan_from_response(response: dict, repo_root: Path, files_scanned: int, problems: list[Problem]) -> FrontendScan:
    problems = list(problems)
    for module in response.get("missing_client_modules", []):
        rel, _, export = module.partition("#")
        message = (
            f"{rel}: client {export} is not declared in the module, so its calls were not found"
            if export
            else f"{rel}: API client module not found, so calls to its clients were not found"
        )
        problems.append(
            diagnostic("frontend_client_module_missing", stage=STAGE, step="resolve client", file=rel, message=message)
        )
    for failed in response.get("files_failed", []):
        rel = _rel(failed["file"], repo_root)
        step = failed.get("step") or "extract calls"
        own = failed.get("kind")
        kind = own if own in _FAILED_KINDS else _FAILED_STEP_KINDS.get(step, "analysis_failed")
        message = _relative_message(failed.get("message", ""), failed["file"], rel)
        if kind == "typescript_syntax_error":
            message = f"{message}; results come from TypeScript's recovered tree"
        problems.append(diagnostic(kind, stage=STAGE, step=step, file=rel, message=message))
    for item in response.get("unresolved", []):
        rel = _rel(item["file"], repo_root)
        reason = item.get("reason", "url_not_static")
        step, why = _UNRESOLVED_TEXT.get(reason, ("read call", reason))
        kind = "unresolved_frontend_path" if reason in _URL_REASONS else "unparsed_frontend_callsite"
        problems.append(
            diagnostic(
                kind,
                stage=STAGE,
                step=step,
                file=rel,
                line=item.get("line"),
                message=_unresolved_message(rel, item, why, reason),
            )
        )
    for item in response.get("limitations", []):
        rel = _rel(item["file"], repo_root)
        problems.append(
            limitation(
                item["kind"],
                stage=STAGE,
                file=rel,
                line=item.get("line"),
                message=_relative_message(item.get("message", ""), item["file"], rel),
            )
        )
    calls = [{**call, "file": _rel(call["file"], repo_root)} for call in response.get("calls", [])]
    return FrontendScan(
        calls=calls,
        problems=problems,
        files_scanned=files_scanned,
        typescript_version=response.get("typescript_version"),
        timings_ms=dict(response.get("timings_ms") or {}),
    )


# ── Routes JSON v2 ───────────────────────────────────────────────────────


def _backend_row(route: LiveRoute) -> BackendRouteRow:
    return {
        "method": route.method,
        "path": route.path,
        "kind": route.kind,
        "handler": route.handler,
        "handler_id": route.handler_id,
        "file": route.file,
        "line": route.line,
        "mount": route.mount,
        "order": route.order,
        "library": library_row(route.library),
    }


def _backend_side(route: LiveRoute) -> MatchBackendSide:
    return {
        "method": route.method,
        "path": route.path,
        "handler": route.handler,
        "file": route.file,
        "line": route.line,
        "library": library_row(route.library),
    }


def _frontend_side(row: FrontendCallRow) -> MatchFrontendSide:
    return {
        "method": row["method"],
        "path": row["path"],
        "raw_path": row["raw_path"],
        "function": row["function"],
        "file": row["file"],
        "line": row["line"],
        "def_line": row["def_line"],
        "client": row["client"],
    }


_CallRow = tuple[FrontendCallRow, route_matching.FrontendUrl]


def frontend_rows(calls: Sequence[dict]) -> list[_CallRow]:
    """One row per call and distinct URL variant.

    Rows dedupe on ``(file, line, column, method, path)``: two calls on
    the same line of different files, or at different columns of one line,
    stay separate; two variants of one call that prepare to the same path
    (``/squads?{q}`` and ``/squads``) collapse.
    """
    out: list[_CallRow] = []
    seen: set[tuple[str, int, int, str, str]] = set()
    for call in calls:
        kind = "websocket" if call["kind"] == "websocket" else "http"
        for variant in call.get("urls", []):
            url = route_matching.prepare_url(variant["parts"], call["method"], kind, variant.get("confidence", "exact"))
            path = route_matching.display_path(url)
            key = (call["file"], call["line"], call["column"], call["method"], path)
            if key in seen:
                continue
            seen.add(key)
            row: FrontendCallRow = {
                "method": call["method"],
                "path": path,
                "raw_path": call.get("raw", ""),
                "kind": call["kind"],
                "client": call.get("client"),
                "builder": call.get("builder"),
                "function": call.get("function"),
                "file": call["file"],
                "line": call["line"],
                "def_line": call.get("def_line"),
                "column": call["column"],
                "confidence": variant.get("confidence", "exact"),
                "origin": variant.get("origin", "literal"),
                "is_test": bool(call.get("is_test")),
                "callee": call.get("callee"),
                "callee_line": call.get("callee_line"),
                "callee_column": call.get("callee_column"),
            }
            out.append((row, url))
    return out


@dataclass
class _Matched:
    matches: list[tuple[int, MatchRow]] = field(default_factory=list)  # (route order, row)
    orders: set[int] = field(default_factory=set)  # routes with a caller
    calls: set[tuple[str, int, int]] = field(default_factory=set)  # (file, line, column) with a route
    frontend_only: list[MatchFrontendSide] = field(default_factory=list)


def _match_rows(compiled: route_matching.CompiledRoutes, rows: list[_CallRow]) -> _Matched:
    """Match every frontend row; match rows come out in route order."""
    out = _Matched()
    for row, url in rows:
        found = compiled.match(url)
        if not found:
            out.frontend_only.append(_frontend_side(row))
            continue
        out.calls.add((row["file"], row["line"], row["column"]))
        for m in found:
            out.orders.add(m.route.order)
            match: MatchRow = {
                "backend": _backend_side(m.route),
                "frontend": _frontend_side(row),
                "confidence": m.confidence,
                "reason": m.reason,
            }
            out.matches.append((m.route.order, match))
    out.matches.sort(key=lambda item: (item[0], item[1]["frontend"]["file"], item[1]["frontend"]["line"]))
    return out


def _product_rows(calls: list[dict]) -> list[_CallRow]:
    """One (row, URL) per call and URL variant outside test files, by position."""
    rows = [(row, url) for row, url in frontend_rows(calls) if not row["is_test"]]
    rows.sort(key=lambda item: (item[0]["file"], item[0]["line"], item[0]["column"], item[0]["path"]))
    return rows


def _coverage(matched_routes: int, backend_routes: int) -> float:
    return round(matched_routes / backend_routes * 100, 1) if backend_routes else 0.0


def build_routes(model: BackendModel, scan: FrontendScan | FrontendUnavailable) -> RoutesResult:
    """Routes JSON v2 from the backend model and a frontend scan.

    Test-file calls are counted in ``summary.test_calls_skipped`` and left out
    of ``frontend_calls`` and matching. When the frontend is unavailable the
    backend routes are still returned, every route is backend-only, and a
    ``frontend_unavailable`` diagnostic marks the result incomplete.
    """
    problems: list[Problem] = [*model.diagnostics, *model.limitations, *scan.problems]
    if isinstance(scan, FrontendUnavailable):
        problems.append(scan.diagnostic())
        calls: list[dict] = []
    else:
        calls = scan.calls
    test_calls = {(c["file"], c["line"], c["column"]) for c in calls if c.get("is_test")}
    rows = _product_rows(calls)
    compiled = route_matching.compile_backend(model.routes, redirect_slashes=model.redirect_slashes)
    matched = _match_rows(compiled, rows)

    found_diagnostics, found_limitations = partition(problems)
    diagnostics = [p.as_diagnostic() for p in found_diagnostics]
    limitations = [p.as_limitation() for p in found_limitations]
    complete = not diagnostics
    ordered = sorted(model.routes, key=lambda r: r.order)
    repository = [r for r in ordered if r.library is None]
    installed = [r for r in ordered if r.library is not None]
    matched_installed = {r.order for r in installed} & matched.orders
    matched_repository = matched.orders - matched_installed
    backend_routes = [_backend_row(r) for r in repository]
    backend_only = [_backend_side(r) for r in repository if r.order not in matched.orders]
    call_ids = {(row["file"], row["line"], row["column"]) for row, _ in rows}
    coverage = _coverage(len(matched_repository), len(backend_routes)) if complete else None
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": complete,
        "app": include_models.app_block(model),
        "diagnostics": diagnostics,
        "analysis_limitations": limitations,
        "backend_routes": backend_routes,
        "library_routes": [_backend_row(r) for r in installed],
        "frontend_calls": [row for row, _ in rows],
        "matches": [m for _, m in matched.matches],
        "backend_only": backend_only,
        "frontend_only": matched.frontend_only,
        "unmounted_routes": [_backend_row(r) for r in model.unmounted_routes],
        "summary": {
            "backend_routes": len(backend_routes),
            "library_routes": len(installed),
            "matched_library_routes": len(matched_installed),
            "frontend_calls": len(call_ids),
            "matched_routes": len(matched_repository),
            "matched_calls": len(matched.calls),
            "match_rows": len(matched.matches),
            "backend_only": len(backend_only),
            "frontend_only": len(matched.frontend_only),
            "test_calls_skipped": len(test_calls),
            "coverage_pct": coverage,
        },
    }


def build_backend_model(repo_root: Path, backend: BackendConfig) -> BackendModel:
    """The live backend model for the configured app, from the git-visible
    files. Raises ``RepoFilesError`` when git cannot list them."""
    from code_intel import router_graph  # deferred: the CLI is the only caller that builds its own model

    return router_graph.build_backend_model(
        repo_root / backend.root,
        backend.package,
        backend.app,
        repo_root,
        lock=backend.lock,
        library=backend.capture.file if backend.capture is not None else None,
        visible=list_repo_files(repo_root).files,
    )


def compute_routes(
    repo_root: Path,
    config: Config,
    *,
    toolchain: ts_bridge.Toolchain | None = None,
    model_builder: Callable[[Path, BackendConfig], BackendModel] | None = None,
) -> RoutesResult:
    """Build the backend model and scan the frontend concurrently, then match.

    The helper is a separate node process, so the scan overlaps the backend
    build instead of adding to it.
    """
    toolchain = toolchain or ts_bridge.find_toolchain(repo_root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        scan_future = pool.submit(scan_frontend, repo_root, config.frontend, toolchain)
        model = (model_builder or build_backend_model)(repo_root, config.backend)
        scan = scan_future.result()
    return build_routes(model, scan)


# ── Text output ──────────────────────────────────────────────────────────


def _fmt_front(side: MatchFrontendSide) -> str:
    fn = side["function"] or "(module)"
    defined = f"def {side['def_line']} · " if side["def_line"] else ""
    return f"{fn}()  [{side['file']}:{defined}call {side['line']}]"


def print_report(result: RoutesResult, *, unmatched_only: bool = False, out: Any = None) -> None:
    """Human-readable route map. An incomplete result leads with its banner."""
    out = out or sys.stdout

    def w(text: str = "") -> None:
        print(text, file=out)

    if result["diagnostics"]:
        w(incomplete_banner(result["diagnostics"]))
    if result["analysis_limitations"]:
        w(limitations_banner(result["analysis_limitations"]))
    if not unmatched_only:
        w(f"\nMatched ({result['summary']['match_rows']} rows)")
        for m in result["matches"]:
            b, f = m["backend"], m["frontend"]
            tag = "" if m["confidence"] == "exact" else f"  (possible: {m['reason']})"
            w(f"  {b['method']:<7} {b['path']}  ←  {_fmt_front(f)}{tag}")
            w(f"          handler {b['handler']}()  [{route_where(b)}]")
    w(f"\nBackend only ({len(result['backend_only'])})")
    for r in result["backend_only"]:
        w(f"  {r['method']:<7} {r['path']}  {r['handler']}()  [{r['file']}:{r['line']}]")
    w(f"\nFrontend only ({len(result['frontend_only'])})")
    for c in result["frontend_only"]:
        w(f"  {c['method']:<7} {c['path']}  {_fmt_front(c)}")
    s = result["summary"]
    w("\nSummary")
    w(f"  Backend routes:  {s['backend_routes']} ({s['matched_routes']} matched, {s['backend_only']} backend-only)")
    if s["library_routes"]:
        w(f"  Library routes:  {s['library_routes']} ({s['matched_library_routes']} matched)")
    w(
        f"  Frontend calls:  {s['frontend_calls']} ({s['matched_calls']} matched, {s['frontend_only']} unmatched rows, "
        f"{s['test_calls_skipped']} test-file calls skipped)"
    )
    coverage = "n/a (incomplete)" if s["coverage_pct"] is None else f"{s['coverage_pct']}% of backend routes"
    w(f"  Coverage:        {coverage}")
    bounds = queries.orphan_gaps(result).summary_bounds(s)
    loose = [f"{key} {queries.BOUND_WORDS[bound]}" for key, bound in bounds.items() if bound in queries.BOUND_WORDS]
    if loose:
        w(f"  Bounds:          {', '.join(loose)} (the findings above can move them)")


# ── Entry point ──────────────────────────────────────────────────────────


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print routes JSON v2")
    parser.add_argument("--unmatched", action="store_true", help="text output: only unmatched routes and calls")


def run(args: argparse.Namespace, context: Context) -> int:
    repo_root, backend = context.repo_root, context.config.backend
    if (refusal := runtime_refusal(repo_root, backend.python_version_file)) is not None:
        print(refusal, file=sys.stderr)
        return 1
    try:
        result = compute_routes(repo_root, context.config)
    except RepoFilesError as exc:
        print(f"code-intel: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result, unmatched_only=args.unmatched)
    if result["diagnostics"]:
        # stdout stays machine-readable; each lost piece is named on stderr.
        print(incomplete_banner(result["diagnostics"]), file=sys.stderr)
        for d in result["diagnostics"]:
            fix = f" Fix: {d['fix']}" if d.get("fix") else ""
            print(f"  [{d['kind']}] {d['message']}{fix}", file=sys.stderr)
        return 2
    return 0
