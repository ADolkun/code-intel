"""``code-intel verify``: the analyzers checked against each other and against the source.

Each check asserts a relation between analyzers, or between an analyzer and the
source text, never a count or a name copied from the code, so renames, aliases
and new routes keep it green. A code shape the analyzers do not model yet can
still fail a check, which is what the command is for.

Checks:

- the backend model, the routes scan and the docs report lose no content;
- every live route's ``file:line`` holds its handler's ``def`` and its id
  resolves there;
- the dependency map and the routes scan list the same routes;
- the JSX elements and TypeScript declarations equal the AST oracles
  (``oracles/jsx_oracle.cjs``, ``oracles/decl_oracle.cjs``);
- every module-level Python function and class is in the symbol index;
- every frontend call row points at a call of its kind;
- every matched backend row is a live route;
- the docs report is self-consistent: every reference sits on its line, every
  failing reference has a specific message, every ``ok`` route reference is a
  live route, the symbol index agrees with the code words, and the lookups
  agree with the report.

Output is one line per check; a failing check lists up to 20 offending rows.

Exit codes: 0 every check passed or was skipped, 1 a check failed, 2 a check
could not run (an old interpreter, a missing frontend toolchain, an analyzer
error) and none failed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from code_intel import code_paths, problems
from code_intel.analysis_runtime import AnalysisFailed, AnalysisRuntime, FrontendFailed, InvalidRoot
from code_intel.config import Config, ConfigFile
from code_intel.oracles import compare_facts
from code_intel.ts_bridge import HelperError, Toolchain, ToolchainUnavailable, incomplete_toolchain_problem

if TYPE_CHECKING:
    from code_intel.cli import Context

ORACLES_DIR = Path(compare_facts.__file__).resolve().parent
ORACLE_TIMEOUT_S = 300
SHOWN = 20
WRAP_LINES = 8

Status = Literal["pass", "fail", "error", "skipped"]


class CannotRun(Exception):
    """A check's prerequisite is missing; the message says what and how to fix it."""


@dataclass(frozen=True)
class Outcome:
    name: str
    status: Status
    detail: str
    rows: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    """What a check found: a one-line ``detail`` and the rows that violate it."""

    detail: str
    rows: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Skip:
    reason: str


# ── Relations over plain data ────────────────────────────────────────────
#
# Each function takes rows (the analyzers' JSON, or small readers of the
# source) and returns the rows that break the relation, so a test can break
# each one on its own.


def lost_content(diagnostics: Iterable[Mapping[str, Any]]) -> list[str]:
    return [
        f"{d.get('file') or '-'}: [{d['kind']}] {d['message']}"
        for d in diagnostics
        if d.get("category") == "content_lost"
    ]


def unregistered_limitations(kinds: Iterable[str]) -> list[str]:
    return sorted({kind for kind in kinds if kind not in problems.LIMITATION_KINDS})


# A lambda endpoint's id is `module:<lambda>@L<line>` (possibly nested in a qualname).
_LAMBDA_QUALNAME = re.compile(r"(?:^|\.)<lambda>@L(\d+)$")

Located = tuple[str, ast.AST]


def find_definition(backend_dir: Path, module: str, qualname: str, repo_root: Path) -> Located | None:
    """``(file, node)`` for ``module:qualname`` under ``backend_dir``; ``<locals>``
    steps into a function body and ``Class()`` (an instance) names the class."""
    base = backend_dir / Path(*module.split("."))
    path = base / "__init__.py" if base.is_dir() else base.with_suffix(".py")
    if not path.is_file():
        return None
    try:
        body: list[ast.stmt] = ast.parse(path.read_bytes()).body
    except OSError, SyntaxError, ValueError:
        return None
    found: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None = None
    for part in qualname.split("."):
        if part == "<locals>":
            continue
        name = part.removesuffix("()")
        found = next(
            (
                n
                for n in body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and n.name == name
            ),
            None,
        )
        if found is None:
            return None
        body = found.body
    return None if found is None else (code_paths.rel_posix(path, repo_root), found)


def handler_location_problems(
    routes: Iterable[Mapping[str, Any]],
    read_line: Callable[[str, int], str],
    definition: Callable[[str, str], Located | None],
) -> list[str]:
    """A named handler's line holds its ``def`` and its id resolves there; a
    lambda endpoint has no ``def``, so its line is the one its id names and
    holds the ``lambda``."""
    wrong: list[str] = []
    for route in routes:
        where = f"{route['method']} {route['path']}"
        line = read_line(route["file"], route["line"])
        module, _, qualname = route["handler_id"].partition(":")
        lambda_id = _LAMBDA_QUALNAME.search(qualname)
        if lambda_id is not None:
            if int(lambda_id.group(1)) != route["line"] or not re.search(r"\blambda\b", line):
                wrong.append(f"{where}: lambda {route['handler_id']} at {route['file']}:{route['line']}")
            continue
        if not re.match(rf"\s*(?:async\s+)?def\s+{re.escape(route['handler'])}\b", line):
            wrong.append(f"{where}: {route['file']}:{route['line']} is {line.strip()!r}")
            continue
        resolved = definition(module, qualname)
        if resolved is None or resolved[0] != route["file"] or getattr(resolved[1], "lineno", None) != route["line"]:
            wrong.append(
                f"{where}: handler_id {route['handler_id']} does not resolve to {route['file']}:{route['line']}"
            )
    return wrong


def route_set_differences(
    di_routes: Iterable[Mapping[str, Any]], scan_routes: Iterable[Mapping[str, Any]]
) -> list[str]:
    di = {(r["method"], r["path"], r["handler_id"]) for r in di_routes}
    scan = {(r["method"], r["path"], r["handler_id"]) for r in scan_routes}
    return [f"only in the dependency map: {' '.join(key)}" for key in sorted(di - scan)] + [
        f"only in the routes scan: {' '.join(key)}" for key in sorted(scan - di)
    ]


def stray_matched_backends(routes: Mapping[str, Any]) -> list[str]:
    """Matched backend rows that are not among the live backend routes."""

    def key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (row["method"], row["path"], row["handler"], row["file"], row["line"])

    live = {key(r) for r in routes["backend_routes"]}
    return [
        f"{m['backend']['method']} {m['backend']['path']} -> {m['backend']['file']}:{m['backend']['line']}"
        for m in routes["matches"]
        if key(m["backend"]) not in live
    ]


def unindexed_definitions(
    indexed: Iterable[tuple[str, str, int]], expected: Iterable[tuple[str, str, int]]
) -> list[str]:
    return [f"{file}:{line}: {name}" for file, name, line in sorted(set(expected) - set(indexed))]


# A WebSocket row points at the start of its URL expression: a string or
# template literal, a name or call (`getWsBaseUrl() + "/ws/…"`), or a parenthesis.
_EXPRESSION_START = re.compile(r"[`'\"(A-Za-z_$]")
_CLIENT_KINDS = frozenset({"call", "options", "instance", "global"})


def call_site_problem(call: Mapping[str, Any], read_line: Callable[[str, int], str]) -> str | None:
    """Why ``call`` does not point at a call of its kind, or None.

    Positions only, never names: an aliased import is still a client call.
    A client or builder row carries the callee as written and the line and
    column it starts at, which may be a later line than the call's own
    (``api\\n  .get(…)``), so the source must hold that text there.
    """
    kind = call["kind"]
    if kind in _CLIENT_KINDS and not call["client"]:
        return f"{kind} row without a client"
    if kind == "builder" and not call["builder"]:
        return "builder row without a builder"
    if kind == "websocket":
        if call["client"] or call["builder"]:
            return "websocket row attributed to a client or builder"
        text = read_line(call["file"], call["line"])
        column = call["column"]
        if not 1 <= column <= len(text):
            return f"column {column} is outside the line ({len(text)} characters)"
        if not _EXPRESSION_START.match(text, column - 1):
            return f"column {column} does not start a URL expression: {text.strip()!r}"
        return None
    callee, line, column = call.get("callee"), call.get("callee_line"), call.get("callee_column")
    if not callee or line is None or column is None:
        return f"{kind} row without callee/callee_line/callee_column"
    if line < call["line"]:
        return f"callee_line {line} is before the call's line {call['line']}"
    text = read_line(call["file"], line)
    if not 1 <= column <= len(text):
        return f"callee_column {column} is outside line {line} ({len(text)} characters)"
    if not text.startswith(callee, column - 1):
        return f"callee {callee!r} is not at {line}:{column}: {text.strip()!r}"
    return None


def call_site_problems(calls: Iterable[Mapping[str, Any]], read_line: Callable[[str, int], str]) -> list[str]:
    return [
        f"{call['file']}:{call['line']}:{call['column']} ({call['kind']}): {found}"
        for call in calls
        if (found := call_site_problem(call, read_line)) is not None
    ]


def _between_backticks(text: str, line: str) -> bool:
    """``text`` sits between two backticks of ``line``. Any two neighbours
    count, not CommonMark's pairing: a span may open on the line before, and
    an escaped backtick outside a span shifts the pairs."""
    return any(text in part for part in line.split("`")[1:-1])


def _starts_on_line(text: str, lines: Sequence[str], line: int) -> bool:
    """``text`` starts on 1-based ``line``, where any whitespace in it may be a
    line break (a code span or link may wrap)."""
    if text in lines[line - 1]:
        return True
    words = text.split()
    if len(words) < 2:
        return False
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words))
    found = pattern.search("\n".join(lines[line - 1 : line - 1 + WRAP_LINES]))
    return found is not None and found.start() < len(lines[line - 1])


def reference_on_its_line(ref: Any, lines: Sequence[str]) -> bool:
    """The reference's text, as written, starts on its reported line. Symbols,
    and paths outside a fence, are read only from code spans, so their text
    must sit between backticks there: the same word in prose would pass a
    wrong line. A link's text is the decoded href, so the line may hold it
    percent-encoded."""
    line = lines[ref.line - 1]
    if ref.kind == "symbol" or (ref.kind == "path" and ref.context != "fence"):
        return _between_backticks(ref.text, line)
    return _starts_on_line(ref.text, lines, ref.line) or (
        ref.kind == "link" and quote(ref.text, safe="/#?&=:.-_~%") in line
    )


def misplaced_references(docs: Mapping[str, Any], read_lines: Callable[[str], Sequence[str]]) -> list[str]:
    out: list[str] = []
    for doc, entry in docs.items():
        lines = read_lines(doc)
        out.extend(
            f"{doc}:{ref.line}: {ref.kind} {ref.text!r}"
            for ref in entry.references
            if not 1 <= ref.line <= len(lines) or not reference_on_its_line(ref, lines)
        )
    return out


def generic_failures(references: Iterable[Any], message: Callable[[Any], str]) -> list[str]:
    """Failing references whose finding says no more than ``<kind> <status>``."""
    return [
        f"{ref.doc}:{ref.line}: {ref.kind} {ref.status}"
        for ref in references
        if ref.broken and (not ref.detail or message(ref).startswith(f"{ref.kind} {ref.status}"))
    ]


def stray_route_references(di_routes: Iterable[Mapping[str, Any]], references: Iterable[Any]) -> list[str]:
    """``ok`` route references whose handler locations are not a live route's,
    or whose target is not one of the routes defined there."""
    by_location: dict[str, set[str]] = {}
    for r in di_routes:
        by_location.setdefault(f"{r['file']}:{r['line']}", set()).add(f"{r['method']} {r['path']}")

    def live_at(ref: Any) -> bool:
        if not ref.defined_in or any(where not in by_location for where in ref.defined_in):
            return False
        return ref.target in set().union(*(by_location[where] for where in ref.defined_in))

    return [
        f"{ref.doc}:{ref.line}: {ref.text} -> {ref.target} {list(ref.defined_in)}"
        for ref in references
        if ref.kind == "route" and ref.status == "ok" and not live_at(ref)
    ]


def lookup_disagreements(report: Any) -> list[str]:
    """The lookups answer from the report: a checked ``ok`` route and symbol
    mention is found by its own query, and a doc's inbound links include the
    docs that link to it."""
    from code_intel import doc_queries

    out: list[str] = []
    refs = sorted(report.references(), key=lambda r: (r.doc, r.line, r.text))

    route = next((r for r in refs if r.kind == "route" and r.status == "ok" and r.defined_in), None)
    if route is not None and route.target is not None:
        found = doc_queries.doc_refs_lookup(report, route=route.target, limit=None)
        mentions = {(m["file"], m["line"], m["text"]) for m in found.mentions.items} if found.mentions else set()
        if not found.definitions or not found.definitions.items:
            out.append(f"route {route.target}: the lookup finds no live route")
        if (route.doc, route.line, route.text) not in mentions:
            out.append(f"route {route.target}: the lookup misses the mention at {route.doc}:{route.line}")

    symbol = next((r for r in refs if r.kind == "symbol" and r.checked and r.status == "ok" and r.defined_in), None)
    if symbol is not None and symbol.target is not None:
        found = doc_queries.doc_refs_lookup(report, symbol=symbol.target, limit=None)
        defined = {f"{d['file']}:{d['line']}" for d in found.definitions.items} if found.definitions else set()
        mentions = {(m["file"], m["line"], m["text"]) for m in found.mentions.items} if found.mentions else set()
        if not set(symbol.defined_in) <= defined:
            out.append(f"symbol {symbol.target}: the lookup misses a definition of {list(symbol.defined_in)}")
        if (symbol.doc, symbol.line, symbol.text) not in mentions:
            out.append(f"symbol {symbol.target}: the lookup misses the mention at {symbol.doc}:{symbol.line}")

    for target, linkers in sorted(report.inbound.items()):
        if target not in report.docs or not linkers:
            continue
        found = doc_queries.doc_refs_lookup(report, doc=target, limit=None)
        inbound = {row["file"] for row in found.inbound.items} if found.inbound else set()
        if not {doc for doc, _ in linkers} <= inbound:
            out.append(
                f"doc {target}: the lookup misses inbound links from {sorted({d for d, _ in linkers} - inbound)}"
            )
        break
    return out


# ── The repository under check ───────────────────────────────────────────


class Subject:
    """The analyzers' results for one checkout, each computed once."""

    def __init__(self, runtime: AnalysisRuntime) -> None:
        self.runtime = runtime
        self.repo_root = runtime.repo_root

    @cached_property
    def config(self) -> Config:
        return self.runtime.config("verify")

    @cached_property
    def backend_dir(self) -> Path:
        return self.repo_root / self.config.backend.root

    @cached_property
    def visible(self) -> frozenset[str]:
        return frozenset(self.runtime.analyzers.list_repo_files(self.repo_root).files)

    @cached_property
    def model(self) -> Any:
        return self.runtime.get_backend_model()

    @cached_property
    def di(self) -> dict:
        return self.runtime.get_di()

    @cached_property
    def toolchain(self) -> Toolchain:
        toolchain = self.runtime.toolchain()
        if not toolchain.available:
            problem = incomplete_toolchain_problem(toolchain)
            raise CannotRun(f"the frontend toolchain is unavailable: {problem.message}. Fix: {problem.fix}")
        return toolchain

    @cached_property
    def routes(self) -> dict:
        _ = self.toolchain
        result = self.runtime.get_routes()
        missing = self.runtime.routes_frontend_problem()
        if missing is not None:
            raise CannotRun(f"the frontend was not analyzed: {missing.message}. Fix: {missing.fix}")
        return result

    @cached_property
    def report(self) -> Any:
        if self.config.docs is None:
            raise CannotRun("the config has no [docs] table")
        return self.runtime.get_doc_report().report

    def read_line(self, rel: str, line: int) -> str:
        return self.read_lines(rel)[line - 1]

    def read_lines(self, rel: str) -> list[str]:
        return code_paths.split_source_lines((self.repo_root / rel).read_text(encoding="utf-8-sig"))


# ── The checks ───────────────────────────────────────────────────────────


def check_backend_content(s: Subject) -> Verdict:
    lost = lost_content(p.as_dict() for p in s.model.diagnostics)
    unknown = unregistered_limitations(p.kind for p in s.model.limitations)
    rows = lost + [f"limitation kind not registered: {kind}" for kind in unknown]
    return Verdict(f"{len(s.model.routes)} routes, {len(s.model.limitations)} limitations", rows)


def check_handler_locations(s: Subject) -> Verdict:
    if not s.model.routes:
        return Verdict("the app has no live routes", ["no live routes: is backend.app right?"])
    rows = [
        {
            "method": r.method,
            "path": r.path,
            "handler": r.handler,
            "handler_id": r.handler_id,
            "file": r.file,
            "line": r.line,
        }
        for r in s.model.routes
    ]
    wrong = handler_location_problems(
        rows,
        s.read_line,
        lambda module, qualname: find_definition(s.backend_dir, module, qualname, s.repo_root),
    )
    return Verdict(f"{len(rows)} routes", wrong)


def check_di_matches_routes(s: Subject) -> Verdict:
    return Verdict(
        f"{len(s.di['routes'])} routes in both", route_set_differences(s.di["routes"], s.routes["backend_routes"])
    )


def check_routes_content(s: Subject) -> Verdict:
    return Verdict(
        f"{len(s.routes['frontend_calls'])} frontend calls",
        lost_content(s.routes["diagnostics"]),
    )


def check_call_sites(s: Subject) -> Verdict:
    calls = s.routes["frontend_calls"]
    return Verdict(f"{len(calls)} frontend calls", call_site_problems(calls, s.read_line))


def check_matched_backends_are_live(s: Subject) -> Verdict:
    return Verdict(f"{len(s.routes['matches'])} matches", stray_matched_backends(s.routes))


def run_oracle(args: Sequence[str]) -> None:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", timeout=ORACLE_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CannotRun(f"{Path(args[1]).name} could not run ({type(exc).__name__}: {exc})") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip()[-1500:]
        raise CannotRun(f"{Path(args[1]).name} exited with code {proc.returncode}: {tail}")


def _oracle_rows(path: Path, key: str) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))[key]


def comparison_rows(result: dict) -> list[str]:
    rows = [f"only in the oracle: {k}" for k in result["missing"]]
    rows += [f"only in the tool: {k}" for k in result["extra"]]
    for field_name, items in result["mismatches"].items():
        rows += [f"{key}: {field_name} oracle {want!r}, tool {got!r}" for key, want, got in items]
    return rows


def check_jsx_oracle(s: Subject) -> Verdict:
    from code_intel import jsx_usage

    toolchain = s.toolchain
    frontend_root = s.config.frontend.root
    found: list[problems.Problem] = []
    facts = s.runtime.facts_cache()
    try:
        tool_rows = jsx_usage.all_jsx_elements(
            Path(frontend_root),
            facts=facts,
            repo_root=s.repo_root,
            frontend_root=frontend_root,
            include_tests=True,
            problems=found,
            visible=s.visible,
        )
    except (ToolchainUnavailable, HelperError, jsx_usage.JsxRootError) as exc:
        raise CannotRun(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="code-intel-verify-") as tmp:
        out = Path(tmp) / "jsx.json"
        run_oracle(
            [
                str(toolchain.node),
                str(ORACLES_DIR / "jsx_oracle.cjs"),
                str(toolchain.typescript_dir),
                str(s.repo_root),
                frontend_root,
                str(s.repo_root / frontend_root),
                str(out),
            ]
        )
        oracle_rows = [compare_facts.jsx_row(r) for r in _oracle_rows(out, "elements") if r["file"] in s.visible]
    tool = [
        {
            "file": r.file,
            "tag": r.tag,
            "line": r.line,
            "column": r.column,
            "kind": r.kind,
            "parent": r.parent,
            "is_test": r.is_test,
        }
        for r in tool_rows
    ]
    result = compare_facts.compare(oracle_rows, tool, ("file", "line", "column", "tag"), compare_facts.JSX_FIELDS)
    return Verdict(f"{result['tool']} elements", comparison_rows(result))


def check_declaration_oracle(s: Subject) -> Verdict:
    toolchain = s.toolchain
    frontend_root = s.config.frontend.root
    found: list[problems.Problem] = []
    try:
        decls = s.runtime.symbol_index().typescript_decls(found)
    except (ToolchainUnavailable, HelperError) as exc:
        raise CannotRun(str(exc)) from exc
    tool = [{"file": rel, **vars(decl), "is_test": code_paths.is_test_path(rel)} for rel, decl in decls]
    with tempfile.TemporaryDirectory(prefix="code-intel-verify-") as tmp:
        out = Path(tmp) / "decls.json"
        run_oracle(
            [
                str(toolchain.node),
                str(ORACLES_DIR / "decl_oracle.cjs"),
                str(toolchain.typescript_dir),
                str(s.repo_root / frontend_root),
                str(out),
                str(s.repo_root),
            ]
        )
        oracle_rows = [compare_facts.decl_row(r) for r in _oracle_rows(out, "decls") if r["file"] in s.visible]
    result = compare_facts.compare(
        oracle_rows,
        [compare_facts.decl_row(r) for r in tool],
        ("file", "line", "column", "name"),
        compare_facts.DECL_FIELDS,
    )
    return Verdict(f"{result['tool']} declarations", comparison_rows(result))


def check_python_index(s: Subject) -> Verdict:
    found: list[problems.Problem] = []
    indexed = {(rel, symbol.name, symbol.line) for rel, symbol in s.runtime.symbol_index().python_symbols(found)}
    lost = lost_content(p.as_dict() for p in found)
    expected: set[tuple[str, str, int]] = set()
    walk: list[problems.Problem] = []
    for source in code_paths.iter_source_files(
        s.backend_dir, code_paths.PY_SUFFIXES, s.repo_root, walk, visible=s.visible
    ):
        text, problem = code_paths.read_python_source(source.path, source.rel)
        if problem is not None or text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        expected.update(
            (source.rel, node.name, node.lineno)
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        )
    if not expected:
        return Verdict("the backend has no module-level definitions", ["no module-level definitions found"])
    return Verdict(f"{len(expected)} module-level definitions", lost + unindexed_definitions(indexed, expected))


def check_docs_content(s: Subject) -> Verdict:
    report = s.report
    return Verdict(f"{len(report.docs)} docs", lost_content(p.as_dict() for p in report.diagnostics))


def check_docs_reference_lines(s: Subject) -> Verdict:
    report = s.report
    return Verdict(
        f"{sum(len(e.references) for e in report.docs.values())} references",
        misplaced_references(report.docs, s.read_lines),
    )


def check_docs_failure_messages(s: Subject) -> Verdict:
    from code_intel import doc_refs

    report = s.report
    broken = [r for r in report.references() if r.broken]
    return Verdict(
        f"{len(broken)} failing references", generic_failures(report.references(), doc_refs.reference_message)
    )


def check_docs_routes_are_live(s: Subject) -> Verdict:
    report = s.report
    refs = [r for r in report.references() if r.kind == "route" and r.status == "ok"]
    return Verdict(f"{len(refs)} ok route references", stray_route_references(s.di["routes"], report.references()))


def absent_code_words(symbols: Iterable[Any], read: Callable[[str], bytes]) -> list[str]:
    """Python definitions the symbol index holds that are not identifier words of their file."""
    from code_intel import code_words

    by_file: dict[str, set[str]] = {}
    for row in symbols:
        if row.language == "python":
            by_file.setdefault(row.file, set()).add(row.name)
    absent: list[str] = []
    for rel, names in sorted(by_file.items()):
        words, found = code_words.code_words_from_bytes(rel, read(rel))
        if found:
            absent.extend(f"{rel}: [{p.kind}] {p.message}" for p in found)
            continue
        absent.extend(f"{rel}: {name}" for name in sorted(names - words.words))
    return absent


def check_docs_code_words(s: Subject) -> Verdict:
    """The symbol check's two sources agree: a definition the index holds in a
    tracked Python file is also an identifier word of that file."""
    files = {row.file for row in s.report.symbols if row.language == "python"}
    return Verdict(
        f"{len(files)} files with Python symbols",
        absent_code_words(s.report.symbols, lambda rel: (s.repo_root / rel).read_bytes()),
    )


def check_docs_lookups(s: Subject) -> Verdict:
    return Verdict("route, symbol and doc lookups", lookup_disagreements(s.report))


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[Subject], Verdict]
    needs_docs: bool = False


CHECKS: tuple[Check, ...] = (
    Check("backend-content", check_backend_content),
    Check("handler-locations", check_handler_locations),
    Check("python-index", check_python_index),
    Check("routes-content", check_routes_content),
    Check("di-matches-routes", check_di_matches_routes),
    Check("call-sites", check_call_sites),
    Check("matched-backends-are-live", check_matched_backends_are_live),
    Check("jsx-oracle", check_jsx_oracle),
    Check("declaration-oracle", check_declaration_oracle),
    Check("docs-content", check_docs_content, needs_docs=True),
    Check("docs-reference-lines", check_docs_reference_lines, needs_docs=True),
    Check("docs-failure-messages", check_docs_failure_messages, needs_docs=True),
    Check("docs-routes-are-live", check_docs_routes_are_live, needs_docs=True),
    Check("docs-code-words", check_docs_code_words, needs_docs=True),
    Check("docs-lookups", check_docs_lookups, needs_docs=True),
)


def run_check(check: Check, subject: Subject) -> Outcome:
    try:
        if check.needs_docs and subject.config.docs is None:
            return Outcome(check.name, "skipped", "the config has no [docs] table")
        verdict = check.run(subject)
    except CannotRun as exc:
        return Outcome(check.name, "error", str(exc))
    except FrontendFailed as exc:
        return Outcome(check.name, "error", f"{exc.problem.message}. Fix: {exc.problem.fix}")
    except AnalysisFailed as exc:
        return Outcome(check.name, "error", exc.message)
    except (InvalidRoot, OSError, UnicodeDecodeError) as exc:
        return Outcome(check.name, "error", f"{type(exc).__name__}: {exc}")
    return Outcome(check.name, "fail" if verdict.rows else "pass", verdict.detail, tuple(verdict.rows))


def verify(runtime: AnalysisRuntime, checks: Sequence[Check] = CHECKS) -> list[Outcome]:
    subject = Subject(runtime)
    return [run_check(check, subject) for check in checks]


def exit_code(outcomes: Iterable[Outcome]) -> int:
    statuses = {o.status for o in outcomes}
    return 1 if "fail" in statuses else 2 if "error" in statuses else 0


def render(outcomes: Sequence[Outcome]) -> str:
    width = max((len(o.name) for o in outcomes), default=0)
    lines: list[str] = []
    for outcome in outcomes:
        lines.append(f"{outcome.status.upper():<7} {outcome.name:<{width}}  {outcome.detail.splitlines()[0]}")
        lines.extend(f"          {row}" for row in outcome.rows[:SHOWN])
        if len(outcome.rows) > SHOWN:
            lines.append(f"          ... {len(outcome.rows) - SHOWN} more")
        lines.extend(f"          {extra}" for extra in outcome.detail.splitlines()[1:])
    counts = {status: sum(1 for o in outcomes if o.status == status) for status in ("pass", "fail", "error", "skipped")}
    lines.append(
        f"verify: {len(outcomes)} checks, {counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['error']} could not run, {counts['skipped']} skipped"
    )
    return "\n".join(lines)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("only", nargs="*", metavar="check", help="run only these checks (default: all)")


def run(args: argparse.Namespace, context: Context) -> int:
    known = {check.name: check for check in CHECKS}
    unknown = [name for name in args.only if name not in known]
    if unknown:
        print(f"verify: unknown check {unknown[0]!r}; choose from {', '.join(known)}", file=sys.stderr)
        return 2
    runtime = AnalysisRuntime(context.repo_root, ConfigFile.locate(context.repo_root, args.config), [])
    if not runtime.runtime_compatible():
        running, required = runtime.runtime_versions()
        print(
            f"verify: Python {running} is older than the backend's {required}; run with Python >={required}",
            file=sys.stderr,
        )
        return 2
    outcomes = verify(runtime, [known[name] for name in args.only] or CHECKS)
    print(render(outcomes))
    return exit_code(outcomes)
