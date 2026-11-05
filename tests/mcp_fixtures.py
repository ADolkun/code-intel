"""Test fixtures for the MCP layer: DI JSON v2 and routes JSON v2 builders typed
by `result_types`, fake analyzers to inject into `AnalysisRuntime`, and the docs
lookup fixture.

Used by test_mcp_queries.py, test_presenters.py, test_mcp_tools.py and
test_analysis_runtime.py, so the four agree on one data shape.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from doc_config import docs_toml
from fake_repo_files import repo_files_on_disk
from fixture_repo import LATE_NS, Clock, write

from code_intel import analysis_runtime, doc_rules, problems
from code_intel.config import BackendConfig, Config, ConfigFile, FrontendConfig, load_config
from code_intel.doc_rules import DocRefsConfig
from code_intel.repo_files import RepoFiles
from code_intel.result_types import (
    BackendRouteRow,
    DiagnosticDict,
    DiDependencyFunction,
    DiDepRow,
    DiResult,
    DiRoute,
    FrontendCallRow,
    LimitationDict,
    MatchBackendSide,
    MatchFrontendSide,
    MatchRow,
    RoutesResult,
)
from code_intel.ts_bridge import FRONTEND_TS_FIX, Toolchain, ToolchainProblem, ToolchainUnavailable

__all__ = ["LATE_NS", "Clock", "write"]

ROUTE_FILE = "backend/service_backend/api/routes/r.py"
ROUTE_MODULE = "service_backend.api.routes.r"
API_FILE = "frontend/lib/api.ts"

# Where the fake toolchain's TypeScript lives (repo-relative), and what a
# missing one is reported as.
TS_DIR = "ts-cache/node_modules/typescript"
TS_MISSING = f"{TS_DIR}/package.json"
TS_FIX = FRONTEND_TS_FIX


# ── DI JSON v2 ───────────────────────────────────────────────────────────


def dep(
    dep_id: str,
    *,
    kind: str = "function",
    file: str | None = "backend/service_backend/deps.py",
    line: int | None = 1,
    sub: Sequence[str] = (),
    trans: Sequence[str] = (),
    aliases: Sequence[str] = (),
) -> DiDependencyFunction:
    module, qualname = dep_id.split(":", 1)
    return cast(
        DiDependencyFunction,
        {
            "id": dep_id,
            "name": qualname.split(".")[-1],
            "module": module,
            "qualname": qualname,
            "kind": kind,
            "file": file,
            "line": line,
            "sub_deps": list(sub),
            "transitive_deps": list(trans),
            "aliases": list(aliases),
        },
    )


def use(dep_id: str, display: str, via: str, *, alias: str | None = None, param: str | None = None) -> DiDepRow:
    return cast(
        DiDepRow,
        {
            "id": dep_id,
            "display": display,
            "via": via,
            "param": param,
            "alias": alias,
            "marker": "Depends",
            "file": ROUTE_FILE,
            "line": 1,
        },
    )


def di_route(
    handler: str,
    path: str,
    *,
    method: str = "GET",
    line: int = 10,
    deps: Iterable[DiDepRow] = (),
    all_deps: Sequence[str] = (),
    file: str = ROUTE_FILE,
    module: str = ROUTE_MODULE,
) -> DiRoute:
    rows = list(deps)

    def displays(via: str) -> list[str]:
        return [d["display"] for d in rows if d["via"] == via]

    return cast(
        DiRoute,
        {
            "method": method,
            "path": path,
            "kind": "websocket" if method == "WS" else "http",
            "handler": handler,
            "handler_id": f"{module}:{handler}",
            "file": file,
            "line": line,
            "mount": "static",
            "order": 0,
            "chain": ["service_backend.main:app"],
            "app_deps": displays("app"),
            "router_deps": displays("router"),
            "decorator_deps": displays("route decorator"),
            "direct_deps": displays("direct"),
            "deps": rows,
            "all_deps": list(all_deps) or list(dict.fromkeys(d["id"] for d in rows)),
        },
    )


def di_result(
    functions: Iterable[DiDependencyFunction] = (),
    routes: Iterable[DiRoute] = (),
    *,
    complete: bool = True,
    diagnostics: Iterable[Mapping[str, Any]] = (),
    limitations: Iterable[Mapping[str, Any]] = (),
) -> DiResult:
    funcs = list(functions)
    route_rows = list(routes)
    for i, r in enumerate(route_rows):
        r["order"] = i
    ws = sum(1 for r in route_rows if r["kind"] == "websocket")
    return cast(
        DiResult,
        {
            "schema_version": 2,
            "complete": complete,
            "diagnostics": [cast(DiagnosticDict, d) for d in diagnostics],
            "analysis_limitations": [cast(LimitationDict, lim) for lim in limitations],
            "app": {"id": "service_backend.main:app", "redirect_slashes": False},
            "dependency_functions": {f["id"]: f for f in funcs},
            "routes": route_rows,
            "unmounted_routes": [],
            "routers": [],
            "reverse_index": {},
            "summary": {
                "routes": len(route_rows),
                "http": len(route_rows) - ws,
                "websocket": ws,
                "dependency_functions": len(funcs),
                "files_scanned": 3,
            },
        },
    )


# ── Routes JSON v2 ───────────────────────────────────────────────────────


def backend_side(method: str, path: str, handler: str, *, line: int = 1, file: str = ROUTE_FILE) -> MatchBackendSide:
    return {"method": method, "path": path, "handler": handler, "file": file, "line": line}


def frontend_side(
    method: str,
    path: str,
    function: str | None,
    *,
    line: int = 10,
    def_line: int | None = None,
    file: str = API_FILE,
    client: str | None = "apiJson",
    raw_path: str | None = None,
) -> MatchFrontendSide:
    return cast(
        MatchFrontendSide,
        {
            "method": method,
            "path": path,
            "raw_path": raw_path if raw_path is not None else path,
            "function": function,
            "file": file,
            "line": line,
            "def_line": def_line,
            "client": client,
        },
    )


def match(
    backend: MatchBackendSide, frontend: MatchFrontendSide, *, confidence: str = "exact", reason: str = "literal"
) -> MatchRow:
    return cast(MatchRow, {"backend": backend, "frontend": frontend, "confidence": confidence, "reason": reason})


def pair(path: str, handler: str, function: str | None, *, method: str = "GET", **frontend: Any) -> MatchRow:
    confidence = frontend.pop("confidence", "exact")
    reason = frontend.pop("reason", "literal")
    return match(
        backend_side(method, path, handler),
        frontend_side(method, path, function, **frontend),
        confidence=confidence,
        reason=reason,
    )


def routes_result(
    matches: Iterable[MatchRow] = (),
    backend_only: Iterable[MatchBackendSide] = (),
    frontend_only: Iterable[MatchFrontendSide] = (),
    *,
    complete: bool = True,
    diagnostics: Iterable[Mapping[str, Any]] = (),
    limitations: Iterable[Mapping[str, Any]] = (),
) -> RoutesResult:
    match_rows = list(matches)
    b_only = list(backend_only)
    f_only = list(frontend_only)
    backend_routes: list[BackendRouteRow] = []
    seen: set[tuple] = set()
    for b in [m["backend"] for m in match_rows] + b_only:
        key = (b["method"], b["path"], b["handler"])
        if key in seen:
            continue
        seen.add(key)
        backend_routes.append(
            cast(
                BackendRouteRow,
                {
                    **b,
                    "kind": "websocket" if b["method"] == "WS" else "http",
                    "handler_id": f"{ROUTE_MODULE}:{b['handler']}",
                    "mount": "static",
                    "order": len(backend_routes),
                },
            )
        )
    calls: list[FrontendCallRow] = []
    for f in [m["frontend"] for m in match_rows] + f_only:
        calls.append(
            cast(
                FrontendCallRow,
                {
                    **f,
                    "kind": "websocket" if f["method"] == "WS" else "client",
                    "builder": None,
                    "column": 3,
                    "confidence": "exact",
                    "origin": "literal",
                    "is_test": False,
                },
            )
        )
    matched_routes = len({(m["backend"]["method"], m["backend"]["path"]) for m in match_rows})
    matched_calls = len({(m["frontend"]["file"], m["frontend"]["line"]) for m in match_rows})
    return cast(
        RoutesResult,
        {
            "schema_version": 2,
            "complete": complete,
            "diagnostics": [cast(DiagnosticDict, d) for d in diagnostics],
            "analysis_limitations": [cast(LimitationDict, lim) for lim in limitations],
            "backend_routes": backend_routes,
            "frontend_calls": calls,
            "matches": match_rows,
            "backend_only": b_only,
            "frontend_only": f_only,
            "unmounted_routes": [],
            "summary": {
                "backend_routes": len(backend_routes),
                "frontend_calls": len(calls),
                "matched_routes": matched_routes,
                "matched_calls": matched_calls,
                "match_rows": len(match_rows),
                "backend_only": len(b_only),
                "frontend_only": len(f_only),
                "test_calls_skipped": 0,
                "coverage_pct": round(100 * matched_routes / len(backend_routes), 1)
                if backend_routes and complete
                else None,
            },
        },
    )


def frontend_unavailable_diagnostic() -> dict:
    return dict(
        problems.diagnostic(
            "frontend_unavailable",
            stage="route_map",
            step="analyze frontend",
            file=TS_MISSING,
            message=f"{TS_MISSING} not found. Fix: {TS_FIX}",
            missing=TS_MISSING,
            fix=TS_FIX,
        ).as_dict()
    )


def limitation(kind: str = "opaque_router_call", message: str = "app escapes to an external call") -> dict:
    return dict(
        problems.limitation(
            kind, stage="router_graph", file="backend/service_backend/main.py", line=764, message=message
        ).as_dict()
    )


def read_failed(file: str = "backend/service_backend/x.py") -> dict:
    return dict(
        problems.diagnostic(
            "read_failed", stage="py_sources", step="read file", file=file, exc=PermissionError(13, "Permission denied")
        ).as_dict()
    )


def unresolved_app(module: str = "service_backend.main") -> dict:
    """The DI diagnostic for an app that did not resolve: its ``file`` is the
    module name when the module itself was not found, not a repository path."""
    return dict(
        problems.diagnostic(
            "unresolved_app",
            stage="build router graph",
            step="resolve app",
            file=module,
            message=f"{module}:app: could not resolve the app (not a FastAPI app)",
        ).as_dict()
    )


# ── Toolchain ────────────────────────────────────────────────────────────


def missing_typescript() -> ToolchainProblem:
    return ToolchainProblem("typescript_not_found", f"{TS_MISSING} not found", TS_MISSING, TS_FIX)


def toolchain(repo_root: Path, *, available: bool = True) -> Toolchain:
    if available:
        return Toolchain(
            node="/usr/bin/node",
            node_version="v24.21.0",
            typescript_dir=repo_root / TS_DIR,
            typescript_version="7.1.0-beta",
            typescript_source="helper",
            lockfile_typescript_version="7.1.0-beta",
            problem=None,
            warnings=(),
            repo_root=repo_root,
        )
    return Toolchain(
        node="/usr/bin/node",
        node_version="v24.21.0",
        typescript_dir=None,
        typescript_version=None,
        typescript_source=None,
        lockfile_typescript_version="7.1.0-beta",
        problem=missing_typescript(),
        warnings=(),
        repo_root=repo_root,
    )


# ── Fake analyzers ───────────────────────────────────────────────────────


@dataclass
class FakeUsage:
    file: str
    line: int
    column: int = 5
    snippet: str = "<Widget />"
    parent_function: str | None = "Page"
    tag_kind: str = "self_close"
    is_test: bool = False


@dataclass
class FakeCandidate:
    name: str
    file: str
    start_line: int
    end_line: int | None
    kind: str
    signature: str
    language: str
    score: float = 0.5
    scope: str = "module"
    container: str | None = None
    exported: bool = True
    is_test: bool = False
    protocol_method: bool = False
    protocols: list = field(default_factory=list)


@dataclass
class FakeSymbolResult:
    candidates: list
    complete: bool = True
    problems: list = field(default_factory=list)
    frontend_problem: Any = None


class FakeFacts:
    def __init__(self) -> None:
        self.cleared = 0

    def clear(self) -> None:
        self.cleared += 1

    def stats(self) -> dict:
        return {"entries": 2, "hits": 3, "misses": 2, "helper_runs": 1, "last_helper_ms": 12}


@dataclass
class FakeModel:
    """Stands in for `backend_model.BackendModel`; only `sources` is read by the runtime."""

    sources: Any
    serial: int


class FakeAnalyzers:
    """Counting fakes with replaceable results. ``analyzers()`` gives the
    `Analyzers` record to inject.

    ``list_repo_files`` lists the files on disk (no git) unless ``repo_files``
    is set; the doc index is the real `doc_refs.DocIndex`, which reads only the
    files it is given, and the pages are the public files only unless ``page_provider`` is set. The symbol
    index answers ``python_symbols`` from ``py_symbols`` and
    ``typescript_decls`` from ``ts_decls``, raising ``ts_error`` instead when
    it is set (a `ToolchainProblem` raises `ToolchainUnavailable`).
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.di: DiResult = di_result()
        self.routes: RoutesResult = routes_result()
        self.usages: list = []
        self.jsx_problems: list = []
        self.symbols: FakeSymbolResult | ToolchainProblem = FakeSymbolResult([])
        self.symbol_calls: list[dict] = []
        self.symbol_configs: list[Config] = []
        self.jsx_calls: list[dict] = []
        self.scan_files: list = []
        self.scan_frontends: list[FrontendConfig] = []
        self.previous_sources: list = []
        self.backends: list[BackendConfig] = []
        self.visible: list[AbstractSet[str]] = []
        self.raise_in: dict[str, BaseException] = {}
        self.facts = FakeFacts()
        self.repo_files: RepoFiles | None = None
        self.py_symbols: list = []
        self.py_symbol_problems: list = []
        self.ts_decls: list = []
        self.ts_problems: list = []
        self.ts_error: BaseException | ToolchainProblem | None = None
        self.page_provider: doc_rules.PageProvider | None = None

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.raise_in:
            raise self.raise_in[name]

    def build_backend_model(
        self, repo_root: Path, backend: BackendConfig, sources: Any, visible: AbstractSet[str]
    ) -> FakeModel:
        self._count("build_backend_model")
        self.previous_sources.append(sources)
        self.backends.append(backend)
        self.visible.append(visible)
        return FakeModel(
            sources=f"sources-{self.calls['build_backend_model']}", serial=self.calls["build_backend_model"]
        )

    def build_di(self, model: Any) -> dict:
        self._count("build_di")
        return dict(self.di)

    def scan_frontend(self, repo_root: Path, frontend: FrontendConfig, tc: Toolchain, files: list) -> Any:
        self._count("scan_frontend")
        self.scan_files = [f.rel for f in files]
        self.scan_frontends.append(frontend)
        if not tc.available:
            assert tc.problem is not None
            return ("unavailable", tc.problem)
        return ("scan", len(files))

    def build_routes(self, model: Any, scan: Any) -> dict:
        self._count("build_routes")
        if scan[0] == "unavailable":
            # As `route_map.build_routes` does: every backend route is backend-only.
            backend = [m["backend"] for m in self.routes["matches"]] + list(self.routes["backend_only"])
            return dict(
                routes_result(backend_only=backend, complete=False, diagnostics=[frontend_unavailable_diagnostic()])
            )
        return dict(self.routes)

    def make_facts_cache(self, repo_root: Path, tc: Callable[[], Toolchain], clock: Callable[[], int]) -> FakeFacts:
        self._count("make_facts_cache")
        return self.facts

    def find_jsx_usage(self, component: str, root: Path, **kwargs: Any) -> list:
        self._count("find_jsx_usage")
        self.jsx_calls.append({"component": component, "root": root, **kwargs})
        kwargs["problems"].extend(self.jsx_problems)
        return list(self.usages)

    def make_symbol_index(self, repo_root: Path, facts: Any, config: Config, clock: Callable[[], int]) -> Any:
        self._count("make_symbol_index")
        self.symbol_configs.append(config)
        fake = self

        class Index:
            def resolve(self, name: str, *, side: str, kind_filter: str | None, file_hint: str | None) -> Any:
                fake._count("resolve")
                fake.symbol_calls.append(
                    {"name": name, "side": side, "kind_filter": kind_filter, "file_hint": file_hint}
                )
                if side == "frontend" and isinstance(fake.symbols, ToolchainProblem):
                    raise ToolchainUnavailable(fake.symbols)
                return fake.symbols

            def python_symbols(self, problems: list) -> list:
                fake._count("python_symbols")
                problems.extend(fake.py_symbol_problems)
                return list(fake.py_symbols)

            def typescript_decls(self, problems: list) -> list:
                fake._count("typescript_decls")
                if isinstance(fake.ts_error, ToolchainProblem):
                    raise ToolchainUnavailable(fake.ts_error)
                if fake.ts_error is not None:
                    raise fake.ts_error
                problems.extend(fake.ts_problems)
                return list(fake.ts_decls)

        return Index()

    def list_repo_files(self, repo_root: Path) -> RepoFiles:
        self._count("list_repo_files")
        return self.repo_files if self.repo_files is not None else repo_files_on_disk(repo_root)

    def make_doc_index(self, repo_root: Path, clock: Callable[[], int]) -> Any:
        self._count("make_doc_index")
        from code_intel import doc_refs

        return doc_refs.DocIndex(repo_root, clock=clock)

    def make_pages(
        self, repo_root: Path, config: Config, toolchain: Callable[[], Toolchain], clock: Callable[[], int]
    ) -> doc_rules.PageProvider:
        self._count("make_pages")
        return self.page_provider or doc_rules.NoPages(config.frontend.pages.public_dir)

    def analyzers(self) -> analysis_runtime.Analyzers:
        return analysis_runtime.Analyzers(
            build_backend_model=self.build_backend_model,
            build_di=self.build_di,
            scan_frontend=self.scan_frontend,
            build_routes=self.build_routes,
            make_facts_cache=self.make_facts_cache,
            find_jsx_usage=self.find_jsx_usage,
            make_symbol_index=self.make_symbol_index,
            list_repo_files=self.list_repo_files,
            make_doc_index=self.make_doc_index,
            make_pages=self.make_pages,
        )


# ── Repos and runtimes ───────────────────────────────────────────────────

# The layout every runtime fixture repo is configured with; the docs tables
# follow it (`config_toml`).
LAYOUT = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"
lock = "backend/uv.lock"
python_version_file = ".python-version"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"
lockfile = "frontend/package-lock.json"

[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiJson"

[symbols]
primary_prefixes = ["frontend/components/", "backend/service_backend/"]

"""

# Real-analyzer fixtures: the apiFetch/apiJson clients of frontend/lib/api.ts.
E2E_LAYOUT = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"
python_version_file = ".python-version"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"

[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiFetch"

[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiJson"

[[frontend.clients]]
kind = "call"
module = "frontend/lib/api.ts"
export = "apiJsonOr204"

[[frontend.clients]]
kind = "options"
module = "frontend/lib/utils/download-export.ts"
export = "downloadExport"

"""


DOC_CONFIG: dict[str, Any] = {
    "scope": ["docs/**/*.md"],
    "route_prefixes": ["/api"],
    "reachability": {"root": "docs/README.md", "include": ["docs/**/*.md"], "exclude": []},
    "rules": [],
    "ignore": [],
}

CONFIG_FILE = ".code-intel/config.toml"


def config_toml(layout: str = LAYOUT, **docs: Any) -> str:
    """A whole config: ``layout`` and the docs tables of ``DOC_CONFIG`` with ``docs`` overrides."""
    return layout + docs_toml({**DOC_CONFIG, **docs})


def write_config(root: Path, text: str, mtime: int = 1000) -> Path:
    return write(root / CONFIG_FILE, text, mtime)


def write_doc_config(root: Path, **overrides: Any) -> Path:
    """The fixture repo's config with every doc under ``docs/`` in scope,
    reachable from ``docs/README.md``, and ``overrides`` for the docs tables."""
    return write_config(root, config_toml(**overrides))


def make_repo(root: Path) -> Path:
    write_config(root, config_toml())
    write(root / ".python-version", "3.14\n")
    write(root / "backend" / "pyproject.toml", "[project]\nname = 'x'\n")
    write(root / "backend" / "service_backend" / "main.py", "app = None\n")
    write(root / "frontend" / "lib" / "api.ts", "export {};\n")
    write(root / "frontend" / "components" / "Page.tsx", "export const Page = () => <div/>;\n")
    write(root / "frontend" / "tsconfig.json", "{}\n")
    return root


class FixtureRuntime(analysis_runtime.AnalysisRuntime):
    """A runtime whose fake toolchain probe tests can switch: ``toolchain_state``
    holds ``available`` (read by every probe) and ``probes`` (their count)."""

    toolchain_state: dict[str, Any]


def make_runtime(
    repo: Path,
    fakes: FakeAnalyzers,
    *,
    toolchain_available: bool = True,
    version_info: tuple[int, int] | None = None,
    clock: Callable[[], int] | None = None,
    code_files: list[Path] | None = None,
    pin_files: Sequence[Path] = (),
) -> FixtureRuntime:
    if code_files is None:
        watched = write(repo / "tools" / "watched.py", "VERSION = 1\n")
        code_files = [watched]
    state = {"available": toolchain_available, "probes": 0}

    def find_toolchain(root: Path) -> Toolchain:
        state["probes"] += 1
        return toolchain(root, available=bool(state["available"]))

    rt = FixtureRuntime(
        repo,
        ConfigFile.locate(repo),
        code_files,
        tool_dir=repo / "tools",
        pin_files=pin_files,
        clock=clock or Clock(),
        find_toolchain=find_toolchain,
        analyzers=fakes.analyzers(),
        version_info=version_info,
    )
    rt.toolchain_state = state
    return rt


def doc_config_of(root: Path) -> DocRefsConfig:
    """The docs settings of the config written in ``root``."""
    return DocRefsConfig.from_config(load_config(root), root)


# ── Docs lookup (doc_refs) ───────────────────────────────────────

DOC_LOOKUP_ROUTE_FILE = "backend/service_backend/api/routes/r.py"
DOC_LOOKUP_OTHER_FILE = "backend/service_backend/other.py"

# docs/a.md names its handler file by path and by link, its route three ways
# (with the right method, without one, with the wrong one), a class defined
# in two files, a class attribute, a function, a local variable, a deleted
# file, a missing doc and its own heading. docs/plans/ is outside the orphan check.
DOC_LOOKUP_A = """\
# A

See `backend/service_backend/api/routes/r.py` and [r](../backend/service_backend/api/routes/r.py).

Call `POST /items/{id}/checkout` or `/items/{item_id}/checkout`, not `GET /items/{id}/checkout`.

The `Holder` class, `Holder.status`, `checkout()` and `local_x`.

Gone: `backend/service_backend/gone.py`. [Missing](nope.md) and [self](#a).
"""

DOC_LOOKUP_SYMBOLS: list[dict[str, Any]] = [
    {"name": "checkout", "kind": "function", "container": None, "file": DOC_LOOKUP_ROUTE_FILE, "line": 1},
    {
        "name": "local_x",
        "kind": "variable",
        "container": "checkout",
        "file": DOC_LOOKUP_ROUTE_FILE,
        "line": 2,
        "scope": "local",
    },
    {"name": "Holder", "kind": "class", "container": None, "file": DOC_LOOKUP_ROUTE_FILE, "line": 3},
    {
        "name": "status",
        "kind": "variable",
        "container": "Holder",
        "file": DOC_LOOKUP_ROUTE_FILE,
        "line": 4,
        "scope": "class",
    },
    {"name": "Holder", "kind": "class", "container": None, "file": DOC_LOOKUP_OTHER_FILE, "line": 1},
]


def doc_lookup_route() -> DiRoute:
    return di_route("checkout", "/items/{item_id}/checkout", method="POST", file=DOC_LOOKUP_ROUTE_FILE, line=1)


def doc_lookup_repo(root: Path) -> Path:
    """The docs and code the lookup tests query (see `DOC_LOOKUP_A`)."""
    write(root / DOC_LOOKUP_ROUTE_FILE, "def checkout():\n    local_x = 1\nclass Holder:\n    status = 1\n")
    write(root / DOC_LOOKUP_OTHER_FILE, "class Holder:\n    pass\n")
    write(root / "docs" / "README.md", "# Docs\n\n[A](a.md)\n[B](b.md)\n")
    write(root / "docs" / "a.md", DOC_LOOKUP_A)
    write(root / "docs" / "b.md", "# B\n\n[back](a.md#a)\n")
    write(root / "docs" / "plans" / "p.md", "# P\n\n[a](../a.md)\n")
    reachability = {"root": "docs/README.md", "include": ["docs/**/*.md"], "exclude": ["docs/plans/**"]}
    write_doc_config(root, reachability=reachability)
    return root


# docs/c.md names `status` of a second owner and Holder's `status` through a
# longer dotted name, so a bare `status` query spans two owners.
DOC_LOOKUP_C = "# C\n\n`Other.status` and `routes.r.Holder.status`.\n"
DOC_LOOKUP_OTHER_STATUS: dict[str, Any] = {
    "name": "status",
    "kind": "variable",
    "container": "Other",
    "file": DOC_LOOKUP_OTHER_FILE,
    "line": 4,
    "scope": "class",
}


def doc_lookup_two_owner_report(root: Path) -> Any:
    """`doc_lookup_report` plus docs/c.md and a second owner of ``status``."""
    from code_intel import doc_refs

    doc_lookup_repo(root)
    write(root / DOC_LOOKUP_OTHER_FILE, "class Holder:\n    pass\nclass Other:\n    status = 2\n")
    write(root / "docs" / "c.md", DOC_LOOKUP_C)
    return doc_refs.build_report(
        repo_files_on_disk(root),
        [doc_lookup_route()],
        [],
        [],
        [*DOC_LOOKUP_SYMBOLS, DOC_LOOKUP_OTHER_STATUS],
        True,
        doc_config_of(root),
    )


def doc_lookup_report(
    root: Path, *, route_limitations: Iterable[dict] = (), extra_problems: Sequence[problems.Problem] = ()
) -> Any:
    """A real `doc_refs.DocReport` over `doc_lookup_repo` (no git, no Node)."""
    from code_intel import doc_refs

    doc_lookup_repo(root)
    return doc_refs.build_report(
        repo_files_on_disk(root),
        [doc_lookup_route()],
        [],
        list(route_limitations),
        DOC_LOOKUP_SYMBOLS,
        True,
        doc_config_of(root),
        extra_problems,
    )
