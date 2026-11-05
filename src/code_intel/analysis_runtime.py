"""Analysis runtime for the MCP server: cache ownership, freshness and failures.

Owns every analysis cache: the backend model, the DI and routes results, the
frontend routes scan, the per-file TypeScript facts cache, the symbol index and
the docs check's per-file Markdown and code-word caches. Each cache is keyed by a
fingerprint of exactly the files it reads, so results refresh themselves after
any edit (racy-clean stamps are never trusted). Nothing here formats a response:
the tools in `server` read results from an `AnalysisRuntime` and hand them to
`presenters`.

The consumer's config is read again on every call and joins every fingerprint
by its content digest, so a config edit changes the next answer without a
restart. Every walk is limited to the git-visible files (`repo_files`), listed
once per call and shared by the fingerprints and the analyzers, so both agree
on which files exist and ignored build output is never read.

The analyzers are injected (`Analyzers`), so tests run the runtime against
fakes; `Analyzers.real()` binds this package's own modules.
"""

from __future__ import annotations

import hashlib
import posixpath
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_intel import code_paths, doc_rules, loaded_code, problems, ts_bridge
from code_intel import include_model as include_models
from code_intel.code_paths import GIT_CREATIONFLAGS as _GIT_CREATIONFLAGS
from code_intel.code_paths import GIT_ERRORS as _GIT_ERRORS
from code_intel.code_paths import FileStamp, SourceFile
from code_intel.config import BackendConfig, Config, ConfigError, ConfigFile, FrontendConfig, PagesConfig
from code_intel.repo_files import RepoFiles, RepoFilesError
from code_intel.ts_bridge import Toolchain, ToolchainProblem, ToolchainUnavailable

PACKAGE_DIR = loaded_code.PACKAGE_DIR

# The consumer's pin of this package. Its digest at start is compared on every
# reply: a bump after the server started means the client should restart it.
PIN_REL = ".code-intel/requirements.txt"

ANALYZERS = ("di_map", "route_map")

# Analyzer steps, named in `AnalysisFailed` messages so a failure says which step broke.
STEP_READ_CONFIG = "read config"
STEP_LIST_REPO_FILES = "list repository files"
STEP_WALK_BACKEND = "walk backend sources"
STEP_BUILD_GRAPH = "build router graph"
STEP_RESOLVE_DEPS = "resolve dependencies"
STEP_WALK_FRONTEND = "walk frontend sources"
STEP_FACTS_HELPER = "run typescript helper (facts mode)"
STEP_ROUTES_HELPER = "run typescript helper (routes mode)"
STEP_MATCH_ROUTES = "match routes"
STEP_RESOLVE_SYMBOL = "resolve symbol"
STEP_READ_DOC_CONFIG = "read docs config"
STEP_INDEX_DOC_SYMBOLS = "index symbols"
STEP_RESOLVE_DOC_REFS = "resolve doc references"
STEP_CHECK_AGENT_FILES = "check agent files"

# Resolving a caller-supplied root: OSError (a symlink loop, a permission
# error) or ValueError (an embedded NUL byte).
_RESOLVE_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)

# Codes for `FrontendProblem.code` that the toolchain probe does not produce.
HELPER_FAILED = "helper_failed"
FRONTEND_NOT_FOUND = "frontend_not_found"
HELPER_FAILED_FIX = ts_bridge.HELPER_FIX
# Helper modes (`ts_bridge.HelperMode`): facts serves JSX usage and frontend
# symbols, routes serves route matching.
FACTS_MODE = "facts"
ROUTES_MODE = "routes"


# ── Errors ───────────────────────────────────────────────────────────────


class AnalysisFailed(RuntimeError):
    """An analyzer raised; the message names the analyzer, the step, the repo root
    and the missing path when there is one."""

    def __init__(self, analyzer: str, step: str, message: str, missing: str | None = None) -> None:
        super().__init__(message)
        self.analyzer = analyzer
        self.step = step
        self.message = message
        self.missing = missing

    @classmethod
    def from_exception(cls, analyzer: str, step: str, exc: BaseException, repo_root: Path) -> AnalysisFailed:
        detail = exc.detail if isinstance(exc, ts_bridge.HelperError) else str(exc)
        missing = None
        filename = getattr(exc, "filename", None)
        if isinstance(exc, FileNotFoundError) and filename:
            missing = code_paths.rel_posix(Path(str(filename)), repo_root)
        return cls.build(analyzer, step, f"{type(exc).__name__}: {detail}", repo_root, missing)

    @classmethod
    def build(cls, analyzer: str, step: str, detail: str, repo_root: Path, missing: str | None) -> AnalysisFailed:
        message = f"{analyzer} failed at step '{step}': {detail}\n  repo root: {repo_root}"
        if missing:
            message = f"{message}\n  missing: {missing}"
        return cls(analyzer, step, message, missing)


class InvalidRoot(ValueError):
    """A JSX root that is not a directory inside the repository."""

    def __init__(self, root: str, message: str) -> None:
        super().__init__(message)
        self.root = root


@dataclass(frozen=True)
class FrontendProblem:
    """Why the frontend could not be analyzed, as a `frontend_unavailable` error
    reports it. ``code`` is a toolchain probe code (`ts_bridge.ProblemCode`),
    `HELPER_FAILED` or `FRONTEND_NOT_FOUND`."""

    code: str
    message: str  # names the missing path or the failed operation
    missing: str | None
    fix: str

    @classmethod
    def from_toolchain(cls, problem: ToolchainProblem) -> FrontendProblem:
        return cls(problem.code, problem.message, problem.missing, problem.fix)

    @classmethod
    def from_helper_error(cls, exc: ts_bridge.HelperError) -> FrontendProblem:
        return cls.from_toolchain(exc.problem())

    def fields(self) -> dict[str, Any]:
        """The `frontend_unavailable` error body fields (``message`` included)."""
        return {"message": self.message, "missing": self.missing, "fix": self.fix, "problem": self.code}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "missing": self.missing, "fix": self.fix}

    def as_diagnostic_fields(self) -> dict[str, Any]:
        """The ``message``, ``missing`` and ``fix`` keys a `frontend_unavailable` diagnostic carries."""
        return {"message": self.message, "missing": self.missing, "fix": self.fix}


class FrontendFailed(RuntimeError):
    """A frontend analysis could not run; ``problem`` says why and how to fix it."""

    def __init__(self, analyzer: str, problem: FrontendProblem) -> None:
        super().__init__(f"{analyzer}: {problem.message}")
        self.analyzer = analyzer
        self.problem = problem


class HelperFailed(FrontendFailed):
    """The TypeScript helper crashed while the toolchain probe reports it usable."""


class FrontendNotFound(FrontendFailed):
    """The checkout has no configured frontend root, so a frontend analysis has nothing to read."""


# Exceptions that already say what failed, so `_step` lets them through unwrapped.
_PASSTHROUGH: tuple[type[Exception], ...] = (AnalysisFailed, ToolchainUnavailable, InvalidRoot, FrontendFailed)


# ── Analyzers ────────────────────────────────────────────────────────────


def _real_backend_builder() -> Callable[[Path, BackendConfig, Any, AbstractSet[str]], Any]:
    """``build_backend_model`` that keeps one ``BackendBuilder`` per repo root and
    backend config, so a rebuild re-parses and re-analyzes only the modules whose
    files changed. No previous sources (the first build, or after a reset) or a
    changed backend config starts afresh."""
    builders: dict[tuple[Path, BackendConfig], Any] = {}

    def build(repo_root: Path, backend: BackendConfig, previous_sources: Any, visible: AbstractSet[str]) -> Any:
        from code_intel import router_graph

        key = (repo_root, backend)
        builder = builders.get(key)
        if builder is None or previous_sources is None or builder.sources is not previous_sources:
            builder = router_graph.BackendBuilder(
                repo_root / backend.root, backend.package, backend.app, repo_root, lock=backend.lock
            )
            builders.clear()
            builders[key] = builder
        return builder.build(visible)

    return build


def _real_build_di(model: Any) -> dict:
    from code_intel import di_map

    return dict(di_map.build_di(model))


def _real_scan_frontend(
    repo_root: Path, frontend: FrontendConfig, toolchain: Toolchain, files: list[SourceFile]
) -> Any:
    from code_intel import route_map

    return route_map.scan_frontend(repo_root, frontend, toolchain, files)


def _real_build_routes(model: Any, scan: Any) -> dict:
    from code_intel import route_map

    return dict(route_map.build_routes(model, scan))


def _real_facts_cache(repo_root: Path, toolchain: Callable[[], Toolchain], clock: Callable[[], int]) -> Any:
    from code_intel import ts_facts

    return ts_facts.FactsCache(repo_root, toolchain, clock=clock)


def _real_find_jsx_usage(component: str, root: Path, **kwargs: Any) -> list:
    from code_intel import jsx_usage

    try:
        return jsx_usage.find_jsx_usage(component, root, **kwargs)
    except jsx_usage.JsxRootError as exc:
        raise InvalidRoot(str(root), str(exc)) from exc


def _real_symbol_index(repo_root: Path, facts: Any, config: Config, clock: Callable[[], int]) -> Any:
    from code_intel import symbol_resolve

    return symbol_resolve.SymbolIndex(repo_root, facts, symbol_resolve.SymbolLayout.from_config(config), clock=clock)


def _real_list_repo_files(repo_root: Path) -> RepoFiles:
    from code_intel import repo_files

    return repo_files.list_repo_files(repo_root)


def _real_doc_index(repo_root: Path, clock: Callable[[], int]) -> Any:
    from code_intel import doc_refs

    return doc_refs.DocIndex(repo_root, clock=clock)


def _real_page_provider(
    repo_root: Path, config: Config, toolchain: Callable[[], Toolchain], clock: Callable[[], int]
) -> doc_rules.PageProvider:
    from code_intel import pages

    return pages.page_provider(repo_root, config, toolchain, clock)


@dataclass(frozen=True)
class Analyzers:
    """The analyzer entry points the runtime calls.

    - ``build_backend_model(repo_root, backend, previous_sources, visible) ->
      BackendModel``; the real one is stateful (see ``_real_backend_builder``)
    - ``build_di(model) -> DiResult``
    - ``scan_frontend(repo_root, frontend, toolchain, files) -> FrontendScan | FrontendUnavailable``
    - ``build_routes(model, scan) -> RoutesResult``
    - ``make_facts_cache(repo_root, toolchain_fn, clock) -> FactsCache``
    - ``find_jsx_usage(component, root, *, facts, repo_root, frontend_root,
      include_self_close, include_tests, problems, visible) -> list[JsxUsage]``
    - ``make_symbol_index(repo_root, facts, config, clock) -> SymbolIndex``
    - ``list_repo_files(repo_root) -> RepoFiles``; raises ``RepoFilesError``
    - ``make_doc_index(repo_root, clock) -> DocIndex``
    - ``make_pages(repo_root, config, toolchain_fn, clock) -> PageProvider``: the frontend's pages for the docs
    """

    build_backend_model: Callable[[Path, BackendConfig, Any, AbstractSet[str]], Any] = field(
        default_factory=_real_backend_builder
    )
    build_di: Callable[[Any], dict] = _real_build_di
    scan_frontend: Callable[[Path, FrontendConfig, Toolchain, list[SourceFile]], Any] = _real_scan_frontend
    build_routes: Callable[[Any, Any], dict] = _real_build_routes
    make_facts_cache: Callable[[Path, Callable[[], Toolchain], Callable[[], int]], Any] = _real_facts_cache
    find_jsx_usage: Callable[..., list] = _real_find_jsx_usage
    make_symbol_index: Callable[[Path, Any, Config, Callable[[], int]], Any] = _real_symbol_index
    list_repo_files: Callable[[Path], RepoFiles] = _real_list_repo_files
    make_doc_index: Callable[[Path, Callable[[], int]], Any] = _real_doc_index
    make_pages: Callable[[Path, Config, Callable[[], Toolchain], Callable[[], int]], doc_rules.PageProvider] = (
        _real_page_provider
    )

    @classmethod
    def real(cls) -> Analyzers:
        return cls()


# ── Analyzer code staleness ──────────────────────────────────────────────


def watched_files(package_dir: Path = PACKAGE_DIR) -> list[Path]:
    """Every module file of the package, resolved: the ones loaded are compared
    with the bytes they were compiled from (`loaded_code.IMPORTED`)."""
    return sorted({path.resolve() for path in package_dir.rglob("*.py")})


def stat_signature(paths: Sequence[Path]) -> tuple[FileStamp | None, ...]:
    """Each file's stamp, None when it cannot be stat'ed. The stamp carries
    ctime, so a rewrite whose mtime was set back still changes it."""
    sig: list[FileStamp | None] = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            sig.append(None)
        else:
            sig.append(FileStamp.from_stat(st))
    return tuple(sig)


def content_digest(paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for path in paths:
        h.update(str(path).encode())
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"\0missing")
    return h.hexdigest()


def source_digest(path: Path) -> str | None:
    """Hex SHA-256 of a file's bytes, None when it cannot be read. The import
    recorder stores the same digest of the bytes it compiled."""
    try:
        return loaded_code.digest(path.read_bytes())
    except OSError:
        return None


@dataclass(frozen=True)
class CodeState:
    """How the server's loaded code compares with its files on disk.

    ``changed`` files differ from the code that was loaded, or are a pin file
    that changed since the server started. ``unconfirmed`` files were loaded
    without a record of their bytes and modified so close to that load that the
    bytes cannot be told apart from the ones on disk now."""

    changed: tuple[Path, ...] = ()
    unconfirmed: tuple[Path, ...] = ()

    @property
    def stale(self) -> bool | None:
        """True on a confirmed change, None when only unconfirmed files remain."""
        if self.changed:
            return True
        return None if self.unconfirmed else False


def _loaded_module_files() -> set[Path]:
    """The resolved files of every module in ``sys.modules``."""
    found: set[Path] = set()
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            found.add(Path(filename).resolve())
        except _RESOLVE_ERRORS:
            continue
    return found


class CodeWatch:
    """Compares the server's own sources on disk with the code it loaded, and
    its pin files with their content when the watch started.

    The modules are imported once, so after an edit or a pin bump the server
    keeps running the old code. ``imported`` maps each module file to the
    digest of the bytes it was compiled from, recorded as it imports (analyzers
    import lazily, so the mapping grows); that digest is exact. A file without
    one is compared with its content when the watch started. When ``imported``
    is given, a file that has no record and no module in ``sys.modules`` has not
    been loaded yet, so nothing old runs from it; without ``imported`` every
    file counts as loaded. ``pins`` (the consumer's requirements file) are
    never loaded as code: their content at start is the reference, exactly,
    and one that appears or disappears later is a change.

    Replies are rendered on the MCP SDK's worker threads, so ``state`` holds a
    lock of its own (the runtime's lock is not held while rendering)."""

    RACY_WINDOW_NS = code_paths.RACY_WINDOW_NS

    def __init__(
        self,
        files: Sequence[Path],
        *,
        pins: Sequence[Path] = (),
        clock: Callable[[], int] = time.time_ns,
        loaded_at_ns: int | None = None,
        imported: Mapping[Path, str] | None = None,
    ) -> None:
        self.modules = tuple(files)
        self.pins = tuple(pins)
        self.files = [*self.modules, *self.pins]
        self._clock = clock
        self._imported = imported
        self._lock = threading.Lock()
        self._started = {path: source_digest(path) for path in self.files}
        sig = stat_signature(self.files)
        self._remember_stat(sig, clock())
        # A module loaded without a record was read before the digest above:
        # when it was modified within the racy window of that load, the bytes
        # loaded may differ from the ones hashed, and nothing can tell which.
        self._racy_at_load = frozenset(
            path
            for path, stamp in zip(self.modules, sig[: len(self.modules)], strict=True)
            if loaded_at_ns is not None and stamp is not None and stamp.changed_ns >= loaded_at_ns - self.RACY_WINDOW_NS
        )
        self._state: CodeState | None = None
        # The import record the cached state was computed from: a reload
        # replaces a digest without changing how many there are.
        self._imported_seen: dict[Path, str] | None = None

    def _remember_stat(self, sig: tuple[FileStamp | None, ...], now: int) -> None:
        self._stat = sig
        newest = max((s.changed_ns for s in sig if s), default=0)
        # Git's racy-clean rule: a file modified this close to the snapshot can
        # change again within the same timestamp tick, so its stat proves nothing
        # until a check after the window confirms the content.
        self._racy_until = newest + self.RACY_WINDOW_NS if newest >= now - self.RACY_WINDOW_NS else 0

    def _reference(self, path: Path, loaded: set[Path] | None) -> tuple[str | None, bool] | None:
        """(digest of the loaded bytes, exact), or None when the file is not loaded."""
        if self._imported is not None:
            recorded = self._imported.get(path)
            if recorded is not None:
                return recorded, True
            if loaded is not None and path not in loaded:
                return None
        return self._started[path], False

    def _compare(self) -> CodeState:
        changed: list[Path] = []
        unconfirmed: list[Path] = []
        loaded = _loaded_module_files() if self._imported is not None else None
        for path in self.modules:
            reference = self._reference(path, loaded)
            if reference is None:
                continue
            digest, exact = reference
            if source_digest(path) != digest:
                changed.append(path)
            elif not exact and path in self._racy_at_load:
                unconfirmed.append(path)
        changed.extend(path for path in self.pins if source_digest(path) != self._started[path])
        return CodeState(tuple(changed), tuple(unconfirmed))

    def state(self) -> CodeState:
        """The comparison, recomputed when a stat changes, a module imports, or
        while a stat is too recent to trust. A touched file whose content
        matches is remembered, so later calls compare stat only."""
        with self._lock:
            sig = stat_signature(self.files)
            imported = None if self._imported is None else dict(self._imported)
            now = self._clock()
            if (
                self._state is not None
                and sig == self._stat
                and imported == self._imported_seen
                and not self._racy_until
            ):
                return self._state
            state = self._compare()
            if sig != self._stat:
                self._remember_stat(sig, now)
            elif self._racy_until and now > self._racy_until:
                self._racy_until = 0
            self._state, self._imported_seen = state, imported
            return state

    def stale(self) -> bool | None:
        """`CodeState.stale` for the current files."""
        return self.state().stale


# ── Results ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JsxResult:
    usages: list
    problems: list  # problems.Problem, from the walk and the helper


@dataclass(frozen=True)
class DocReportResult:
    """`AnalysisRuntime.get_doc_report`: the report and, from the same run,
    what it lacks on the frontend side (``frontend_problems``): a
    `frontend_unavailable` diagnostic when frontend definitions were not
    indexed, and the TypeScript facts' own diagnostics. They are not part of
    the report, because the check never depends on them; a lookup whose
    answer needs frontend definition locations adds them to its own analysis."""

    report: Any  # doc_refs.DocReport
    frontend_problems: list  # problems.Problem


@dataclass(frozen=True)
class RefreshResult:
    dependency_function_count: int
    di_route_count: int
    routes_summary: dict
    complete: bool
    diagnostics: list[dict]
    limitations: list[dict]
    # Why routes have no frontend; its counts are then meaningless.
    frontend_problem: FrontendProblem | None = None

    @property
    def analysis(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "diagnostics": self.diagnostics,
            "analysis_limitations": self.limitations,
        }


@dataclass(frozen=True)
class StatusSnapshot:
    repo_root: str
    tool_dir: str
    git_sha: str | None
    # True / False, or None while an analyzer has not run on the current sources.
    complete: bool | None
    # True on a confirmed change, None when `code_unconfirmed` lists files whose
    # loaded bytes cannot be compared with the disk.
    code_stale: bool | None
    python: str
    executable: str
    # None when the config cannot be read, so the floor is unknown.
    runtime_compatible: bool | None
    python_running: str
    python_required: str | None
    diagnostics: list[dict]
    limitations: list[dict]
    analyzers: dict[str, dict]
    cache: dict[str, Any]
    toolchain: dict[str, Any]
    counts: dict[str, Any] | None
    counts_error: dict[str, Any] | None = None
    counts_errors: dict[str, dict] = field(default_factory=dict)
    code_unconfirmed: list[dict[str, str]] = field(default_factory=list)
    # The FastAPI version the configured lock pins and the include_router model
    # the backend analysis applies for it (include_model.app_block's fields).
    fastapi: dict[str, Any] = field(default_factory=dict)
    # ``{"path": <config file>, "error": <why it cannot be used> | None}``.
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def analysis(self) -> dict[str, Any]:
        return {"complete": self.complete, "diagnostics": self.diagnostics, "analysis_limitations": self.limitations}


@dataclass(frozen=True)
class _Fingerprint:
    digest: str
    racy: bool
    files: tuple[SourceFile, ...] = ()
    present: bool = True  # False when the walked root does not exist
    # The files whose stamps were too recent to trust, and their content
    # digest when the fingerprint was taken: once the window has passed, equal
    # content confirms a cache built from them (git's racy-clean check).
    racy_paths: tuple[Path, ...] = ()
    racy_digest: str | None = None


def settled(built: _Fingerprint, current: _Fingerprint) -> bool:
    """True when a cache built under ``built`` still describes the sources
    ``current`` was taken from: equal stamps, and for files that were racy at
    build time, a window that has passed and unchanged content."""
    if built.digest != current.digest:
        return False
    if not built.racy:
        return True
    if current.racy:
        return False
    return content_digest(built.racy_paths) == built.racy_digest


@dataclass(frozen=True)
class _LoadedConfig:
    config: Config
    digest: str


def _dedupe(rows: Iterable[Mapping]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        key = tuple(sorted((k, repr(v)) for k, v in row.items()))
        if key not in seen:
            seen.add(key)
            out.append(dict(row))
    return out


def _as_problem(row: Mapping[str, Any]) -> problems.Problem:
    """An analyzer's JSON problem row (``Problem.as_dict``) as a `problems.Problem`."""
    return problems.Problem(
        kind=str(row.get("kind")),
        category=row.get("category"),
        stage=str(row.get("stage") or ""),
        step=row.get("step"),
        file=row.get("file"),
        line=row.get("line"),
        message=str(row.get("message") or ""),
        exception=row.get("exception"),
        likely_cause=row.get("likely_cause"),
        missing=row.get("missing"),
        fix=row.get("fix"),
        problem=row.get("problem"),
    )


def _normalize_rel(rel: str) -> str:
    """``rel`` without ``./`` steps, so ``./pyproject.toml`` (a backend root of
    ``.``) is the path git and the walks name."""
    return posixpath.normpath(rel)


def toolchain_status(
    toolchain: Toolchain,
    repo_root: Path,
    facts_stats: Mapping | None,
    helper_failures: Mapping[str, FrontendProblem] | None = None,
) -> dict[str, Any]:
    """The `status` toolchain block. A usable toolchain whose last helper
    run in some mode crashed is ``helper_failed``, not ``ok``: the probe alone
    cannot see it. ``helper_failures`` maps each helper mode whose last run
    crashed to its problem; ``problem`` is the first of them by mode name."""
    ts_dir = code_paths.rel_posix(toolchain.typescript_dir, repo_root) if toolchain.typescript_dir else None
    failures = dict(sorted((helper_failures or {}).items()))
    problem: FrontendProblem | None = None
    if not toolchain.available:
        problem = FrontendProblem.from_toolchain(ts_bridge.incomplete_toolchain_problem(toolchain))
        state = "unavailable"
    elif failures:
        state, problem = "helper_failed", next(iter(failures.values()))
    else:
        state = "ok"
    return {
        "state": state,
        "node": {"path": toolchain.node, "version": toolchain.node_version},
        "typescript": {
            "dir": ts_dir,
            "version": toolchain.typescript_version,
            "source": toolchain.typescript_source,
            "lockfile_version": toolchain.lockfile_typescript_version,
        },
        "problem": None if problem is None else problem.as_dict(),
        "helper_failures": {mode: failure.as_dict() for mode, failure in failures.items()},
        "warnings": list(toolchain.warnings),
        "facts_cache": dict(facts_stats) if facts_stats is not None else None,
    }


def routes_frontend_problem(diag: Mapping, toolchain: Toolchain, *, frontend_present: bool) -> FrontendProblem:
    """The cause behind a routes result's `frontend_unavailable` diagnostic.

    The routes scan reports a missing frontend root, an unusable toolchain and
    a crashed helper with the same diagnostic kind, so the cause comes from what
    the runtime knew when it started the scan, checked in the scan's own order:
    an explicit ``problem`` code on the diagnostic, then a missing frontend
    root, then the toolchain probe. With a frontend and a usable toolchain,
    only a helper failure remains.
    """
    fix = str(diag.get("fix") or ts_bridge.FRONTEND_TS_FIX)
    message = problems.strip_fix(
        str(diag.get("message") or f"route_map: frontend_unavailable without a message: {diag!r}"), fix
    )
    missing = diag.get("missing")
    if diag.get("problem"):
        return FrontendProblem(str(diag["problem"]), message, missing, fix)
    if not frontend_present:
        return FrontendProblem(FRONTEND_NOT_FOUND, message, missing, fix)
    if not toolchain.available:
        return FrontendProblem.from_toolchain(ts_bridge.incomplete_toolchain_problem(toolchain))
    return FrontendProblem(HELPER_FAILED, message, missing, fix)


# ── Runtime ──────────────────────────────────────────────────────────────


class AnalysisRuntime:
    """Cached backend, DI, routes, JSX and symbol analysis for one repo checkout.

    The MCP SDK runs sync tools on worker threads, so two calls can run at
    once; the lock serializes every cache read and build.
    """

    def __init__(
        self,
        repo_root: Path,
        config_file: ConfigFile,
        code_files: Sequence[Path],
        *,
        tool_dir: Path = PACKAGE_DIR,
        pin_files: Sequence[Path] = (),
        clock: Callable[[], int] = time.time_ns,
        find_toolchain: Callable[[Path], Toolchain] = ts_bridge.find_toolchain,
        analyzers: Analyzers | None = None,
        version_info: tuple[int, int] | None = None,
        code_loaded_at_ns: int | None = None,
        code_imported: Mapping[Path, str] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config_file = config_file
        self.tool_dir = tool_dir
        self.analyzers = analyzers or Analyzers.real()
        self._clock = clock
        self._find_toolchain = find_toolchain
        self._lock = threading.RLock()
        # The facts cache probes the toolchain from worker threads (symbol
        # search runs its TypeScript side beside the Python index) while the
        # calling thread holds `_lock` and waits for them, so the probe has its
        # own lock and never waits on `_lock`.
        self._toolchain_lock = threading.Lock()

        self._config: _LoadedConfig | None = None

        self._toolchain: Toolchain | None = None
        self._toolchain_identity: tuple[tuple[str | None, str | None], FileStamp | None, str | None] | None = None
        self._facts: Any = None
        self._symbols: Any = None
        # The config the symbol index was made for: its layout comes from it.
        self._symbols_config: Config | None = None

        self._model: Any = None
        self._model_key: _Fingerprint | None = None
        self._di: dict | None = None
        self._di_model: Any = None
        # The backend digest DI was built from: a racy rebuild replaces the model
        # object without changing the sources DI describes.
        self._di_digest: str | None = None
        self._routes: dict | None = None
        self._routes_key: tuple | None = None
        # The (backend, frontend) fingerprints the cached routes were built under.
        self._routes_built: tuple[_Fingerprint, _Fingerprint] | None = None
        # The routes helper is the slow half of a routes build, so its scan has
        # its own key: a backend-only edit rematches against the cached scan.
        self._scan: Any = None
        self._scan_key: tuple | None = None
        self._scan_built: _Fingerprint | None = None
        # Why the cached routes result has no frontend, when it has none.
        self._routes_frontend: FrontendProblem | None = None
        # The last helper crash per helper mode (`ts_bridge.HelperMode`), until
        # a run in that mode succeeds; status reports them because the
        # toolchain probe never runs the helper. The modes serve different
        # tools, so a success in one says nothing about the other.
        self._helper_failures: dict[str, FrontendProblem] = {}
        # The docs check's per-file Markdown facts and code words; created on first use.
        self._doc_index: Any = None
        # The frontend page provider, made for the frontend root and pages table it was built from.
        self._pages: Any = None
        self._pages_key: tuple[str, PagesConfig] | None = None

        self.last_errors: dict[str, str] = {}
        self.failed_steps: dict[str, str | None] = {}
        self.last_success_at: dict[str, float] = {}
        self.last_runtime_ms: dict[str, int] = {}
        # not_run | complete | incomplete | failed; absent means not_run.
        self.last_state: dict[str, str] = {}

        running = tuple(sys.version_info[:2]) if version_info is None else tuple(version_info[:2])
        self.python_running: tuple[int, ...] = running

        self.code_watch = CodeWatch(
            code_files, pins=pin_files, clock=clock, loaded_at_ns=code_loaded_at_ns, imported=code_imported
        )

    # ── Config ───────────────────────────────────────────────────────────

    def config(self, analyzer: str = "config") -> Config:
        """The config as the file holds it now. Its bytes are read on every
        call (a small file) and parsed again only when their digest changes, so
        an edit reaches the next answer and a racy stamp can never hide one.
        Raises `AnalysisFailed` (step ``read config``) naming ``analyzer``."""
        with self._lock, self._step(analyzer, STEP_READ_CONFIG):
            try:
                data = self.config_file.read_bytes()
                digest = loaded_code.digest(data)
                if self._config is None or self._config.digest != digest:
                    self._config = _LoadedConfig(self.config_file.parse(data), digest)
            except ConfigError as exc:
                missing = self.config_file.display if isinstance(exc.__cause__, FileNotFoundError) else None
                raise AnalysisFailed.build(analyzer, STEP_READ_CONFIG, str(exc), self.repo_root, missing) from exc
            return self._config.config

    def _config_digest(self) -> str:
        """The digest of the config `config()` last read; call `config()` first."""
        assert self._config is not None
        return self._config.digest

    # ── Code and interpreter ─────────────────────────────────────────────

    def code_state(self) -> CodeState:
        return self.code_watch.state()

    def code_unconfirmed(self, state: CodeState) -> list[dict[str, str]]:
        """``state``'s unconfirmed files as ``{"file", "reason"}``, the file repo-relative."""
        window_s = CodeWatch.RACY_WINDOW_NS / 1_000_000_000
        reason = (
            f"modified within {window_s:g} s of the server loading it, "
            "so the bytes it loaded cannot be compared with the file on disk"
        )
        return [{"file": code_paths.rel_posix(path, self.repo_root), "reason": reason} for path in state.unconfirmed]

    def min_python(self) -> tuple[int, int]:
        """The backend's Python floor from its configured version file, read on
        every call: a bump applies to the next answer."""
        version_file = self.config().backend.python_version_file
        return problems.min_python_version(self.repo_root / version_file if version_file else None)

    def runtime_compatible(self) -> bool:
        """Whether this interpreter parses everything the backend may contain;
        analysis is refused up front instead of returning results that miss files."""
        return problems.runtime_is_compatible(self.min_python(), self.python_running)

    def runtime_versions(self) -> tuple[str, str]:
        """(running, required) as ``major.minor`` strings for messaging."""
        return "{}.{}".format(*self.python_running[:2]), "{}.{}".format(*self.min_python())

    def git_sha(self) -> str | None:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                creationflags=_GIT_CREATIONFLAGS,
            )
        except _GIT_ERRORS:
            return None
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None

    # ── Toolchain ────────────────────────────────────────────────────────

    def toolchain(self) -> Toolchain:
        """The probed toolchain, re-probed on every call while unavailable and
        whenever the node it would choose, the TypeScript package.json or the
        compiler file it loads changes, so a removed, replaced or newly
        shadowing node, or a same-version reinstall, reaches the caches."""
        with self._toolchain_lock:
            cached = self._toolchain
            if cached is not None and cached.available and cached.typescript_dir is not None:
                if self._identity(cached.typescript_dir) == self._toolchain_identity:
                    return cached
            probed = self._find_toolchain(self.repo_root)
            self._toolchain = probed
            self._toolchain_identity = self._identity(probed.typescript_dir) if probed.typescript_dir else None
            return probed

    def _identity(self, ts_dir: Path) -> tuple[tuple[str | None, str | None], FileStamp | None, str | None]:
        """Stat-only facts that change when a probe would answer differently;
        `node --version` itself is cached by the binary's stamp, so a re-probe
        after a change runs node only when the binary is new."""
        try:
            package = FileStamp.from_stat((ts_dir / "package.json").stat())
        except OSError:
            package = None
        return ts_bridge.node_identity(self.repo_root), package, ts_bridge.compiler_stamp(ts_dir)

    def _toolchain_key(self, toolchain: Toolchain) -> tuple[str, ...]:
        if toolchain.available:
            return ("ok", *toolchain.stamp())
        problem = toolchain.problem
        return ("unavailable", problem.code if problem else "", str(problem.missing if problem else ""))

    def _facts_cache(self) -> Any:
        if self._facts is None:
            self._facts = self.analyzers.make_facts_cache(self.repo_root, self.toolchain, self._clock)
        return self._facts

    def _symbol_index(self, config: Config) -> Any:
        """The symbol index for ``config``'s layout; a config edit replaces it
        (the facts cache, which does not depend on the config, is kept)."""
        if self._symbols is None or self._symbols_config != config:
            self._symbols = self.analyzers.make_symbol_index(self.repo_root, self._facts_cache(), config, self._clock)
            self._symbols_config = config
        return self._symbols

    def facts_cache(self) -> Any:
        """The per-file TypeScript facts cache every frontend analysis here shares."""
        with self._lock:
            return self._facts_cache()

    def symbol_index(self) -> Any:
        """The symbol index for the current config, sharing `facts_cache`."""
        with self._lock:
            return self._symbol_index(self.config("symbol_resolve"))

    # ── Steps and bookkeeping ────────────────────────────────────────────

    @contextmanager
    def _step(self, analyzer: str, step: str) -> Iterator[None]:
        try:
            yield
        except _PASSTHROUGH:
            raise
        except Exception as exc:
            raise AnalysisFailed.from_exception(analyzer, step, exc, self.repo_root) from exc

    def _helper_failed(self, analyzer: str, exc: ts_bridge.HelperError) -> HelperFailed:
        problem = FrontendProblem.from_helper_error(exc)
        self._helper_failures[exc.mode] = problem
        return HelperFailed(analyzer, problem)

    def _record_success(self, name: str, started: float, complete: bool) -> None:
        self.last_runtime_ms[name] = round((time.perf_counter() - started) * 1000)
        self.last_success_at[name] = time.time()
        self.last_errors.pop(name, None)
        self.last_state[name] = "complete" if complete else "incomplete"

    def _record_failure(self, name: str, exc: BaseException) -> None:
        self.last_errors[name] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        self.failed_steps[name] = exc.step if isinstance(exc, AnalysisFailed) else None
        self.last_state[name] = "failed"

    def _repo_files(self, analyzer: str) -> RepoFiles:
        """The git-visible files; a git failure is `AnalysisFailed` at step
        ``list repository files``. Its message already carries git's stderr
        and the fix, so the exception type name would only add noise."""
        with self._step(analyzer, STEP_LIST_REPO_FILES):
            try:
                return self.analyzers.list_repo_files(self.repo_root)
            except RepoFilesError as exc:
                raise AnalysisFailed.build(analyzer, STEP_LIST_REPO_FILES, str(exc), self.repo_root, None) from exc

    # ── Fingerprints ─────────────────────────────────────────────────────

    def _extra_stamp(self, rel: str) -> FileStamp | None:
        """None when the file cannot be stat'ed: the digest records it as absent,
        so its appearance later changes the fingerprint."""
        try:
            return FileStamp.from_stat((self.repo_root / rel).stat())
        except OSError:
            return None

    def _fingerprint(
        self,
        root: Path,
        suffixes: tuple[str, ...],
        extras: Iterable[str],
        stage: str,
        visible: AbstractSet[str],
        generated_parent: Path | None = None,
    ) -> _Fingerprint:
        walk_problems: list[problems.Problem] = []
        files = code_paths.iter_source_files(
            root,
            suffixes,
            self.repo_root,
            walk_problems,
            stage=stage,
            generated_parent=generated_parent,
            visible=visible,
        )
        stamps = [(rel, self._extra_stamp(rel)) for rel in dict.fromkeys(_normalize_rel(rel) for rel in extras)]
        extra = "\n".join(f"{rel}\0{stamp}" for rel, stamp in stamps)
        now = self._clock()
        racy_paths = tuple(
            [f.path for f in files if code_paths.is_racy(f.stamp, now)]
            + [self.repo_root / rel for rel, stamp in stamps if stamp is not None and code_paths.is_racy(stamp, now)]
        )
        racy = bool(racy_paths)
        digest = hashlib.sha256(usedforsecurity=False)
        # A config edit can change what the analyzers do with the same files.
        digest.update(self._config_digest().encode())
        digest.update(code_paths.fingerprint(files).encode())
        digest.update(extra.encode("utf-8", "surrogateescape"))
        # A walk problem means files are missing from the digest; when it clears,
        # the digest changes.
        digest.update(repr(sorted(p.message for p in walk_problems)).encode("utf-8", "surrogateescape"))
        racy_digest = content_digest(racy_paths) if racy else None
        return _Fingerprint(digest.hexdigest(), racy, tuple(files), racy_paths=racy_paths, racy_digest=racy_digest)

    def _backend_fingerprint(self, analyzer: str, config: Config, visible: AbstractSet[str]) -> _Fingerprint:
        backend = config.backend
        package = self.repo_root / backend.root / backend.package.replace(".", "/")
        with self._step(analyzer, STEP_WALK_BACKEND):
            if not package.is_dir():
                rel = code_paths.rel_posix(package, self.repo_root)
                raise AnalysisFailed.build(
                    analyzer,
                    STEP_WALK_BACKEND,
                    f"backend package directory {rel} not found",
                    self.repo_root,
                    rel,
                )
            # The lock decides the include_router model (include_model.py), the
            # version file whether the backend may be analyzed at all.
            extras = [posixpath.join(backend.root, "pyproject.toml")]
            extras += [rel for rel in (backend.python_version_file, backend.lock) if rel]
            return self._fingerprint(package, code_paths.PY_SUFFIXES, extras, "fingerprint backend sources", visible)

    def _frontend_fingerprint(self, analyzer: str, config: Config, visible: AbstractSet[str]) -> _Fingerprint:
        frontend = config.frontend
        root = self.repo_root / frontend.root
        with self._step(analyzer, STEP_WALK_FRONTEND):
            if not root.is_dir():
                return _Fingerprint(f"missing:{self._config_digest()}", False, (), present=False)
            return self._fingerprint(
                root,
                code_paths.TS_SUFFIXES,
                [rel for rel in (frontend.tsconfig, frontend.lockfile) if rel],
                "fingerprint frontend sources",
                visible,
                # The same walk the routes scan does, so build output is never scanned.
                generated_parent=root,
            )

    # ── Backend model and DI ─────────────────────────────────────────────

    def _backend_model(self, analyzer: str, config: Config, key: _Fingerprint, visible: AbstractSet[str]) -> Any:
        if self._model is not None and self._model_key is not None:
            if settled(self._model_key, key):
                self._model_key = key
                return self._model
        previous = getattr(self._model, "sources", None)
        with self._step(analyzer, STEP_BUILD_GRAPH):
            model = self.analyzers.build_backend_model(self.repo_root, config.backend, previous, visible)
        self._model, self._model_key = model, key
        return model

    def get_backend_model(self) -> Any:
        with self._lock:
            config = self.config("di_map")
            visible = self._repo_files("di_map").files
            return self._backend_model("di_map", config, self._backend_fingerprint("di_map", config, visible), visible)

    def get_di(self) -> dict:
        with self._lock:
            return self._get_di(None)

    def _get_di(self, visible: AbstractSet[str] | None) -> dict:
        """`get_di`, reusing ``visible`` when the caller already listed the files."""
        started = time.perf_counter()
        try:
            config = self.config("di_map")
            if visible is None:
                visible = self._repo_files("di_map").files
            key = self._backend_fingerprint("di_map", config, visible)
            model = self._backend_model("di_map", config, key, visible)
            if self._di is not None and self._di_model is model:
                return self._di
            with self._step("di_map", STEP_RESOLVE_DEPS):
                result = self.analyzers.build_di(model)
        except Exception as exc:
            self._record_failure("di_map", exc)
            raise
        self._di, self._di_model, self._di_digest = result, model, key.digest
        self._record_success("di_map", started, complete=bool(result.get("complete")))
        return result

    # ── Routes ───────────────────────────────────────────────────────────

    def _routes_cache_key(self, backend: _Fingerprint, frontend: _Fingerprint, toolchain: Toolchain) -> tuple:
        return (backend.digest, *self._scan_cache_key(frontend, toolchain))

    def _scan_cache_key(self, frontend: _Fingerprint, toolchain: Toolchain) -> tuple:
        return (frontend.digest, self._toolchain_key(toolchain))

    def _frontend_scan(
        self,
        config: Config,
        backend: _Fingerprint,
        frontend: _Fingerprint,
        toolchain: Toolchain,
        visible: AbstractSet[str],
    ) -> tuple[Any, Any, bool]:
        """(model, scan, scanned). Reuses the cached scan while its sources and
        the toolchain are unchanged; otherwise the helper runs in a worker
        thread while this thread builds (or reuses) the backend model.

        A missing frontend or an unusable toolchain is cached like a scan: the
        key changes when either is fixed. A raised error never reaches the
        cache, and `get_routes` drops a crashed-helper scan."""
        key = self._scan_cache_key(frontend, toolchain)
        built = self._scan_built
        if self._scan is not None and self._scan_key == key and built is not None and settled(built, frontend):
            return self._backend_model("route_map", config, backend, visible), self._scan, False
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.analyzers.scan_frontend, self.repo_root, config.frontend, toolchain, list(frontend.files)
            )
            model = self._backend_model("route_map", config, backend, visible)
            with self._step("route_map", STEP_ROUTES_HELPER):
                scan = future.result()
        self._scan, self._scan_key, self._scan_built = scan, key, frontend
        return model, scan, True

    def get_routes(self) -> dict:
        """Routes JSON v2. The TypeScript helper runs in a worker thread while this
        thread builds (or reuses) the backend model, so a cold call costs about
        the slower of the two plus matching. After a backend-only edit the
        cached frontend scan is reused, so only the model and matching rerun."""
        with self._lock:
            started = time.perf_counter()
            try:
                config = self.config("route_map")
                visible = self._repo_files("route_map").files
                backend = self._backend_fingerprint("route_map", config, visible)
                frontend = self._frontend_fingerprint("route_map", config, visible)
                toolchain = self.toolchain()
                key = self._routes_cache_key(backend, frontend, toolchain)
                if (
                    self._routes is not None
                    and self._routes_key == key
                    and self._routes_settled(backend, frontend)
                    and not self._routes_helper_crashed()
                ):
                    return self._routes
                model, scan, scanned = self._frontend_scan(config, backend, frontend, toolchain, visible)
                with self._step("route_map", STEP_MATCH_ROUTES):
                    result = self.analyzers.build_routes(model, scan)
            except Exception as exc:
                self._record_failure("route_map", exc)
                raise
            self._routes, self._routes_key = result, key
            self._routes_built = (backend, frontend)
            diag = next((d for d in result["diagnostics"] if d.get("kind") == "frontend_unavailable"), None)
            self._routes_frontend = (
                None if diag is None else routes_frontend_problem(diag, toolchain, frontend_present=frontend.present)
            )
            if self._routes_helper_crashed():
                # A crash may be transient, so the next routes call runs the
                # helper again instead of replaying the failure; the result
                # stays cached only for status to report.
                self._scan = self._scan_key = None
            # A reused scan says nothing new about the helper: a later crash
            # must not be cleared by a run that never started it.
            if scanned and self._routes_frontend is not None and self._routes_frontend.code == HELPER_FAILED:
                self._helper_failures[ROUTES_MODE] = self._routes_frontend
            elif scanned and self._routes_frontend is None and toolchain.available:
                self._helper_failures.pop(ROUTES_MODE, None)
            self._record_success("route_map", started, complete=bool(result.get("complete")))
            return result

    def _routes_helper_crashed(self) -> bool:
        return self._routes_frontend is not None and self._routes_frontend.code == HELPER_FAILED

    def _routes_settled(self, backend: _Fingerprint, frontend: _Fingerprint) -> bool:
        built = self._routes_built
        return built is not None and settled(built[0], backend) and settled(built[1], frontend)

    def routes_frontend_problem(self) -> FrontendProblem | None:
        """Why the last routes result has no frontend; None when it has one."""
        return self._routes_frontend

    # ── JSX and symbols ──────────────────────────────────────────────────

    def frontend_root_problem(self, analyzer: str = "jsx_usage") -> FrontendProblem | None:
        """`FRONTEND_NOT_FOUND` when the checkout has no configured frontend root,
        worded as the routes scan words it."""
        problem = ts_bridge.frontend_not_found_problem(self.repo_root, self.config(analyzer).frontend.root)
        return None if problem is None else FrontendProblem.from_toolchain(problem)

    def resolve_root(self, root: str) -> Path:
        """``root`` as an absolute directory inside the repo; `InvalidRoot`, or
        `FrontendNotFound` for a root inside a frontend root the checkout lacks,
        which is a missing frontend rather than a mistyped root."""
        if "\x00" in root:
            raise InvalidRoot(root, f"root {root!r} contains a NUL character, which no path can hold")
        repo = self.repo_root.resolve()
        try:
            candidate = (self.repo_root / root).resolve()
        except _RESOLVE_ERRORS as exc:
            raise InvalidRoot(root, f"root {root!r} could not be resolved ({type(exc).__name__}: {exc})") from exc
        if candidate != repo and repo not in candidate.parents:
            raise InvalidRoot(root, f"root {root!r} resolves to {candidate.as_posix()}, outside the repository {repo}")
        if not candidate.is_dir():
            frontend = (repo / self.config("jsx_usage").frontend.root).resolve()
            missing = self.frontend_root_problem()
            if missing is not None and (candidate == frontend or frontend in candidate.parents):
                raise FrontendNotFound("jsx_usage", missing)
            raise InvalidRoot(root, f"root {root!r}: {candidate.as_posix()} is not a directory")
        return candidate

    def get_jsx_usage(
        self,
        component: str,
        root: Path,
        *,
        include_self_close: bool = True,
        include_tests: bool = False,
    ) -> JsxResult:
        """Every query walks its own root (git-visible files with the JSX
        suffixes) and reads the per-file facts cache, so an edit to any scanned
        file shows up next call."""
        with self._lock:
            config = self.config("jsx_usage")
            toolchain = self.toolchain()
            if not toolchain.available:
                raise ToolchainUnavailable(ts_bridge.incomplete_toolchain_problem(toolchain))
            visible = self._repo_files("jsx_usage").files
            found: list[problems.Problem] = []
            with self._step("jsx_usage", STEP_FACTS_HELPER):
                try:
                    usages = self.analyzers.find_jsx_usage(
                        component,
                        root,
                        facts=self._facts_cache(),
                        repo_root=self.repo_root,
                        frontend_root=config.frontend.root,
                        include_self_close=include_self_close,
                        include_tests=include_tests,
                        problems=found,
                        visible=visible,
                    )
                except ts_bridge.HelperError as exc:
                    raise self._helper_failed("jsx_usage", exc) from exc
            self._helper_failures.pop(FACTS_MODE, None)
            return JsxResult(list(usages), found)

    def resolve_symbol(self, name: str, *, side: str, kind: str | None, file_hint: str | None) -> Any:
        """`SymbolIndex.resolve`; ``side="frontend"`` raises `ToolchainUnavailable`
        without a toolchain, ``side="both"`` returns backend candidates marked
        incomplete."""
        with self._lock:
            if side == "frontend" and (missing := self.frontend_root_problem("symbol_resolve")) is not None:
                raise FrontendNotFound("symbol_resolve", missing)
            index = self._symbol_index(self.config("symbol_resolve"))
            with self._step("symbol_resolve", STEP_RESOLVE_SYMBOL):
                try:
                    result = index.resolve(name, side=side, kind_filter=kind, file_hint=file_hint)
                except ts_bridge.HelperError as exc:
                    raise self._helper_failed("symbol_resolve", exc) from exc
            if side != "backend":
                frontend_problem = getattr(result, "frontend_problem", None)
                if getattr(frontend_problem, "code", None) == HELPER_FAILED:
                    self._helper_failures[FACTS_MODE] = FrontendProblem(
                        HELPER_FAILED,
                        str(getattr(frontend_problem, "message", "")),
                        getattr(frontend_problem, "missing", None),
                        str(getattr(frontend_problem, "fix", None) or HELPER_FAILED_FIX),
                    )
                elif frontend_problem is None:
                    self._helper_failures.pop(FACTS_MODE, None)
            return result

    # ── Docs check ───────────────────────────────────────────────────────

    def get_doc_report(self) -> DocReportResult:
        """The docs-vs-code report (`doc_refs.DocReport`) for the current
        checkout, with its frontend problems (`DocReportResult`).

        Shares the DI cache with the DI tools and the symbol index with symbol
        search; the per-file Markdown and code-word caches live in the doc
        index. Resolution itself runs on every call. Raises `AnalysisFailed`
        for a git failure, a config without ``[docs]`` and any analyzer error.

        Frontend definitions only add definition locations (a frontend name
        still resolves through tracked code words), so a missing toolchain or
        a crashed helper is reported as a limitation inside the report and
        never as a diagnostic; ``frontend_problems`` keeps the diagnostic for
        lookups whose answer needs those locations. Both come from one run
        under the lock, so a concurrent call cannot pair one report with
        another run's problems.

        Problems of files git does not track are dropped, as the report drops
        their symbol rows, routes and pages (`doc_refs.untracked_path`): CI's
        checkout has no such files, so they would make a local report differ
        from CI's. A problem whose ``file`` is not a repository path (a module
        name, no file) is kept. Only the DI limitations that can hide a live
        route (`problems.BACKEND_ROUTE_GAP_KINDS`) reach the report: the others
        say nothing about whether a route exists.
        """
        with self._lock:
            from code_intel import doc_refs

            config = self.config("doc_refs")
            with self._step("doc_refs", STEP_READ_DOC_CONFIG):
                try:
                    doc_config = doc_rules.DocRefsConfig.from_config(config, self.repo_root)
                except ConfigError as exc:
                    raise AnalysisFailed.build(
                        "doc_refs", STEP_READ_DOC_CONFIG, str(exc), self.repo_root, None
                    ) from exc
            index = self._doc_index_cache()
            files = self._repo_files("doc_refs")
            di = self._get_di(files.files)
            symbols, symbol_problems, frontend_problems, frontend_available = self._doc_symbols(config)

            def kept(file: Any) -> bool:
                return not doc_refs.untracked_path(files, file if isinstance(file, str) else None)

            extra = [
                *(_as_problem(row) for row in di["diagnostics"] if kept(row.get("file"))),
                *(p for p in symbol_problems if kept(p.file)),
            ]
            route_gaps = [
                row
                for row in di["analysis_limitations"]
                if row.get("kind") in problems.BACKEND_ROUTE_GAP_KINDS and kept(row.get("file"))
            ]
            with self._step("doc_refs", STEP_RESOLVE_DOC_REFS):
                try:
                    report = index.report(
                        files,
                        di["routes"],
                        di["unmounted_routes"],
                        route_gaps,
                        symbols,
                        frontend_available,
                        doc_config,
                        list(dict.fromkeys(extra)),
                        pages=self._page_provider(config),
                    )
                except RepoFilesError as exc:
                    raise AnalysisFailed.build(
                        "doc_refs", STEP_RESOLVE_DOC_REFS, str(exc), self.repo_root, None
                    ) from exc
            return DocReportResult(report, frontend_problems)

    def _page_provider(self, config: Config) -> doc_rules.PageProvider:
        """The page provider for ``config``'s frontend, kept while its root and
        ``[frontend.pages]`` are unchanged so its facts stay cached."""
        key = (config.frontend.root, config.frontend.pages)
        if self._pages is None or self._pages_key != key:
            self._pages = self.analyzers.make_pages(self.repo_root, config, self.toolchain, self._clock)
            self._pages_key = key
        return self._pages

    def get_agent_findings(self) -> list[Any]:
        """The agent-file findings (`doc_report.Finding` rows) for the current
        checkout; none without an ``[agents]`` table."""
        with self._lock:
            from code_intel import agent_files

            agents = self.config("agent_files").agents
            if agents is None:
                return []
            files = self._repo_files("agent_files")
            with self._step("agent_files", STEP_CHECK_AGENT_FILES):
                return agent_files.check(self.repo_root, agents, files.files)

    def _doc_index_cache(self) -> Any:
        if self._doc_index is None:
            self._doc_index = self.analyzers.make_doc_index(self.repo_root, self._clock)
        return self._doc_index

    def _doc_symbols(self, config: Config) -> tuple[list[Any], list[problems.Problem], list[problems.Problem], bool]:
        """(symbol rows, Python index problems, frontend problems, whether
        frontend definitions were indexed). The TypeScript helper runs in a
        worker thread while the Python index builds, as symbol search does
        with ``side="both"``."""
        index = self._symbol_index(config)
        backend: list[problems.Problem] = []
        frontend: list[problems.Problem] = []
        unavailable: FrontendProblem | None = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(index.typescript_decls, frontend)
            with self._step("doc_refs", STEP_INDEX_DOC_SYMBOLS):
                rows = list(index.python_symbols(backend))
            with self._step("doc_refs", STEP_FACTS_HELPER):
                try:
                    rows.extend(future.result())
                except ToolchainUnavailable as exc:
                    unavailable = FrontendProblem.from_toolchain(exc.problem)
                except ts_bridge.HelperError as exc:
                    unavailable = FrontendProblem.from_helper_error(exc)
                    self._helper_failures[FACTS_MODE] = unavailable
                else:
                    self._helper_failures.pop(FACTS_MODE, None)
        if unavailable is not None:
            frontend.append(
                problems.diagnostic(
                    "frontend_unavailable",
                    stage=doc_rules.STAGE,
                    step=STEP_FACTS_HELPER,
                    file=unavailable.missing,
                    message=f"frontend definitions were not indexed: {unavailable.message}",
                    missing=unavailable.missing,
                    fix=unavailable.fix,
                    problem=unavailable.code,
                )
            )
        return rows, backend, frontend, unavailable is None

    # ── Refresh and status ───────────────────────────────────────────────

    def reset(self) -> None:
        """Drop every cache, so the next read rebuilds from source."""
        with self._lock:
            self._config = None
            self._model = self._model_key = None
            self._di = self._di_model = self._di_digest = None
            self._routes = self._routes_key = None
            self._routes_built = None
            self._scan = self._scan_key = None
            self._scan_built = None
            self._routes_frontend = None
            self._helper_failures.clear()
            if self._facts is not None:
                self._facts.clear()
            self._symbols = self._symbols_config = None
            if self._doc_index is not None:
                self._doc_index.clear()
            self._doc_index = None
            self._pages = self._pages_key = None
            with self._toolchain_lock:
                self._toolchain = None
            self.last_state.clear()

    def refresh(self) -> RefreshResult:
        """Drop every cache and rebuild DI and routes now."""
        with self._lock:
            self.reset()
            di = self.get_di()
            routes = self.get_routes()
            return RefreshResult(
                dependency_function_count=len(di["dependency_functions"]),
                di_route_count=len(di["routes"]),
                routes_summary=dict(routes["summary"]),
                complete=bool(di["complete"]) and bool(routes["complete"]),
                diagnostics=_dedupe([*di["diagnostics"], *routes["diagnostics"]]),
                limitations=_dedupe([*di["analysis_limitations"], *routes["analysis_limitations"]]),
                frontend_problem=self._routes_frontend,
            )

    def _load_counts(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, dict]]:
        """(counts, counts_error, counts_errors). Each analyzer runs in its own
        try, so one failure leaves the other's counts in place."""
        if not self.runtime_compatible():
            running, required = self.runtime_versions()
            error = {
                "error": "runtime_incompatible",
                "message": f"Python {running} is older than the backend's {required}; counts need DI and routes",
                "running": running,
                "required": required,
            }
            return None, error, {}
        counts: dict[str, Any] = {"di": None, "routes": None}
        errors: dict[str, dict] = {}
        loaders: tuple[tuple[str, Callable[[], dict]], ...] = (("di", self.get_di), ("routes", self.get_routes))
        for name, load in loaders:
            try:
                data = load()
            except AnalysisFailed as exc:
                errors[name] = {
                    "error": "analysis_failed",
                    "message": exc.message,
                    "analyzer": exc.analyzer,
                    "step": exc.step,
                    "missing": exc.missing,
                }
                continue
            except Exception as exc:
                # Status reports each analyzer's own failure; one unexpected
                # error must not hide the other analyzer's counts.
                errors[name] = {"error": "internal_error", "message": f"{name}: {type(exc).__name__}: {exc}"}
                continue
            frontend_problem = self._routes_frontend if name == "routes" else None
            if frontend_problem is not None:
                errors[name] = {"error": "frontend_unavailable", **frontend_problem.fields()}
                continue
            counts[name] = dict(data["summary"])
        return counts, None, errors

    def _revalidate(self, config: Config | None) -> tuple[bool, bool]:
        """Drop caches whose sources changed; returns (di_fresh, routes_fresh).

        A cache and its analyzer state fall together, so status never reports a
        complete run next to a cache that no longer matches the sources. A cache
        whose sources are still inside the racy window is kept and reported as
        not fresh, because dropping it would show "not loaded" beside the run
        that just built it; once the window has passed, its content decides
        (`settled`), so it is either fresh again or dropped. Without a usable
        config or git listing nothing can be compared, so every cache drops.
        """
        backend: _Fingerprint | None = None
        frontend: _Fingerprint | None = None
        if config is not None:
            try:
                visible = self._repo_files("di_map").files
                backend = self._backend_fingerprint("di_map", config, visible)
                frontend = self._frontend_fingerprint("route_map", config, visible)
            except AnalysisFailed:
                pass
        di_fresh = self._revalidate_di(backend)
        routes_fresh = self._revalidate_routes(backend, frontend)
        return di_fresh, routes_fresh

    def _revalidate_di(self, backend: _Fingerprint | None) -> bool:
        built = self._model_key
        fresh = backend is not None and built is not None and settled(built, backend)
        pending = backend is not None and built is not None and built.digest == backend.digest and backend.racy
        if not (fresh or pending):
            self._model = self._model_key = None
        if self._di is not None and (self._model is None or backend is None or self._di_digest != backend.digest):
            self._di = self._di_model = self._di_digest = None
            self.last_state.pop("di_map", None)
        return self._di is not None and fresh

    def _revalidate_routes(self, backend: _Fingerprint | None, frontend: _Fingerprint | None) -> bool:
        if self._routes is None:
            return False
        fresh = pending = False
        if backend is not None and frontend is not None:
            if self._routes_cache_key(backend, frontend, self.toolchain()) == self._routes_key:
                fresh = self._routes_settled(backend, frontend)
                pending = not fresh and (backend.racy or frontend.racy)
        if not (fresh or pending):
            self._routes = self._routes_key = self._routes_built = None
            self._routes_frontend = None
            self.last_state.pop("route_map", None)
        return fresh

    def status(self, load_counts: bool = False) -> StatusSnapshot:
        """Cache health, completeness, toolchain and diagnostics, revalidated
        before reading. The probe never runs the TypeScript helper. A config
        that cannot be read is reported, not raised: status is how to see why."""
        with self._lock:
            config: Config | None
            try:
                config = self.config()
            except AnalysisFailed as exc:
                config, config_error = None, exc.message
            else:
                config_error = None
            counts, counts_error, counts_errors = (None, None, {})
            if load_counts and config is not None:
                counts, counts_error, counts_errors = self._load_counts()
            di_fresh, routes_fresh = self._revalidate(config)

            loaded = [c for c in (self._di, self._routes) if c is not None]
            diagnostics = _dedupe(d for c in loaded for d in c["diagnostics"])
            limitations = _dedupe(lim for c in loaded for lim in c["analysis_limitations"])
            states = {name: self.last_state.get(name, "not_run") for name in ANALYZERS}
            complete: bool | None
            if any(s == "failed" for s in states.values()) or any(c["complete"] is False for c in loaded):
                complete = False
            elif di_fresh and routes_fresh and all(s == "complete" for s in states.values()):
                complete = True
            else:
                complete = None

            running = "{}.{}".format(*self.python_running[:2])
            required = None if config is None else self.runtime_versions()[1]
            facts_stats = self._facts.stats() if self._facts is not None else None
            code = self.code_state()
            return StatusSnapshot(
                repo_root=str(self.repo_root),
                tool_dir=str(self.tool_dir),
                git_sha=self.git_sha(),
                complete=complete,
                code_stale=code.stale,
                python=sys.version.split()[0],
                executable=sys.executable,
                runtime_compatible=None if config is None else self.runtime_compatible(),
                python_running=running,
                python_required=required,
                diagnostics=diagnostics,
                limitations=limitations,
                analyzers={name: self._analyzer_status(name) for name in ANALYZERS},
                cache={
                    "backend_model": {"loaded": self._model is not None},
                    "di": {"loaded": self._di is not None, "fresh": di_fresh},
                    "routes": {"loaded": self._routes is not None, "fresh": routes_fresh},
                },
                toolchain=toolchain_status(self.toolchain(), self.repo_root, facts_stats, self._helper_failures),
                counts=counts,
                counts_error=counts_error,
                counts_errors=counts_errors,
                code_unconfirmed=self.code_unconfirmed(code),
                fastapi={} if config is None else self._fastapi_status(config),
                config={"path": code_paths.rel_posix(self.config_file.path, self.repo_root), "error": config_error},
            )

    def _fastapi_status(self, config: Config) -> dict[str, Any]:
        model = include_models.read(self.repo_root, config.backend.lock)
        return {
            "fastapi_version": model.fastapi_version,
            "include_model": model.kind,
            "include_model_reason": model.reason,
            "source": model.source,
            "summary": include_models.describe(model),
        }

    def _analyzer_status(self, name: str) -> dict[str, Any]:
        state = self.last_state.get(name, "not_run")
        return {
            "state": state,
            "last_step": self.failed_steps.get(name) if state == "failed" else None,
            "last_success_at": self.last_success_at.get(name),
            "last_runtime_ms": self.last_runtime_ms.get(name),
            "last_error": self.last_errors.get(name) if state == "failed" else None,
        }
