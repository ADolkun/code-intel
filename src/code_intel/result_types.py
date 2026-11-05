"""JSON shapes shared by producers and consumers.

- DI JSON v2 (``di_map.build_di``) and routes JSON v2 (``route_map.build_routes``);
- the error codes and fields of the body every MCP tool returns on failure;
- the TypeScript helper's ``facts`` and ``routes`` protocol messages;
- ``ClientSpec``, one entry of the config's ``frontend.clients``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from code_intel.backend_model import DepKind, DepMarker, DepVia, IncludeModelKind, MountState, RouteKind

SCHEMA_VERSION = 2
FACTS_SCHEMA = 1

# ── Problems ─────────────────────────────────────────────────────────────


class DiagnosticBase(TypedDict):
    kind: str
    category: Literal["content_lost", "resolver"]
    stage: str
    step: str | None
    file: str | None
    line: int | None
    message: str


class DiagnosticDict(DiagnosticBase, total=False):
    exception: str  # exception type name
    likely_cause: str
    missing: str  # frontend_unavailable: the missing path, when one is known
    fix: str  # frontend_unavailable: what to do about it
    problem: str  # frontend_unavailable: the cause code (helper_failed, frontend_not_found, a probe code)


class LimitationDict(TypedDict):
    kind: str
    stage: str
    file: str | None
    line: int | None
    message: str


# ── Error bodies (presenters.error_body) ─────────────────────────────────

ErrorCode = Literal[
    "invalid_argument",
    "invalid_root",
    "runtime_incompatible",
    "analysis_failed",
    "frontend_unavailable",
    "internal_error",
]

# Extra fields per error code; a trailing "?" marks an optional field.
# `presenters.error_body` builds every error body from this table.
ERROR_FIELDS: dict[ErrorCode, tuple[str, ...]] = {
    "invalid_argument": ("argument", "value", "allowed?"),
    "invalid_root": ("root",),
    "runtime_incompatible": ("running", "required"),
    "analysis_failed": ("analyzer", "step", "missing?"),
    "frontend_unavailable": ("missing", "fix", "problem"),
    "internal_error": ("tool",),
}


# ── DI JSON v2 ───────────────────────────────────────────────────────────


class DiApp(TypedDict):
    id: str
    redirect_slashes: bool
    # The FastAPI version the configured ``backend.lock`` locks, and the include_router model it
    # gives: "copy" (up to 0.136, an include copies the router's routes when it
    # runs), "live" (0.137 and later, it serves routes added later too) or
    # "unknown" (``include_model_reason`` says why).
    fastapi_version: str | None
    include_model: IncludeModelKind
    include_model_reason: str | None


class DiDependencyFunction(TypedDict):
    id: str
    name: str
    module: str
    qualname: str
    kind: DepKind
    file: str | None
    line: int | None
    sub_deps: list[str]
    transitive_deps: list[str]
    aliases: list[str]
    # Only for an ``external`` kind: the expression the module-level name is assigned.
    bound_to: NotRequired[str]


class DiDepRow(TypedDict):
    id: str
    display: str
    via: DepVia
    param: str | None
    alias: str | None
    marker: DepMarker
    file: str
    line: int


class DiRoute(TypedDict):
    method: str
    path: str
    kind: RouteKind
    handler: str
    handler_id: str
    file: str
    line: int
    mount: MountState
    order: int
    chain: list[str]
    app_deps: list[str]
    router_deps: list[str]
    decorator_deps: list[str]
    direct_deps: list[str]
    deps: list[DiDepRow]
    all_deps: list[str]


class DiRouter(TypedDict):
    id: str
    file: str
    line: int
    kind: Literal["APIRouter", "FastAPI"]
    prefix: str | None
    prefix_resolved: bool
    deps: list[str]
    mounted: bool


class DiSummary(TypedDict):
    routes: int
    http: int
    websocket: int
    dependency_functions: int
    files_scanned: int


class DiResult(TypedDict):
    schema_version: Literal[2]
    complete: bool
    diagnostics: list[DiagnosticDict]
    analysis_limitations: list[LimitationDict]
    app: DiApp
    dependency_functions: dict[str, DiDependencyFunction]
    routes: list[DiRoute]
    unmounted_routes: list[DiRoute]
    routers: list[DiRouter]
    reverse_index: dict[str, list[str]]
    summary: DiSummary


# ── Routes JSON v2 ───────────────────────────────────────────────────────

FrontendCallKind = Literal["call", "options", "instance", "global", "websocket", "builder"]
FrontendMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "WS", "UNKNOWN"]
Confidence = Literal["exact", "possible"]
UrlOrigin = Literal["literal", "const", "builder", "conditional", "prop_flow"]
MatchReason = Literal[
    "literal",
    "param",
    "literal_on_param",
    "hole_on_literal",
    "mixed_segment",
    "trailing_hole",
    "method_unknown",
    "redirect",
    "prop_flow",
]


class BackendRouteRow(TypedDict):
    method: str
    path: str
    kind: RouteKind
    handler: str
    handler_id: str
    file: str
    line: int
    mount: MountState
    order: int


class FrontendCallRow(TypedDict):
    method: FrontendMethod
    path: str
    raw_path: str
    kind: FrontendCallKind
    client: str | None
    builder: str | None
    function: str | None
    file: str
    line: int
    def_line: int | None
    column: int
    confidence: Confidence
    origin: UrlOrigin
    is_test: bool
    # The callee as written (`rawFetch`, `api.apiJson`'s `apiJson`, `"apiJson"`)
    # and where it starts: a member call split over lines puts it below `line`.
    # None for WebSocket strings, whose `column` is the URL expression's start.
    callee: str | None
    callee_line: int | None
    callee_column: int | None  # 1-based, in code points, on `callee_line`


class MatchBackendSide(TypedDict):
    method: str
    path: str
    handler: str
    file: str
    line: int


class MatchFrontendSide(TypedDict):
    method: FrontendMethod
    path: str
    raw_path: str
    function: str | None
    file: str
    line: int
    def_line: int | None
    client: str | None


class MatchRow(TypedDict):
    backend: MatchBackendSide
    frontend: MatchFrontendSide
    confidence: Confidence
    reason: MatchReason


BackendOnlyRow = MatchBackendSide
FrontendOnlyRow = MatchFrontendSide


class RoutesSummary(TypedDict):
    backend_routes: int
    frontend_calls: int
    matched_routes: int
    matched_calls: int
    match_rows: int
    backend_only: int
    frontend_only: int
    test_calls_skipped: int
    coverage_pct: float | None  # None whenever complete is False


class RoutesResult(TypedDict):
    schema_version: Literal[2]
    complete: bool
    app: DiApp
    diagnostics: list[DiagnosticDict]
    analysis_limitations: list[LimitationDict]
    backend_routes: list[BackendRouteRow]
    frontend_calls: list[FrontendCallRow]
    matches: list[MatchRow]
    backend_only: list[BackendOnlyRow]
    frontend_only: list[FrontendOnlyRow]
    unmounted_routes: list[BackendRouteRow]
    summary: RoutesSummary


# ── Frontend client configuration ────────────────────────────────────────

ClientKind = Literal["call", "options", "instance", "global"]


@dataclass(frozen=True)
class ClientSpec:
    """One configured frontend API client; ``config`` fills the per-kind defaults.

    - ``call``: a function exported by ``module``; the URL is argument ``url_arg``
      and the init object argument ``init_arg``.
    - ``options``: a function exported by ``module`` taking an options object at
      ``options_arg`` whose ``url_property`` holds the URL.
    - ``instance``: an object exported by ``module`` (an axios-style instance);
      ``export.get/post/put/patch/delete/head/options(url, ...)`` and
      ``export.request({url, method})`` are calls, and ``base_path`` is prepended
      to every URL.
    - ``global``: a global function ``name`` (``fetch``); with ``same_origin_only``
      an absolute URL is another service and yields neither a call nor a diagnostic.

    ``module`` is repository-relative.
    """

    kind: ClientKind
    module: str | None = None
    export: str | None = None
    name: str | None = None
    default_method: str = "GET"
    url_arg: int | None = None
    init_arg: int | None = None
    options_arg: int | None = None
    url_property: str | None = None
    base_path: str = ""
    same_origin_only: bool = False

    def as_request(self) -> dict:
        """The ``clients`` entry of a ``routes`` helper request (unset fields omitted)."""
        out: dict = {"kind": self.kind}
        for key in ("module", "export", "name", "url_arg", "init_arg", "options_arg", "url_property"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        out["default_method"] = self.default_method
        if self.base_path:
            out["base_path"] = self.base_path
        if self.same_origin_only:
            out["same_origin_only"] = True
        return out


# ── TypeScript helper protocol 1 ─────────────────────────────────────────

FileStatus = Literal[
    "ok",
    "read_failed",
    "decode_failed",
    "parse_failed",
    "extract_failed",
]
TsDeclKind = Literal[
    "function",
    "component",
    "const",
    "variable",
    "class",
    "method",
    "interface",
    "type_alias",
    "enum",
    "namespace",
]
TsDeclScope = Literal["module", "namespace", "class", "object", "local"]


class FactsRequest(TypedDict):
    protocol: Literal[1]
    mode: Literal["facts"]
    typescript_dir: str
    files: list[str]  # absolute paths
    snippet_max: int
    signature_max: int


class SyntaxErrorJson(TypedDict):
    line: int
    column: int  # 1-based, in code points
    message: str


class JsxElementJson(TypedDict):
    tag: str
    line: int
    column: int
    kind: Literal["open", "self_close"]
    parent: str | None
    snippet: str


class TsDeclJson(TypedDict):
    name: str
    kind: TsDeclKind
    line: int
    end_line: int
    column: int
    scope: TsDeclScope
    container: str | None
    exported: bool
    signature: str


class FactsFileJson(TypedDict):
    """One file of a ``facts`` response.

    ``status`` is ``read_failed`` when the file could not be read, ``parse_failed``
    when the TypeScript parser itself threw (no tree, so no facts), and
    ``extract_failed`` when an extractor threw on the parsed tree; ``error`` then
    holds the message. ``decode_failed`` means bytes that are not UTF-8 were
    read as U+FFFD; the facts come from that text and ``error`` names the byte.
    Syntax errors leave ``status`` at ``ok``: the facts come from TypeScript's
    recovered tree and ``syntax_errors`` counts them.
    """

    path: str  # as given in the request
    status: FileStatus
    error: str | None
    syntax_errors: int
    first_syntax_error: SyntaxErrorJson | None
    elements: list[JsxElementJson]
    decls: list[TsDeclJson]


class FactsResponse(TypedDict):
    protocol: Literal[1]
    mode: Literal["facts"]
    typescript_version: str
    facts_schema: Literal[1]
    files: list[FactsFileJson]


class RoutesRequestFile(TypedDict):
    path: str
    is_test: bool


class RoutesLimits(TypedDict):
    max_url_variants: int
    max_eval_depth: int


class RoutesRequest(TypedDict):
    protocol: Literal[1]
    mode: Literal["routes"]
    typescript_dir: str
    frontend_root: str
    tsconfig: str
    files: list[RoutesRequestFile]
    clients: list[dict]  # ClientSpec.as_request()
    builder_modules: list[str]
    websocket_marker: str
    limits: RoutesLimits


class UrlTextPart(TypedDict):
    text: str


class UrlHolePart(TypedDict):
    hole: Literal["param", "query", "unknown"]
    expr: str


class UrlVariantJson(TypedDict):
    parts: list[UrlTextPart | UrlHolePart]
    confidence: Confidence
    origin: UrlOrigin
    via: list[str]


class RoutesCallJson(TypedDict):
    file: str
    line: int
    column: int
    is_test: bool
    kind: FrontendCallKind
    client: str | None
    builder: str | None
    function: str | None
    def_line: int | None
    method: FrontendMethod
    method_source: Literal["default", "init", "member", "unresolved"]
    urls: list[UrlVariantJson]
    raw: str
    callee: str | None  # the callee name as written; None for WebSocket strings
    callee_line: int | None
    callee_column: int | None  # 1-based, in code points, on `callee_line`


class RoutesUnresolvedJson(TypedDict):
    file: str
    line: int
    column: int
    client: str
    reason: Literal[
        "url_not_static",
        "url_not_path",
        "client_reference_not_called",
        "spread_arguments",
        "options_not_object",
        "client_not_resolved",
    ]
    text: str


class RoutesLimitationJson(TypedDict):
    kind: str
    file: str
    line: int
    message: str


class RoutesFileFailedJson(TypedDict):
    """A file whose calls were lost, or only partly read.

    ``step`` is ``read file`` (unreadable), ``read tsconfig`` (the tsconfig
    could not be read or parsed; ``file`` is the tsconfig), ``decode file``
    (bytes that are not UTF-8, read as U+FFFD), ``parse file`` (TypeScript
    syntax errors; the file is still analyzed from the recovered tree) or
    ``extract calls`` (an exception while reading the file's calls).
    """

    file: str
    step: str
    message: str


class RoutesParseFailedJson(RoutesFileFailedJson, total=False):
    # "parse_failed": the parser or binder threw on the file, so it was left
    # out of the program entirely (step "parse file").
    kind: Literal["parse_failed"]


class RoutesResponse(TypedDict):
    """The ``routes`` response.

    ``missing_client_modules`` holds ``"<module>"`` for a configured client
    module that is not in the program, or ``"<module>#<export>"`` when the
    module exists but does not declare that client (``"lib/api.ts#apiJson"``).
    Calls in files sent with ``is_test: true`` are returned with ``is_test``
    set; the consumer leaves them out of matching.
    """

    protocol: Literal[1]
    mode: Literal["routes"]
    typescript_version: str
    calls: list[RoutesCallJson]
    unresolved: list[RoutesUnresolvedJson]
    limitations: list[RoutesLimitationJson]
    missing_client_modules: list[str]
    files_failed: list[RoutesFileFailedJson | RoutesParseFailedJson]
    timings_ms: dict[str, int]
