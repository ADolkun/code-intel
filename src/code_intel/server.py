"""The code-intel MCP server (``code-intel mcp``).

Exposes FastAPI dependency mapping, frontend-to-backend route matching, JSX
render-site scanning, symbol disambiguation and a code-to-docs lookup as MCP
tools over stdio, for the repository ``--repo-root`` names (default: the git
work tree containing the current directory).

Architecture notes:
  • This module registers the tools, validates their arguments before any
    analysis and maps failures to error bodies. `analysis_runtime` owns every
    cache and its freshness; `queries` (`doc_queries` for the docs) holds the
    pure filtering and suggestion logic; `presenters` (`doc_presenters`)
    renders text and JSON through one envelope.
  • Backend analysis runs in-process; frontend analysis runs the TypeScript
    helper (`ts/helper.cjs`) with Node.
  • Each cache is keyed by a fingerprint of the files it reads and of the
    config file, so results refresh themselves on file and config changes.
    Package code is imported once, so an edit to it, or a bump of the
    consumer's `.code-intel/requirements.txt`, needs a server restart; every
    response warns when that happens (`analysis_runtime.CodeWatch`, fed by
    `loaded_code`, which records the bytes each module was compiled from).
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import posixpath
import traceback
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.tools.base import Tool
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import Field, ValidationError

from code_intel import (
    analysis_runtime,
    doc_presenters,
    doc_queries,
    doc_rules,
    loaded_code,
    presenters,
    queries,
)
from code_intel.analysis_runtime import AnalysisFailed, AnalysisRuntime, FrontendFailed, FrontendProblem, InvalidRoot
from code_intel.cli import Context as CliContext
from code_intel.config import ConfigFile
from code_intel.ts_bridge import ToolchainUnavailable

_log = logging.getLogger("code_intel.server")

MatchMode = Literal["exact", "suffix", "contains", "auto"]
Side = Literal["backend", "frontend", "both"]
OutputFormat = Literal["text", "json"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "WS"]
SymbolKindArg = Literal[
    "function",
    "class",
    "component",
    "method",
    "const",
    "variable",
    "type_alias",
    "interface",
    "enum",
    "namespace",
    "none",
]
Limit = Annotated[int | None, Field(ge=0)]
Name = Annotated[str, Field(min_length=1)]

# Default row cap for list-heavy tools: an unbounded `get_db` reverse lookup or a
# `<div>` usage scan runs to hundreds of KB, which buries the answer.
DEFAULT_LIMIT = 50


class _StrictServer(MCPServer):
    """An MCP server that rejects argument names a tool does not declare.

    The SDK builds each tool's argument model with extra keys allowed and drops
    them, so a misspelled argument (`hanlder=`) would silently return an
    unfiltered answer. It also refuses a value the schema rejects (`limit=-1`,
    an unknown enum value, a missing required argument) with a bare validation
    message before the tool runs, which a caller asking for JSON cannot parse.
    The raw arguments are only visible here, before that model runs, so both
    become the documented `invalid_argument` body."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            allowed = sorted(tool.parameters.get("properties", {}))
            unknown = sorted(set(arguments) - set(allowed))
            if unknown:
                body = presenters.unknown_arguments(name, unknown, arguments[unknown[0]], allowed)
                return _argument_error(body, arguments)
            if (refused := _schema_refusal(tool, arguments)) is not None:
                return _argument_error(refused, arguments)
        return await super().call_tool(name, arguments, context)


def _schema_refusal(tool: Tool, arguments: dict[str, Any]) -> presenters.Body | None:
    """The first argument the tool's schema refuses, as an `invalid_argument`
    body, validated exactly as the SDK is about to (JSON strings pre-parsed)."""
    try:
        tool.fn_metadata.validate_arguments(arguments)
    except ValidationError as exc:
        error = exc.errors()[0]
        argument = str(error["loc"][0]) if error["loc"] else "arguments"
        schema = tool.parameters.get("properties", {}).get(argument, {})
        return presenters.invalid_argument(argument, arguments.get(argument), _schema_choices(schema), error["msg"])
    return None


def _schema_choices(schema: dict[str, Any]) -> tuple[str, ...]:
    """The fixed values an argument's schema accepts (a Literal, optional or
    not), or none for a free-form argument."""
    for option in (schema, *schema.get("anyOf", ())):
        if "enum" in option:
            return tuple(option["enum"])
    return ()


def _argument_error(body: presenters.Body, arguments: dict[str, Any]) -> CallToolResult:
    requested = arguments.get("output_format")
    output_format = requested if requested in presenters.OUTPUT_FORMATS else "text"
    text = _respond(body, output_format)
    return CallToolResult(content=[TextContent(type="text", text=str(text))], is_error=True)


mcp = _StrictServer(
    "code-intel",
    instructions=(
        "Static analysis of this repository's FastAPI backend and TypeScript frontend; "
        "caches refresh themselves on file and config changes.\n"
        "- di_deps / di_reverse: FastAPI Depends chains for a route and the blast radius of a "
        "dependency, including app, router, include_router and route-decorator guards, identified "
        "by module:qualname.\n"
        "- route_match / route_unmatched: frontend API call to backend handler, and orphans on "
        "either side.\n"
        "- jsx_usage: <Component> render sites.\n"
        "- symbol_resolve: which definition a same-name symbol means, and the Protocol methods a "
        "Python class's calls typed as a Protocol resolve to.\n"
        "- doc_refs: docs that mention a file, route or symbol, and a doc's links and code "
        "references with whether each still exists.\n"
        "- status: health, staleness, toolchain, analysis limitations.\n"
        "For general references, call hierarchy and text search, use the repository's other "
        "code-intelligence tools (see AGENTS.md)."
    ),
)

# Set by `configure` (`code-intel mcp`) before the server runs.
runtime: AnalysisRuntime | None = None


def configure(repo_root: Path, config_file: ConfigFile) -> AnalysisRuntime:
    """The runtime the tools answer from, for ``repo_root`` and its config."""
    global runtime
    runtime = AnalysisRuntime(
        repo_root,
        config_file,
        analysis_runtime.watched_files(),
        pin_files=[repo_root / analysis_runtime.PIN_REL],
        code_loaded_at_ns=loaded_code.LOADED_AT_NS,
        code_imported=loaded_code.IMPORTED,
    )
    return runtime


def _runtime() -> AnalysisRuntime:
    if runtime is None:
        raise RuntimeError("the server has no repository; start it with `code-intel mcp`")
    return runtime


# ── Responses, failures and argument validation ──────────────────────────


class ErrorText(str):
    """A rendered error body. Direct callers get the text; the MCP client gets
    it with ``isError=True`` (see `_tool`)."""

    __slots__ = ()


def _respond(body: presenters.Body, output_format: str, analysis: Any = None, *, banners: bool = True) -> str:
    rt = _runtime()
    code = rt.code_state()
    text = presenters.envelope(
        body,
        output_format,
        stale=code.stale is True,
        unconfirmed=rt.code_unconfirmed(code),
        analysis=analysis,
        banners=banners,
    )
    return ErrorText(text) if body.is_error else text


def _frontend_unavailable(problem: FrontendProblem, output_format: str) -> str:
    return _respond(presenters.frontend_unavailable(**problem.fields()), output_format)


def _crash_site(exc: BaseException) -> str:
    """``file:line in function`` of the frame that raised, so the reply is debuggable without the log."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return "unknown frame"
    last = frames[-1]
    return f"{Path(last.filename).name}:{last.lineno} in {last.name}"


def _run(tool: str, output_format: str, body: Callable[[], str]) -> str:
    """Run a tool body, mapping every failure to an error body in the envelope."""
    if output_format not in presenters.OUTPUT_FORMATS:
        error = presenters.invalid_argument(
            "output_format", output_format, presenters.OUTPUT_FORMATS, "unknown output format"
        )
        return _respond(error, "text")
    try:
        return body()
    except AnalysisFailed as exc:
        error = presenters.error_body(
            "analysis_failed", exc.message, analyzer=exc.analyzer, step=exc.step, missing=exc.missing
        )
        return _respond(error, output_format)
    except ToolchainUnavailable as exc:
        return _frontend_unavailable(FrontendProblem.from_toolchain(exc.problem), output_format)
    except FrontendFailed as exc:
        return _frontend_unavailable(exc.problem, output_format)
    except InvalidRoot as exc:
        return _respond(presenters.error_body("invalid_root", str(exc), root=exc.root), output_format)
    except Exception as exc:
        # Every tool answers with an error body instead of raising into the
        # transport; the traceback goes to the server log (stderr on stdio).
        _log.exception("%s failed unexpectedly", tool)
        message = f"{tool} failed unexpectedly at {_crash_site(exc)}: {type(exc).__name__}: {exc}"
        return _respond(presenters.error_body("internal_error", message, tool=tool), output_format)


def _check_name(argument: str, value: str | None, *, required: bool = False) -> presenters.Body | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        return presenters.invalid_argument(argument, value, (), "must be a non-empty, non-whitespace string")
    return None


def _check_choice(
    argument: str, value: Any, allowed: tuple[str, ...], *, optional: bool = False
) -> presenters.Body | None:
    if (optional and value is None) or value in allowed:
        return None
    return presenters.invalid_argument(argument, value, allowed, "not an accepted value")


def _check_limit(limit: Any) -> presenters.Body | None:
    if limit is None or (isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0):
        return None
    return presenters.invalid_argument("limit", limit, (), "must be an integer >= 0, or null for every row")


def _first_error(*checks: presenters.Body | None) -> presenters.Body | None:
    return next((c for c in checks if c is not None), None)


def _refusal() -> presenters.Body | None:
    """Refuse backend analysis on an interpreter older than the backend needs:
    its PEP 758 `except A, B:` clauses would not parse, so results would silently
    miss those files."""
    rt = _runtime()
    if rt.runtime_compatible():
        return None
    return presenters.runtime_refusal(*rt.runtime_versions())


def _check_dep_function(value: str) -> presenters.Body | None:
    if queries.is_dep_id(value) or queries.bare_dep_name(value) is not None:
        return None
    return presenters.invalid_argument(
        "dep_function", value, (), "a factory call must be name(arguments), with nothing after the closing parenthesis"
    )


def _repo_path(argument: str, value: str) -> tuple[str | None, presenters.Body | None]:
    """``value`` as a repository-relative POSIX path; an absolute path inside
    the repository is converted. The path need not exist (a lookup after a
    deletion still finds the docs naming it), so nothing is resolved on disk."""

    def invalid(reason: str) -> tuple[None, presenters.Body]:
        return None, presenters.invalid_argument(argument, value, (), reason)

    if "\0" in value:
        return invalid("must not contain a NUL byte")
    text = value.strip()
    if os.path.isabs(text):
        repo_root = _runtime().repo_root
        normalized = os.path.normpath(text)
        for root in dict.fromkeys((str(repo_root), str(repo_root.resolve()))):
            if normalized == root or normalized.startswith(root.rstrip(os.sep) + os.sep):
                text = Path(os.path.relpath(normalized, root)).as_posix()
                break
        else:
            return invalid("is outside the repository; give a repository-relative path")
    rel = doc_rules.normalize_repo_path(text)
    if rel is None:
        if posixpath.normpath(text) == ".":
            return invalid("names the repository root; give a file or directory inside it")
        return invalid("escapes the repository; give a repository-relative path")
    return rel, None


_MATCH_MODES = typing.get_args(MatchMode)
_SIDES = typing.get_args(Side)
_METHODS = typing.get_args(HttpMethod)
_SYMBOL_KINDS = typing.get_args(SymbolKindArg)


# ── Tools ────────────────────────────────────────────────────────────────


def _tool[ToolFn: Callable[..., str]](fn: ToolFn) -> ToolFn:
    """Register ``fn`` as a text-only MCP tool whose error bodies reach the
    client with ``isError=True``. Returns ``fn`` itself, so direct callers get
    the text whether it is an error or not."""

    @functools.wraps(fn)
    def registered(*args: Any, **kwargs: Any) -> str | CallToolResult:
        result = fn(*args, **kwargs)
        if isinstance(result, ErrorText):
            return CallToolResult(content=[TextContent(type="text", text=str(result))], is_error=True)
        return result

    mcp.tool(structured_output=False)(registered)
    return fn


@_tool
def di_deps(
    handler: str | None = None,
    path: str | None = None,
    method: HttpMethod | None = None,
    match: MatchMode = "exact",
    limit: Limit = None,
    output_format: OutputFormat = "text",
) -> str:
    """FastAPI dependency chain for a route: app, router and include guards,
    route-decorator guards, handler parameters, and the transitive closure. With
    no handler or path, lists every dependency target. Lines are `def` lines.

    Args:
        handler: Handler short name ("create_item") or its module:qualname id.
        path: Route path ("/items"); parameter names are ignored.
        method: HTTP method (upper case); needs handler or path.
        match: "exact" (default), "suffix", "contains", or "auto".
        limit: Max rows; default none (every row).
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if invalid := _first_error(
            _check_name("handler", handler),
            _check_name("path", path),
            _check_choice("match", match, _MATCH_MODES),
            _check_choice("method", method, _METHODS, optional=True),
            _check_limit(limit),
        ):
            return _respond(invalid, output_format)
        if method is not None and handler is None and path is None:
            return _respond(
                presenters.invalid_argument("method", method, (), "method needs handler or path"), output_format
            )
        if refusal := _refusal():
            return _respond(refusal, output_format)
        data = _runtime().get_di()
        result = queries.di_deps(data, handler=handler, path=path, method=method, match=match, limit=limit)
        return _respond(presenters.di_deps(result), output_format, data)

    return _run("di_deps", output_format, body)


@_tool
def di_reverse(
    dep_function: Name,
    limit: Limit = DEFAULT_LIMIT,
    output_format: OutputFormat = "text",
) -> str:
    """Blast radius: every route using a dependency, labelled direct, route
    decorator, router, app or transitive, with each handler's `def` line.

    Matches an exact module:qualname id, then an exact short name, then an exact
    alias. A name defined in several modules returns one group per definition.

    Args:
        dep_function: Id ("pkg.deps:get_db", "pkg.svc:Svc().get" for a bound
            method), name or alias ("get_db"); a factory call such as
            "require_permission(X)" matches on the text before "(".
        limit: Max rows across all groups; default 50, null for every row.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if invalid := _first_error(
            _check_name("dep_function", dep_function, required=True),
            _check_dep_function(dep_function),
            _check_limit(limit),
        ):
            return _respond(invalid, output_format)
        if refusal := _refusal():
            return _respond(refusal, output_format)
        data = _runtime().get_di()
        result = queries.di_reverse(data, dep_function, limit=limit)
        return _respond(presenters.di_reverse(result), output_format, data)

    return _run("di_reverse", output_format, body)


@_tool
def route_match(
    path: str | None = None,
    handler: str | None = None,
    frontend_func: str | None = None,
    match: MatchMode = "exact",
    limit: Limit = DEFAULT_LIMIT,
    output_format: OutputFormat = "text",
) -> str:
    """Match a frontend API call to its backend handler, or the reverse.

    With no arguments, returns a coverage summary. A handler, function or path
    that exists but is unmatched falls back to the orphan lists. Backend lines
    are handler `def` lines; frontend rows show `def D · call C`. Without Node
    or TypeScript, returns the backend routes that fit, marked incomplete, and
    makes no claim about their frontend consumers.

    Args:
        path: Route path; parameter names are ignored ("/items/{id}").
        handler: Backend handler short name ("create_item").
        frontend_func: Frontend function name ("createItem").
        match: "exact" (default), "suffix", "contains", or "auto".
        limit: Max rows; default 50, null for every row.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if invalid := _first_error(
            _check_name("path", path),
            _check_name("handler", handler),
            _check_name("frontend_func", frontend_func),
            _check_choice("match", match, _MATCH_MODES),
            _check_limit(limit),
        ):
            return _respond(invalid, output_format)
        if refusal := _refusal():
            return _respond(refusal, output_format)
        data = _runtime().get_routes()
        result = queries.route_match(
            data, path=path, handler=handler, frontend_func=frontend_func, match=match, limit=limit
        )
        return _respond(presenters.route_match(result), output_format, data)

    return _run("route_match", output_format, body)


@_tool
def route_unmatched(
    side: Side = "both",
    limit: Limit = DEFAULT_LIMIT,
    output_format: OutputFormat = "text",
) -> str:
    """List orphaned routes: backend handlers with no frontend consumer, or
    frontend calls pointing at no backend route. Needs Node and TypeScript for
    every side, because without the frontend every route would look unused.

    Args:
        side: "backend", "frontend", or "both" (default).
        limit: Max rows per side; default 50, null for every row.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if invalid := _first_error(_check_choice("side", side, _SIDES), _check_limit(limit)):
            return _respond(invalid, output_format)
        if refusal := _refusal():
            return _respond(refusal, output_format)
        rt = _runtime()
        data = rt.get_routes()
        if (frontend_problem := rt.routes_frontend_problem()) is not None:
            return _frontend_unavailable(frontend_problem, output_format)
        result = queries.route_unmatched(data, side=side, limit=limit)
        return _respond(presenters.route_unmatched(result), output_format, data)

    return _run("route_unmatched", output_format, body)


@_tool
def status(load_counts: bool = False, output_format: OutputFormat = "text") -> str:
    """Server health: completeness, cache freshness, stale analyzer code, git SHA,
    Python, the config, the Node/TypeScript toolchain, diagnostics and analysis
    limitations.

    Args:
        load_counts: If true, run DI and routes to include their counts. Each
            analyzer reports its own failure; on an old interpreter none runs.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        snapshot = _runtime().status(load_counts=load_counts)
        return _respond(presenters.status(snapshot), output_format, snapshot.analysis, banners=False)

    return _run("status", output_format, body)


@_tool
def jsx_usage(
    component_name: Name,
    root: str | None = None,
    limit: Limit = DEFAULT_LIMIT,
    include_self_close: bool = True,
    include_tests: bool = False,
    output_format: OutputFormat = "text",
) -> str:
    """Every JSX render site of a component (`<Comp/>`, `<Comp>…</Comp>`) with
    file:line:column, tag kind and parent function, from the TypeScript parser.
    Scans .tsx, .jsx, .js, .mjs and .cjs files git sees. Needs Node and TypeScript.

    Args:
        component_name: Exact tag name ("DataTable", "Ctx.Provider").
        root: Directory to scan, relative to the repo root. Default: the
            configured frontend root.
        limit: Max rows; default 50, null for every row.
        include_self_close: Include `<Foo />` usages. Default true.
        include_tests: Include test, spec and mock files. Default false.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if invalid := _first_error(
            _check_name("component_name", component_name, required=True),
            _check_name("root", root),
            _check_limit(limit),
        ):
            return _respond(invalid, output_format)
        rt = _runtime()
        scan_root = root if root is not None else rt.config("jsx_usage").frontend.root
        root_dir = rt.resolve_root(scan_root)
        found = rt.get_jsx_usage(
            component_name, root_dir, include_self_close=include_self_close, include_tests=include_tests
        )
        result = queries.jsx_usages(
            component_name,
            found.usages,
            limit=limit,
            root=scan_root,
            include_self_close=include_self_close,
            include_tests=include_tests,
            found_problems=found.problems,
        )
        return _respond(presenters.jsx_usage(result), output_format, result.analysis)

    return _run("jsx_usage", output_format, body)


@_tool
def symbol_resolve(
    name: Name,
    kind: SymbolKindArg | None = None,
    file_hint: str | None = None,
    side: Side = "both",
    limit: Limit = DEFAULT_LIMIT,
    output_format: OutputFormat = "text",
) -> str:
    """Which definition a symbol name means: ranked candidates across backend
    Python and frontend TypeScript/JavaScript (.ts, .tsx, .mts, .cts, .js, .jsx,
    .mjs, .cjs) with file, line range, kind, scope, signature and score. A
    Python method also names the Protocol methods that calls typed as a
    Protocol resolve to (LSP files those calls there, not under the class).

    Args:
        name: Exact symbol name ("DataTable", "list_items").
        kind: Optional filter: function, class, component, method, const,
            variable, type_alias, interface, enum, namespace, or none (any).
        file_hint: Substring or glob of the path; matches score 1.0.
        side: "backend", "frontend", or "both" (default).
        limit: Max rows; default 50, null for every row.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if invalid := _first_error(
            _check_name("name", name, required=True),
            _check_name("file_hint", file_hint),
            _check_choice("kind", kind, _SYMBOL_KINDS, optional=True),
            _check_choice("side", side, _SIDES),
            _check_limit(limit),
        ):
            return _respond(invalid, output_format)
        if side != "frontend" and (refusal := _refusal()):
            return _respond(refusal, output_format)
        kind_filter = None if kind in (None, "none") else kind
        found = _runtime().resolve_symbol(name, side=side, kind=kind_filter, file_hint=file_hint)
        result = queries.symbol_resolution(name, found, side=side, kind=kind_filter, file_hint=file_hint, limit=limit)
        return _respond(presenters.symbol_resolution(result), output_format, result.analysis)

    return _run("symbol_resolve", output_format, body)


@_tool
def doc_refs(
    file: str | None = None,
    route: str | None = None,
    symbol: str | None = None,
    doc: str | None = None,
    limit: Limit = DEFAULT_LIMIT,
    output_format: OutputFormat = "text",
) -> str:
    """Docs that mention a file, route or symbol, with the line and whether
    each mention still matches the code; or, for a doc, its links, code
    references and the docs linking to it. Give exactly one argument.

    Args:
        file: Repository path of a file or directory; need not exist, so the
            docs naming a deleted file are found. Also finds docs naming a
            route it handles or a symbol it defines.
        route: "METHOD /path" or "/path" ("POST /items/{id}/checkout");
            parameter names are ignored, a mention without a method matches
            any method, and a configured route prefix may be added or omitted.
        symbol: Name or dotted name ("CheckoutBlockedDetail", "Item.status").
            A name also matches the first or last part of a dotted mention,
            and the reply says `ambiguous` when its definitions span more than
            one owner or file; a dotted name matches only that owner's attribute.
        doc: A Markdown doc ("docs/items.md").
        limit: Max rows per list; default 50, null for every row.
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        given = {"file": file, "route": route, "symbol": symbol, "doc": doc}
        if invalid := _first_error(*(_check_name(name, value) for name, value in given.items())):
            return _respond(invalid, output_format)
        named = [name for name, value in given.items() if value is not None]
        if len(named) != 1:
            argument = named[1] if named else "file"
            reason = "give exactly one of file, route, symbol or doc"
            return _respond(presenters.invalid_argument(argument, given[argument], (), reason), output_format)
        if route is not None and doc_queries.parse_doc_route(route) is None:
            methods = ", ".join((*_METHODS, "WebSocket"))
            reason = f"must be 'METHOD /path' or '/path', with METHOD one of {methods}"
            return _respond(presenters.invalid_argument("route", route, (), reason), output_format)
        if symbol is not None and not doc_queries.is_doc_symbol(symbol):
            reason = "must be a name or a dotted name, such as 'Item' or 'Item.status'"
            return _respond(presenters.invalid_argument("symbol", symbol, (), reason), output_format)
        paths: dict[str, str | None] = {"file": None, "doc": None}
        for name in ("file", "doc"):
            if (value := given[name]) is not None:
                rel, invalid = _repo_path(name, value)
                if invalid is not None:
                    return _respond(invalid, output_format)
                paths[name] = rel
        if paths["doc"] is not None and not paths["doc"].lower().endswith(".md"):
            reason = "must be a Markdown file ending in .md"
            return _respond(presenters.invalid_argument("doc", doc, (), reason), output_format)
        if invalid := _check_limit(limit):
            return _respond(invalid, output_format)
        # Routes and backend symbols come from the backend analysis.
        if refusal := _refusal():
            return _respond(refusal, output_format)
        found = _runtime().get_doc_report()
        needs_frontend = doc_queries.doc_lookup_needs_frontend(file=paths["file"], symbol=symbol)
        result = doc_queries.doc_refs_lookup(
            found.report,
            file=paths["file"],
            route=route,
            symbol=symbol,
            doc=paths["doc"],
            limit=limit,
            frontend_problems=found.frontend_problems if needs_frontend else (),
        )
        return _respond(doc_presenters.doc_refs(result), output_format, result.analysis)

    return _run("doc_refs", output_format, body)


@_tool
def refresh(output_format: OutputFormat = "text") -> str:
    """Drop every cache and rebuild DI and routes. Troubleshooting only: caches
    already refresh themselves on source and config changes. Does not reload
    analyzer code (restart the server for that).

    Args:
        output_format: "text" (default) or "json".
    """

    def body() -> str:
        if refusal := _refusal():
            return _respond(refusal, output_format)
        result = _runtime().refresh()
        return _respond(presenters.refresh(result), output_format, result.analysis)

    return _run("refresh", output_format, body)


# ── Command ──────────────────────────────────────────────────────────────


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """No options beyond ``--repo-root`` and ``--config``."""


def run(args: argparse.Namespace, context: CliContext) -> int:
    configure(context.repo_root, ConfigFile.locate(context.repo_root, args.config))
    mcp.run(transport="stdio")
    return 0
