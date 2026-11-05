"""Diagnostics, limitations and the interpreter floor shared by every analyzer.

A *diagnostic* means content was lost or a resolver failed: it sets
``complete: false``. A *limitation* is a known blind spot that leaves the
result usable: it never changes ``complete``. Every kind is registered here so
a test can check that each emitted kind is documented in the README.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from code_intel.result_types import DiagnosticDict, LimitationDict

Category = Literal["content_lost", "resolver"]

# This tool itself needs 3.14, so a missing or unparseable version file imposes nothing beyond that.
_FALLBACK_MIN_PYTHON: tuple[int, int] = (3, 14)

# Kind -> (category, one-line description). Content-lost kinds name the step
# that failed through ``Problem.step``.
DIAGNOSTIC_KINDS: Mapping[str, tuple[Category, str]] = {
    "list_failed": ("content_lost", "a directory could not be listed, so its files were not analyzed"),
    "stat_failed": ("content_lost", "a file's metadata could not be read, so the file was not analyzed"),
    "read_failed": ("content_lost", "a file could not be read"),
    "decode_failed": ("content_lost", "a file's bytes could not be decoded as text"),
    "parse_failed": ("content_lost", "a file could not be parsed"),
    "analysis_failed": ("content_lost", "an unexpected error stopped the analysis of one file"),
    "unresolved_app": (
        "resolver",
        "the configured app (module:attribute) did not resolve to exactly one FastAPI app, so no route is live",
    ),
    "typescript_syntax_error": (
        "content_lost",
        "TypeScript reported syntax errors; results come from TypeScript's recovered tree",
    ),
    "frontend_unavailable": (
        "content_lost",
        "the frontend could not be analyzed (missing frontend root, Node or TypeScript)",
    ),
    "frontend_client_module_missing": (
        "content_lost",
        "a configured frontend API client module does not exist, so its calls were not found",
    ),
    "unparsed_frontend_callsite": (
        "resolver",
        "a reference to an API client is not a call the analyzer can read",
    ),
    "unresolved_frontend_path": (
        "resolver",
        "a frontend API call's URL does not evaluate to a path starting with /",
    ),
}

LIMITATION_KINDS: Mapping[str, str] = {
    "unmounted_router": "a router with registrations is not reachable from the app; its routes are not live",
    "unimported_router_change": (
        "a module that nothing imports when the app is imported changes another module's router; the change is not counted"
    ),
    "unresolved_include": "an include_router child could not be resolved to a router",
    "unresolved_include_prefix": "an include_router prefix= could not be evaluated",
    "unresolved_dependencies": "a dependencies= value or element could not be evaluated to Depends/Security calls",
    "unresolved_route_path": "a route path could not be evaluated",
    "unresolved_dependency": (
        "a Depends/Security target could not be resolved to a callable, or FastAPI would reject the declaration"
    ),
    "opaque_router_call": "a router or app is passed to code that may add routes or dependencies the analyzer cannot see",
    "opaque_router_change": (
        "a router's routes, prefix or dependencies are changed in place, or its route list is handed on, so routes the "
        "analysis counts may be moved or removed"
    ),
    "asgi_mount": "mount() of a non-FastAPI ASGI app; its routes are not analyzed",
    "router_include_cycle": "routers include each other in a cycle; the edge closing the cycle is cut",
    "unsupported_route_registration": (
        "a Starlette-level route registration (route, add_route, websocket_route, add_websocket_route) or host"
    ),
    "unsupported_router_argument": "a router or app is built with routes=, whose routes the analyzer does not read",
    "custom_router_class": (
        "a router subclass overrides its constructor or a registration method, so its prefix, dependencies or routes may differ"
    ),
    "conditional_router_mount": "an include runs under a branch, so it may not happen at runtime",
    "deferred_router_mount": "an include registered inside a function nothing calls at import",
    "deferred_route_registration": "a route registered inside a function nothing calls at import",
    "unresolved_route_router": "a route decorator's base may be a router but could not be resolved",
    "unresolved_route_prefix": "a router's own prefix= could not be evaluated",
    "unresolved_mount_path": "a mount() path could not be evaluated",
    "mount_shadows_route": "a route registered after a mount that the mount's path overlaps may not be reached",
    "unresolved_route_methods": "a methods= value the analysis cannot evaluate; the route is reported as ANY",
    "unresolved_redirect_slashes": "an app's redirect_slashes= could not be evaluated; it is reported as True",
    "opaque_decorator": (
        "a package decorator returns a wrapper without functools.wraps, so FastAPI reads the wrapper, not the function"
    ),
    "shared_dependency_id": (
        "callables FastAPI reads with different parameters share one dependency id; one form's sub-dependencies are listed"
    ),
    "unresolved_http_method": "a frontend call's HTTP method could not be evaluated; it is reported as UNKNOWN",
    "url_variants_truncated": "a frontend URL has more variants than the evaluator keeps",
    "unresolved_prop_value": "a component prop that feeds a URL receives a non-literal value at some call site",
    "recursive_router_call": (
        "a function called at import calls itself, directly or through other functions; the recursive call is not followed"
    ),
    "router_call_limit": (
        "a function called at import is too many calls deep, or past the call chains the analysis follows; it is not followed"
    ),
    "conditional_import": (
        "a module is imported under a branch, loop or handler, so the route changes it makes may not happen at runtime"
    ),
    "submodule_missing": "a git submodule is not checked out, so documentation references into it are not checked",
    "frontend_definitions_unavailable": (
        "Node or TypeScript is unavailable, so the docs check has no frontend definition locations; "
        "frontend symbols still resolve through tracked code"
    ),
    "library_capture_stale": (
        "the library capture does not match the code: an installed version differs from the lock, or an entry "
        "matches nothing the analysis finds; run code-intel capture"
    ),
    "frontend_pages_unavailable": (
        "the frontend pages could not be read (Node or TypeScript is unavailable), so a path that only a page "
        "could satisfy is not checked"
    ),
    "frontend_provisioning": (
        "TypeScript is being installed in the background (first use), so frontend results are n/a until it is ready"
    ),
}

# The backend findings that can hide, misplace or change a live route: the
# keys of `queries.BACKEND_ROUTE_GAPS`, which says how each one does. They live
# here so the runtime's docs check can read them without importing `queries`,
# which imports the runtime.
BACKEND_ROUTE_GAP_KINDS: frozenset[str] = frozenset(
    {
        "unresolved_route_router",
        "router_include_cycle",
        "list_failed",
        "stat_failed",
        "read_failed",
        "decode_failed",
        "parse_failed",
        "analysis_failed",
        "unresolved_app",
        "unresolved_include",
        "opaque_router_call",
        "asgi_mount",
        "unsupported_route_registration",
        "unsupported_router_argument",
        "unresolved_route_path",
        "unresolved_route_prefix",
        "unresolved_include_prefix",
        "unresolved_mount_path",
        "mount_shadows_route",
        "opaque_router_change",
        "custom_router_class",
        "recursive_router_call",
        "router_call_limit",
        "unimported_router_change",
        "library_capture_stale",
    }
)


@dataclass(frozen=True)
class Problem:
    """One diagnostic (``category`` set) or limitation (``category`` None).

    ``file`` is repo-relative POSIX when the file is under the repo. ``step``
    names the failed operation (``read file``, ``list directory``, ...) and is
    None for limitations.
    """

    kind: str
    category: Category | None
    stage: str
    step: str | None
    file: str | None
    line: int | None
    message: str
    exception: str | None = None
    likely_cause: str | None = None
    # Set when the fix is known: the path that is missing and what to do about it.
    missing: str | None = None
    fix: str | None = None
    # frontend_unavailable only: the cause code (a toolchain probe code,
    # ``helper_failed`` or ``frontend_not_found``).
    problem: str | None = None

    @property
    def is_diagnostic(self) -> bool:
        return self.category is not None

    def as_dict(self) -> DiagnosticDict | LimitationDict:
        return self.as_limitation() if self.category is None else self.as_diagnostic()

    def as_limitation(self) -> LimitationDict:
        return {
            "kind": self.kind,
            "stage": self.stage,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }

    def as_diagnostic(self) -> DiagnosticDict:
        if self.category is None:
            raise ValueError(f"{self.kind} is a limitation, not a diagnostic")
        out: DiagnosticDict = {
            "kind": self.kind,
            "category": self.category,
            "stage": self.stage,
            "step": self.step,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }
        if self.exception is not None:
            out["exception"] = self.exception
        if self.likely_cause is not None:
            out["likely_cause"] = self.likely_cause
        if self.missing is not None:
            out["missing"] = self.missing
        if self.fix is not None:
            out["fix"] = self.fix
        if self.problem is not None:
            out["problem"] = self.problem
        return out


ProblemLike = Problem | Mapping


def failure_message(file: str | None, step: str, exc: BaseException) -> str:
    """``"<file>: could not <step> (<ExceptionType>: <text>)"``."""
    subject = f"{file}: " if file else ""
    return f"{subject}could not {step} ({type(exc).__name__}: {exc})"


def strip_fix(message: str, fix: str | None) -> str:
    """``message`` without a trailing ``". Fix: <fix>"``, for callers that add the fix themselves."""
    suffix = f". Fix: {fix}"
    if fix and message.endswith(suffix):
        return message[: -len(suffix)]
    return message


def diagnostic(
    kind: str,
    *,
    stage: str,
    step: str,
    file: str | None,
    line: int | None = None,
    message: str | None = None,
    exc: BaseException | None = None,
    likely_cause: str | None = None,
    missing: str | None = None,
    fix: str | None = None,
    problem: str | None = None,
    python_floor: tuple[int, int] | None = None,
) -> Problem:
    """Build a registered diagnostic.

    ``message`` defaults to ``failure_message(file, step, exc)``. ``line``
    defaults to the exception's ``lineno``. A ``parse_failed`` SyntaxError on an
    interpreter older than ``python_floor`` (the repo minimum) gets a
    ``likely_cause`` naming the mismatch.
    """
    if kind not in DIAGNOSTIC_KINDS:
        raise ValueError(f"diagnostic kind {kind!r} is not registered in problems.DIAGNOSTIC_KINDS")
    if message is None:
        if exc is None:
            raise ValueError(f"diagnostic {kind!r} for {file!r} needs a message or an exception")
        message = failure_message(file, step, exc)
    if line is None and exc is not None:
        lineno = getattr(exc, "lineno", None)
        line = lineno if isinstance(lineno, int) else None
    if likely_cause is None and kind == "parse_failed" and isinstance(exc, SyntaxError) and python_floor is not None:
        likely_cause = interpreter_likely_cause(python_floor)
    return Problem(
        kind=kind,
        category=DIAGNOSTIC_KINDS[kind][0],
        stage=stage,
        step=step,
        file=file,
        line=line,
        message=message,
        exception=type(exc).__name__ if exc is not None else None,
        likely_cause=likely_cause,
        missing=missing,
        fix=fix,
        problem=problem,
    )


def limitation(kind: str, *, stage: str, file: str | None, line: int | None, message: str) -> Problem:
    """Build a registered limitation."""
    if kind not in LIMITATION_KINDS:
        raise ValueError(f"limitation kind {kind!r} is not registered in problems.LIMITATION_KINDS")
    return Problem(kind=kind, category=None, stage=stage, step=None, file=file, line=line, message=message)


def partition(problems: Iterable[Problem]) -> tuple[list[Problem], list[Problem]]:
    """Split into ``(diagnostics, limitations)``, keeping order."""
    diagnostics: list[Problem] = []
    limitations: list[Problem] = []
    for problem in problems:
        (diagnostics if problem.is_diagnostic else limitations).append(problem)
    return diagnostics, limitations


def _field(problem: ProblemLike, name: str):
    if isinstance(problem, Problem):
        return getattr(problem, name)
    return problem.get(name)


# step -> (singular phrase, plural phrase) for the incomplete banner.
_STEP_PHRASES: Mapping[str, tuple[str, str]] = {
    "list directory": ("directory could not be listed", "directories could not be listed"),
    "stat file": ("file could not be inspected", "files could not be inspected"),
    "read file": ("file could not be read", "files could not be read"),
    "decode file": ("file could not be decoded", "files could not be decoded"),
    "parse file": ("file could not be parsed", "files could not be parsed"),
    "analyze module": ("module could not be analyzed", "modules could not be analyzed"),
    "extract facts": ("file could not be analyzed", "files could not be analyzed"),
    "index symbols": ("file could not be analyzed", "files could not be analyzed"),
    "extract calls": ("file could not be analyzed", "files could not be analyzed"),
    "read tsconfig": ("tsconfig could not be read", "tsconfigs could not be read"),
    "run typescript helper (facts mode)": (
        "TypeScript helper run failed",
        "TypeScript helper runs failed",
    ),
}

# kind -> (singular, plural), for diagnostics whose step does not name what was lost.
_KIND_PHRASES: Mapping[str, tuple[str, str]] = {
    "typescript_syntax_error": ("file has TypeScript syntax errors", "files have TypeScript syntax errors"),
    "frontend_unavailable": ("frontend could not be analyzed", "frontends could not be analyzed"),
    "frontend_client_module_missing": ("API client module is missing", "API client modules are missing"),
    "unparsed_frontend_callsite": ("API client reference could not be read", "API client references could not be read"),
    "unresolved_frontend_path": ("frontend URL is not a path", "frontend URLs are not paths"),
    "unresolved_app": ("app could not be resolved", "apps could not be resolved"),
}


def _phrase(step: str | None, kind: str | None, count: int) -> str:
    phrases = _KIND_PHRASES.get(kind or "") or _STEP_PHRASES.get(step or "")
    if phrases is not None:
        return f"{count} {phrases[0] if count == 1 else phrases[1]}"
    label = step or kind or "unknown step"
    return f"{count} failure{'' if count == 1 else 's'} at step '{label}'"


def incomplete_banner(diagnostics: Sequence[ProblemLike]) -> str:
    """One-line banner naming what was lost, grouped by step and category.

    Example: ``⚠ INCOMPLETE — 2 files could not be read, 1 include could not be
    resolved; counts may be undercounts.``
    """
    groups: dict[tuple[str | None, str | None, str | None], int] = {}
    for problem in diagnostics:
        step = _field(problem, "step")
        kind = _field(problem, "kind")
        # Kinds with their own phrase group by kind; the rest share a step phrase.
        key = (
            (None, kind, _field(problem, "category"))
            if kind in _KIND_PHRASES
            else (step, None, _field(problem, "category"))
        )
        groups[key] = groups.get(key, 0) + 1
    parts = [_phrase(step, kind, count) for (step, kind, _category), count in groups.items()]
    content_lost = any(_field(p, "category") == "content_lost" for p in diagnostics)
    tail = "counts may be undercounts" if content_lost else "results may be imprecise"
    return f"⚠ INCOMPLETE — {', '.join(parts) or 'no diagnostics recorded'}; {tail}."


def limitations_banner(limitations: Sequence[ProblemLike]) -> str:
    """Multi-line banner for non-fatal limitations, one bullet per entry.

    Rendered before the result: a blind spot that is easy to skim past is the
    failure this channel exists to prevent.
    """
    lines = [
        f"ⓘ {len(limitations)} analysis limitation(s) — informational, results stay "
        f"usable but may look more confident than they are:"
    ]
    for lim in limitations:
        loc = _field(lim, "file") or "?"
        line = _field(lim, "line")
        if line:
            loc = f"{loc}:{line}"
        lines.append(f"  • [{_field(lim, 'kind') or '?'}] {loc}: {_field(lim, 'message') or ''}")
    return "\n".join(lines)


# ── Interpreter floor ─────────────────────────────────────────────────────


def _parse_min_python(text: str) -> tuple[int, int] | None:
    """Parse a ``.python-version`` body into ``(major, minor)``, or None."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head = stripped.split()[0]
        parts = head.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (int(parts[0]), int(parts[1]))
        return None
    return None


def min_python_version(version_file: Path | None) -> tuple[int, int]:
    """Minimum supported Python from the repo's ``.python-version`` file.

    Falls back to ``_FALLBACK_MIN_PYTHON`` when no file is configured or it is
    missing or unparseable, so the guard never crashes on a stripped-down checkout.
    """
    if version_file is None:
        return _FALLBACK_MIN_PYTHON
    try:
        parsed = _parse_min_python(version_file.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError:
        parsed = None
    return parsed or _FALLBACK_MIN_PYTHON


def runtime_is_compatible(minimum: tuple[int, int], version_info: tuple[int, ...] | None = None) -> bool:
    """True when the interpreter is new enough to parse backend source.

    Compares with ``>=`` because a newer interpreter parses older syntax. The
    version is injectable so the guard is testable without an old interpreter.
    """
    running = tuple((sys.version_info[:2] if version_info is None else version_info)[:2])
    return running >= tuple(minimum)


def runtime_refusal(repo_root: Path, python_version_file: str | None) -> str | None:
    """The message for refusing to analyze when this interpreter is older than the repo's
    minimum Python (``python_version_file``, repo-relative), else None."""
    minimum = min_python_version(repo_root / python_version_file if python_version_file else None)
    if runtime_is_compatible(minimum):
        return None
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    required = "{}.{}".format(*minimum)
    return (
        f"Error: Python {running} is older than the repo minimum {required}. "
        f"Backend source uses syntax this interpreter cannot parse, so results would be "
        f"silently incomplete. Re-run with Python >={required} (e.g. `uv run --python {required}`)."
    )


def interpreter_likely_cause(minimum: tuple[int, int], version_info: tuple[int, ...] | None = None) -> str | None:
    """The ``likely_cause`` for a SyntaxError, when the interpreter is too old."""
    if runtime_is_compatible(minimum, version_info):
        return None
    running = (sys.version_info[:2] if version_info is None else version_info)[:2]
    return f"interpreter too old: running {running[0]}.{running[1]}, repo requires >={minimum[0]}.{minimum[1]}"
