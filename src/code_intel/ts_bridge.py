"""Find Node and the helper's TypeScript, and run the TypeScript helper.

TypeScript is the helper's own pinned install (``code-intel setup`` puts it in
the user cache, shared by every checkout), or the directory named by
``CODE_INTEL_TYPESCRIPT_DIR``.

The helper (``ts/helper.cjs``) is one-shot: each batch spawns one ``node``
process, writes one JSON request to stdin and reads one JSON document from
stdout. File lists travel over stdin, so no command line ever approaches the
Windows 32k limit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from code_intel import toolchain_pins
from code_intel.code_paths import FileStamp, rel_posix

TOOL_DIR = Path(__file__).resolve().parent
HELPER_DIR = TOOL_DIR / "ts"
HELPER = HELPER_DIR / "helper.cjs"

TS_DIR_ENV = "CODE_INTEL_TYPESCRIPT_DIR"  # optional override; a relative value is taken from the repo root
CACHE_ENV = "CODE_INTEL_CACHE_DIR"
# Optional node binary override: MCP clients launched from a GUI often get a
# PATH without the node a version manager installs.
NODE_ENV = "CODE_INTEL_NODE"
HELPER_PROTOCOL = 1
MIN_NODE_MAJOR = 18
# The first TypeScript with a stable compiler API; 7.0 only has it under unstable paths.
MIN_TYPESCRIPT = (7, 1)
NODE_VERSION_TIMEOUT_S = 5.0
FACTS_TIMEOUT_S = 120.0
ROUTES_TIMEOUT_S = 180.0
# The helper exits 2, with {"protocol_error": ...} on stdout, when it rejects a request.
PROTOCOL_ERROR_EXIT = 2

FRONTEND_TS_FIX = f'run "code-intel setup" (or set {TS_DIR_ENV} to a typescript package directory)'
NODE_FIX = f"install Node.js {MIN_NODE_MAJOR} or newer and put node on PATH, or set {NODE_ENV} to the node binary"
NODE_ENV_FIX = f"set {NODE_ENV} to the path of a Node.js {MIN_NODE_MAJOR}+ binary, or unset it to use node from PATH"
HELPER_FIX = "run the helper by hand (see README, Commands) to see the full error"

ProblemCode = Literal[
    "node_not_found",
    "node_failed",
    "node_too_old",
    "typescript_not_found",
    "typescript_unreadable",
    "typescript_unsupported",
    "toolchain_incomplete",
    "helper_failed",
    "frontend_not_found",
]
HelperMode = Literal["facts", "routes"]
HelperStep = Literal["start", "write request", "timeout", "exit", "decode output", "protocol"]

# subprocess.CREATE_NO_WINDOW exists only on Windows; the value is fixed by the Win32 API.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_IS_WINDOWS = os.name == "nt"
# Matched against the whole stripped output: a release build prints exactly
# this, and anything else (a pre-release suffix, a second line) is not a
# version the minimum check can trust.
_NODE_VERSION_RE = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
# Starting or waiting on a process: OSError (missing binary, permissions) or a timeout.
_RUN_ERRORS: tuple[type[Exception], ...] = (OSError, subprocess.SubprocessError)
# Reading package.json: OSError, or ValueError for bad bytes and bad JSON.
_JSON_READ_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError)
# json.dumps on a request: TypeError for an unserializable value, ValueError for NaN or cycles.
_ENCODE_ERRORS: tuple[type[Exception], ...] = (TypeError, ValueError)


@dataclass(frozen=True)
class ToolchainProblem:
    code: ProblemCode
    message: str  # names the missing path or the failed operation
    missing: str | None  # repo-relative when under the repo, else absolute
    fix: str


@dataclass(frozen=True)
class Toolchain:
    node: str | None  # absolute path: $CODE_INTEL_NODE when set, else shutil.which("node")
    node_version: str | None  # "v24.21.0"
    typescript_dir: Path | None
    typescript_version: str | None  # typescript_dir/package.json "version"
    typescript_source: Literal["env", "helper"] | None
    lockfile_typescript_version: str | None  # the packaged ts/package-lock.json
    problem: ToolchainProblem | None
    warnings: tuple[str, ...]
    # The helper runs with this as its working directory.
    repo_root: Path | None = None
    node_source: Literal["env", "path"] | None = None
    # The compiler file the helper loads, as stat facts: a same-version
    # reinstall or a patched compiler changes it (`compiler_stamp`).
    compiler_stamp: str | None = None
    # The node binary's stat facts (`node_stamp`): a reinstall or upgrade in
    # place changes them even when the path and the version string do not.
    node_stamp: str | None = None

    @property
    def available(self) -> bool:
        return self.problem is None and self.node is not None and self.typescript_dir is not None

    def stamp(self) -> tuple[str, ...]:
        """Cache stamp: changes when node, the TypeScript package or compiler, or the helper code changes."""
        return (
            self.node_version or "",
            self.node or "",
            self.node_stamp or "",
            str(self.typescript_dir) if self.typescript_dir is not None else "",
            self.typescript_version or "",
            self.compiler_stamp or "",
            helper_digest(),
        )


class ToolchainUnavailable(RuntimeError):
    """Raised when a frontend analysis needs the toolchain and it is unavailable."""

    def __init__(self, problem: ToolchainProblem) -> None:
        super().__init__(f"{problem.message}. Fix: {problem.fix}")
        self.problem = problem


class HelperError(RuntimeError):
    """The helper process failed; ``step`` names where."""

    def __init__(self, mode: str, step: HelperStep, detail: str) -> None:
        super().__init__(f"typescript helper ({mode} mode) failed at step '{step}': {detail}")
        self.mode = mode
        self.step = step
        self.detail = detail

    def problem(self) -> ToolchainProblem:
        """The failure as a ``helper_failed`` problem, so every frontend path
        reports a crashed helper the same way it reports a missing toolchain."""
        return ToolchainProblem("helper_failed", f"{HELPER.as_posix()}: {self}", None, HELPER_FIX)


def incomplete_toolchain_problem(toolchain: Toolchain) -> ToolchainProblem:
    """The problem for a toolchain that is unavailable without a recorded
    problem, naming which piece is missing."""
    if toolchain.problem is not None:
        return toolchain.problem
    missing = [
        name
        for name, value in (("node", toolchain.node), ("typescript directory", toolchain.typescript_dir))
        if value is None
    ]
    what = " and ".join(missing) or "node and typescript"
    return ToolchainProblem(
        "toolchain_incomplete",
        f"toolchain incomplete: {what} not resolved (node={toolchain.node}, typescript_dir={toolchain.typescript_dir})",
        None,
        # Node is probed first, so its fix comes first when both are missing.
        NODE_FIX if toolchain.node is None else FRONTEND_TS_FIX,
    )


def frontend_not_found_problem(repo_root: Path, frontend_root: str) -> ToolchainProblem | None:
    """The problem for a checkout without the configured frontend root, or None
    when it is there. Every frontend tool reports a missing root this way, before
    the toolchain."""
    if (repo_root / frontend_root).is_dir():
        return None
    return ToolchainProblem(
        "frontend_not_found",
        f"{frontend_root}: frontend root not found",
        frontend_root,
        f"run the analysis from a checkout that has {frontend_root}/, or correct frontend.root in the config",
    )


# ── Discovery ────────────────────────────────────────────────────────────

# (node path, stamp) -> version, so repeated probes do not respawn node.
_node_versions: dict[tuple[str, FileStamp], str] = {}
_node_versions_lock = threading.Lock()


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _creationflags() -> int:
    # Without this flag every helper run flashes a console window on Windows.
    return _CREATE_NO_WINDOW if _IS_WINDOWS else 0


def _node_version_key(node: str) -> tuple[str, FileStamp] | None:
    """The version cache key: a binary replaced in place gets a new key, even
    one copied over with its modification time preserved (the stamp has ctime)."""
    try:
        st = os.stat(node)
    except OSError:
        return None
    return (node, FileStamp.from_stat(st))


def _node_version(node: str, run: Callable[..., Any], cache: bool) -> tuple[str | None, ToolchainProblem | None]:
    key = _node_version_key(node) if cache else None
    if key is not None:
        with _node_versions_lock:
            cached = _node_versions.get(key)
        if cached is not None:
            return cached, None
    version, problem = _run_node_version(node, run)
    if problem is None and key is not None and version is not None:
        with _node_versions_lock:
            _node_versions[key] = version
    return version, problem


def _run_node_version(node: str, run: Callable[..., Any]) -> tuple[str | None, ToolchainProblem | None]:
    """``node --version``, checked against the minimum major version."""
    try:
        proc = run(
            [node, "--version"],
            capture_output=True,
            timeout=NODE_VERSION_TIMEOUT_S,
            check=False,
            creationflags=_creationflags(),
        )
    except _RUN_ERRORS as exc:
        return None, ToolchainProblem(
            "node_failed", f"{node}: could not run node --version ({type(exc).__name__}: {exc})", node, NODE_FIX
        )
    stdout = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else str(proc.stderr or "")
        return None, ToolchainProblem(
            "node_failed",
            f"{node}: node --version exited with code {proc.returncode}: {_first_line(stderr) or '(no output)'}",
            node,
            NODE_FIX,
        )
    version = stdout.strip()
    match = _NODE_VERSION_RE.fullmatch(version)
    if match is None:
        return None, ToolchainProblem(
            "node_failed",
            f"{node}: reading the Node version failed: node --version printed {version!r}, "
            "expected exactly vMAJOR.MINOR.PATCH",
            node,
            NODE_FIX,
        )
    if int(match.group(1)) < MIN_NODE_MAJOR:
        return version, ToolchainProblem(
            "node_too_old", f"{node}: node {version} is older than the required {MIN_NODE_MAJOR}", node, NODE_FIX
        )
    return version, None


def _env_label(name: str, raw: str, resolved: str) -> str:
    """``NAME=raw``, plus the resolved path when it differs from what was written."""
    return f"{name}={raw}" + ("" if resolved == raw else f" ({resolved})")


def _typescript_package(
    ts_dir: Path, repo_root: Path, source: Literal["env", "helper"], raw: str = ""
) -> tuple[str | None, ToolchainProblem | None]:
    package_json = ts_dir / "package.json"
    shown = rel_posix(package_json, repo_root)
    fix = (
        f"set {TS_DIR_ENV} to a directory holding the typescript package (its package.json names typescript)"
        if source == "env"
        else FRONTEND_TS_FIX
    )
    if not package_json.is_file():
        where = f"{_env_label(TS_DIR_ENV, raw, str(ts_dir))}: " if source == "env" else ""
        return None, ToolchainProblem("typescript_not_found", f"{where}{shown} not found", shown, fix)
    try:
        data = json.loads(package_json.read_bytes().decode("utf-8-sig"))
    except _JSON_READ_ERRORS as exc:
        return None, ToolchainProblem(
            "typescript_unreadable", f"{shown}: could not read package.json ({type(exc).__name__}: {exc})", shown, fix
        )
    name = data.get("name") if isinstance(data, dict) else None
    if name != "typescript":
        return None, ToolchainProblem(
            "typescript_unreadable", f"{shown}: package name is {name!r}, expected 'typescript'", shown, fix
        )
    version = data.get("version")
    if not isinstance(version, str) or not version:
        return None, ToolchainProblem("typescript_unreadable", f"{shown}: no version field", shown, fix)
    if (parsed := _major_minor(version)) is not None and parsed < MIN_TYPESCRIPT:
        # The helper uses the TypeScript 7 API: 6 and older expose a different,
        # in-process one, and 7.0 has this one only under unstable paths.
        needed = ".".join(map(str, MIN_TYPESCRIPT))
        unsupported_fix = (
            f"set {TS_DIR_ENV} to a directory holding TypeScript {needed} or newer"
            if source == "env"
            else FRONTEND_TS_FIX
        )
        return None, ToolchainProblem(
            "typescript_unsupported",
            f"{shown}: typescript {version} has no compiler API the helper can use; it needs {needed} or newer",
            shown,
            unsupported_fix,
        )
    return version, None


def _major_minor(version: str) -> tuple[int, int] | None:
    """``(7, 1)`` for ``7.1.0-beta``; None for a version that does not start with two numbers."""
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def find_toolchain(
    repo_root: Path,
    *,
    env: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> Toolchain:
    """Probe node and TypeScript without running the helper.

    Node comes from ``$CODE_INTEL_NODE`` when it is set and non-empty (a path, or a
    command name looked up on PATH), otherwise from ``node`` on PATH.
    TypeScript comes from ``$CODE_INTEL_TYPESCRIPT_DIR`` when it is set and
    non-empty (a relative value is taken relative to ``repo_root``), otherwise
    from the install ``code-intel setup`` made in the cache (``install_dir``). A
    version that differs from the packaged ``ts/package-lock.json`` is a warning,
    not a problem. When several things are wrong, ``problem`` reports node first.
    """
    node, node_source, node_problem = _find_node(env, which, repo_root)
    node_version, version_problem = _probe_node_version(node, node_source, env.get(NODE_ENV, ""), run)
    ts_dir, source, raw = _typescript_dir(env, repo_root)
    typescript_version, ts_problem = _typescript_package(ts_dir, repo_root, source, raw)
    lockfile_version, warnings = _lockfile_warnings(repo_root, ts_dir, typescript_version)
    problems = [p for p in (node_problem, version_problem, ts_problem) if p is not None]
    return Toolchain(
        node=node,
        node_version=node_version,
        typescript_dir=ts_dir if typescript_version else None,
        typescript_version=typescript_version,
        typescript_source=source if typescript_version else None,
        lockfile_typescript_version=lockfile_version,
        problem=problems[0] if problems else None,
        warnings=warnings,
        repo_root=repo_root,
        node_source=node_source if node is not None else None,
        compiler_stamp=compiler_stamp(ts_dir) if typescript_version else None,
        node_stamp=node_stamp(node),
    )


def node_identity(
    repo_root: Path, *, env: Mapping[str, str] = os.environ, which: Callable[[str], str | None] = shutil.which
) -> tuple[str | None, str | None]:
    """(the node path ``find_toolchain`` would choose now, its stat facts),
    without running it: a stat per PATH entry, so callers can check a cached
    toolchain's node on every call."""
    node, _, _ = _find_node(env, which, repo_root)
    return node, node_stamp(node)


def typescript_entry(ts_dir: Path) -> Path:
    """The file the helper loads for the compiler API: the ``./sync`` entry of
    package.json "exports", else package.json itself (the package has no "main")."""
    package_json = ts_dir / "package.json"
    try:
        exports = json.loads(package_json.read_text(encoding="utf-8")).get("exports")
    except _JSON_READ_ERRORS:
        exports = None
    entry = exports.get("./sync") if isinstance(exports, dict) else None
    return ts_dir / entry if isinstance(entry, str) and entry else package_json


def compiler_stamp(ts_dir: Path | None) -> str | None:
    """The compiler entry file's stat facts, or None when it cannot be read."""
    return _stat_facts(typescript_entry(ts_dir)) if ts_dir is not None else None


def node_stamp(node: str | None) -> str | None:
    """The node binary's stat facts, or None when it is gone. Symlinks are
    followed, so a version manager or package manager that repoints a stable
    path at another install changes it."""
    return _stat_facts(Path(node)) if node is not None else None


def _stat_facts(path: Path) -> str | None:
    try:
        st = FileStamp.from_stat(path.stat())
    except OSError:
        return None
    return f"{st.mtime_ns}:{st.size}:{st.ino}:{st.ctime_ns}"


def _probe_node_version(
    node: str | None, node_source: Literal["env", "path"] | None, raw: str, run: Callable[..., Any]
) -> tuple[str | None, ToolchainProblem | None]:
    """``node --version`` for a found node; a problem names ``$CODE_INTEL_NODE`` when it chose the binary."""
    if node is None:
        return None, None
    # Only the real subprocess.run is cached, so injected fakes stay deterministic.
    version, problem = _node_version(node, run, cache=run is subprocess.run)
    if problem is None or node_source != "env":
        return version, problem
    return version, ToolchainProblem(
        problem.code, problem.message.replace(node, _env_label(NODE_ENV, raw, node), 1), problem.missing, NODE_ENV_FIX
    )


def cache_dir(env: Mapping[str, str] = os.environ) -> Path:
    """``$CODE_INTEL_CACHE_DIR``, else ``~/.cache/code-intel``."""
    override = env.get(CACHE_ENV, "")
    return Path(override).expanduser().absolute() if override else Path.home() / ".cache" / "code-intel"


def install_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where ``code-intel setup`` installs the helper's TypeScript: a directory
    named by the packaged lockfile's digest, so every checkout, worktree and
    repository on the machine shares one install per pinned version.

    It is the helper's own TypeScript, not the frontend's: the helper needs the
    7.1 line, whose compiler API is at stable paths (7.0 has it only under
    unstable ones), and a second TypeScript in frontend/node_modules would race
    the frontend's own ``tsc`` for the .bin link."""
    digest = hashlib.sha256(toolchain_pins.HELPER_LOCK.read_bytes()).hexdigest()[:16]
    return cache_dir(env) / "ts" / digest


def _typescript_dir(env: Mapping[str, str], repo_root: Path) -> tuple[Path, Literal["env", "helper"], str]:
    """(directory, where it came from, the raw ``$CODE_INTEL_TYPESCRIPT_DIR``)."""
    override = env.get(TS_DIR_ENV, "")
    if not override:
        return install_dir(env) / "node_modules" / "typescript", "helper", ""
    ts_dir = Path(override).expanduser()
    return (ts_dir if ts_dir.is_absolute() else repo_root / ts_dir), "env", override


def _lockfile_warnings(
    repo_root: Path, ts_dir: Path, typescript_version: str | None
) -> tuple[str | None, tuple[str, ...]]:
    """(the lockfile's TypeScript version, a warning when the found one differs)."""
    lockfile_version = toolchain_pins.helper_typescript_version(toolchain_pins.HELPER_LOCK)
    if not (typescript_version and lockfile_version) or typescript_version == lockfile_version:
        return lockfile_version, ()
    return lockfile_version, (
        f"typescript {typescript_version} at {rel_posix(ts_dir, repo_root)} differs from "
        f"the packaged lockfile ({lockfile_version}); results may differ from CI",
    )


def _find_node(
    env: Mapping[str, str], which: Callable[[str], str | None], repo_root: Path
) -> tuple[str | None, Literal["env", "path"] | None, ToolchainProblem | None]:
    """(absolute node path, where it came from, problem). ``$CODE_INTEL_NODE``
    must name an executable file, either as a path (a relative one is taken
    relative to ``repo_root``, like ``$CODE_INTEL_TYPESCRIPT_DIR``) or as a command
    found on PATH. The path is absolute because the helper runs with
    ``repo_root`` as its working directory, not the caller's."""
    override = env.get(NODE_ENV, "")
    if not override:
        node = which("node")
        if node is None:
            return None, None, ToolchainProblem("node_not_found", "node not found on PATH", "node", NODE_FIX)
        return os.path.abspath(node), "path", None
    candidate = Path(override).expanduser()
    if os.sep in override or (os.altsep and os.altsep in override) or candidate.is_absolute():
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        resolved = os.path.abspath(candidate)
        where = _env_label(NODE_ENV, override, resolved)
        if not candidate.is_file():
            return None, "env", ToolchainProblem("node_not_found", f"{where}: no such file", override, NODE_ENV_FIX)
        if not os.access(resolved, os.X_OK):
            return (
                None,
                "env",
                ToolchainProblem("node_not_found", f"{where}: file is not executable", override, NODE_ENV_FIX),
            )
        return resolved, "env", None
    found = which(override)
    if found is None:
        return (
            None,
            "env",
            ToolchainProblem(
                "node_not_found", f"{NODE_ENV}={override}: command not found on PATH", override, NODE_ENV_FIX
            ),
        )
    return os.path.abspath(found), "env", None


# ── Helper code identity ─────────────────────────────────────────────────

_digest_lock = threading.Lock()
_digest_cache: tuple[tuple[tuple[str, FileStamp], ...], str] | None = None


def helper_files() -> list[Path]:
    """The helper's ``ts/*.cjs`` files, sorted, for code watching and cache stamps."""
    return sorted(HELPER_DIR.glob("*.cjs"))


def helper_digest() -> str:
    """SHA-256 over the helper files' names and bytes, recomputed only when their stats change."""
    global _digest_cache
    files = helper_files()
    # The stamp has ctime, so a script rewritten with its mtime set back still misses.
    signature = tuple((f.name, FileStamp.from_stat(f.stat())) for f in files)
    with _digest_lock:
        if _digest_cache is not None and _digest_cache[0] == signature:
            return _digest_cache[1]
    digest = hashlib.sha256(usedforsecurity=False)
    for f in files:
        digest.update(f.name.encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    value = digest.hexdigest()
    with _digest_lock:
        _digest_cache = (signature, value)
    return value


# ── Running the helper ───────────────────────────────────────────────────


def run_helper(
    toolchain: Toolchain,
    mode: HelperMode,
    request: dict,
    *,
    timeout_s: float,
    run: Callable[..., Any] = subprocess.run,
) -> dict:
    """Run one helper batch and return its parsed response.

    ``protocol``, ``mode`` and ``typescript_dir`` are filled in from the
    arguments. Raises ``ToolchainUnavailable`` when the toolchain is not
    available and ``HelperError`` naming the failed step otherwise.
    """
    if not toolchain.available or toolchain.node is None or toolchain.typescript_dir is None:
        raise ToolchainUnavailable(incomplete_toolchain_problem(toolchain))
    payload = {**request, "protocol": HELPER_PROTOCOL, "mode": mode, "typescript_dir": str(toolchain.typescript_dir)}
    try:
        # ASCII escapes keep lone surrogates from Python paths from failing the encode.
        data = json.dumps(payload, ensure_ascii=True).encode("ascii")
    except _ENCODE_ERRORS as exc:
        raise HelperError(mode, "write request", f"{type(exc).__name__}: {exc}") from exc
    cwd = toolchain.repo_root if toolchain.repo_root is not None else TOOL_DIR
    try:
        proc = run(
            [toolchain.node, str(HELPER)],
            input=data,
            capture_output=True,
            timeout=timeout_s,
            cwd=str(cwd),
            check=False,
            creationflags=_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise HelperError(mode, "timeout", f"no response within {timeout_s:g} s") from exc
    except _RUN_ERRORS as exc:
        raise HelperError(mode, "start", f"{toolchain.node}: {type(exc).__name__}: {exc}") from exc
    stdout: bytes = proc.stdout or b""
    _check_exit(mode, proc.returncode, stdout, proc.stderr or b"")
    return _parse_response(mode, stdout)


def _check_exit(mode: HelperMode, returncode: int, stdout: bytes, stderr: bytes) -> None:
    """Raise ``HelperError`` for a non-zero exit, naming the protocol error
    the helper reported when it rejected the request."""
    if returncode == PROTOCOL_ERROR_EXIT:
        reason = _protocol_error(stdout)
        if reason is not None:
            raise HelperError(mode, "protocol", reason)
    if returncode != 0:
        first = _first_line(stderr.decode("utf-8", "replace"))
        raise HelperError(mode, "exit", f"node exited with code {returncode}: {first or '(no stderr)'}")


def _parse_response(mode: HelperMode, stdout: bytes) -> dict:
    """The helper's JSON response, checked against the protocol and mode."""
    try:
        response = json.loads(stdout.decode("utf-8"))
    except ValueError as exc:
        raise HelperError(mode, "decode output", f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(response, dict):
        raise HelperError(mode, "decode output", f"expected a JSON object, got {type(response).__name__}")
    if "protocol_error" in response:
        raise HelperError(mode, "protocol", _first_line(str(response["protocol_error"])) or "(empty protocol error)")
    if response.get("protocol") != HELPER_PROTOCOL or response.get("mode") != mode:
        raise HelperError(
            mode,
            "protocol",
            f"response has protocol {response.get('protocol')!r} and mode {response.get('mode')!r}, "
            f"expected {HELPER_PROTOCOL} and {mode!r}",
        )
    return response


def _protocol_error(stdout: bytes) -> str | None:
    try:
        body = json.loads(stdout.decode("utf-8"))
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("protocol_error"), str):
        # One line: a stack or "Require stack" after it would only repeat local paths.
        return _first_line(body["protocol_error"]) or body["protocol_error"]
    return None
