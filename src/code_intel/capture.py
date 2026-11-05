"""``code-intel capture``: record what installed code contributes to the app, for the analysis to apply.

Static analysis cannot see into installed code: a router a library factory
builds, the dependencies a library callable reads from its signature. This
command finds those gaps statically, imports the configured app once under the
configured Python (``[backend.capture]``), and writes what the running app
shows at exactly those places to ``.code-intel/library-capture.json``.

    code-intel capture              # write the file
    code-intel capture --check      # compare with the committed file, write nothing
    code-intel capture --python P   # use P instead of the configured interpreter

It refuses (exit 2, nothing written) when an installed version of ``fastapi``
or of a captured distribution differs from the repository's lock, since the
file would describe code the lock does not install. ``--check`` exit codes:
0 equal, 1 different, 2 could not run.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from code_intel import library_capture, problems, runner, toolchain_pins
from code_intel.backend_model import External, Located
from code_intel.config import BackendConfig, CaptureConfig
from code_intel.library_capture import CaptureFormatError, LibraryCapture

if TYPE_CHECKING:
    from code_intel.cli import Context


class CaptureError(Exception):
    """The capture could not be made; the message says why."""


@dataclass(frozen=True)
class IncludeGap:
    file: str
    site: library_capture.IncludeSite


@dataclass(frozen=True)
class Gaps:
    """What the static analysis could not see and the running app can show."""

    includes: tuple[IncludeGap, ...]
    bindings: tuple[str, ...]  # module:name of an external dependency binding
    externals: tuple[str, ...]  # dotted names of installed callables the package uses as dependencies


def static_gaps(repo_root: Path, backend: BackendConfig) -> Gaps:
    """The fillable gaps of the backend, found without importing anything."""
    from code_intel import di_map, router_graph
    from code_intel.repo_files import RepoFilesError, list_repo_files

    try:
        visible = list_repo_files(repo_root).files
    except RepoFilesError as exc:
        raise CaptureError(str(exc)) from exc
    model = router_graph.build_backend_model(
        repo_root / backend.root, backend.package, backend.app, repo_root, lock=backend.lock, visible=visible
    )
    di = di_map.build_di(model)
    if di["diagnostics"]:
        raise CaptureError(problems.incomplete_banner(di["diagnostics"]))

    modules = {module.rel: module for module in model.sources.modules.values()}
    gap_lines = {
        (limitation["file"], limitation["line"])
        for limitation in di["analysis_limitations"]
        if limitation["kind"] == "unresolved_include" and limitation["file"] is not None
    }
    includes: list[IncludeGap] = []
    for rel in sorted({file for file, _ in gap_lines}):
        module = modules.get(rel)
        if module is None:
            continue
        includes.extend(
            IncludeGap(rel, site)
            for site in library_capture.include_sites(module.tree)
            if (rel, site.line) in gap_lines
        )

    bindings: set[str] = set()
    externals: set[str] = set()
    for entry in di["dependency_functions"].values():
        if entry["kind"] == "external":
            bindings.add(entry["id"])
        elif entry["kind"] == "unresolved":
            dotted = _external_name(model.resolver, entry["module"], entry["qualname"])
            if dotted is not None:
                externals.add(dotted)
    return Gaps(tuple(includes), tuple(sorted(bindings)), tuple(sorted(externals)))


def _external_name(resolver: Any, module: str, text: str) -> str | None:
    """The dotted name ``text`` (as written in ``module``) resolves to, when it is
    one name outside the package."""
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, (ast.Name, ast.Attribute)):
        return None
    targets = resolver.resolve_expr(Located(module, node))
    if len(targets) == 1 and isinstance(targets[0], External):
        return targets[0].dotted
    return None


def _request(backend: BackendConfig, gaps: Gaps) -> dict[str, Any]:
    return {
        "package": backend.package,
        "includes": [{"file": gap.file, "start": gap.site.line, "end": gap.site.end_line} for gap in gaps.includes],
        "dependencies": [{"binding": name} for name in gaps.bindings] + [{"external": name} for name in gaps.externals],
    }


def _assemble(backend: BackendConfig, gaps: Gaps, result: Mapping[str, Any]) -> tuple[LibraryCapture, list[str]]:
    """The capture for what the oracle recorded, and a note for each gap it could not fill."""
    includes: list[Any] = []
    dependencies: list[Any] = []
    callables: dict[str, Any] = {}
    notes: list[str] = []
    for gap, record in zip(gaps.includes, result["includes"], strict=True):
        label = f"include_router at {gap.file}:{gap.site.line} ({gap.site.router})"
        if "reason" in record:
            notes.append(f"{label}: {record['reason']}")
            continue
        callables.update(record["callables"])
        includes.append(
            {"file": gap.file, "router": gap.site.router, "occurrence": gap.site.occurrence, "routes": record["routes"]}
        )
    requested = [("binding", name) for name in gaps.bindings] + [("external", name) for name in gaps.externals]
    for (key, name), record in zip(requested, result["dependencies"], strict=True):
        if "reason" in record:
            notes.append(f"dependency {name}: {record['reason']}")
            continue
        callables.update(record["callables"])
        dependencies.append({key: name, "id": record["id"], "sub": record["sub"]})
    versions = result["versions"]
    used = {"fastapi", *(location["dist"] for location in callables.values())}
    for include in includes:
        used.update(route["location"]["dist"] for route in include["routes"])
    capture = library_capture.from_json(
        {
            "format": library_capture.FORMAT,
            "app": backend.app,
            "distributions": {name: versions[name] for name in sorted(used)},
            "includes": includes,
            "dependencies": dependencies,
            "callables": callables,
        }
    )
    return capture, notes


def check_versions(capture: LibraryCapture, repo_root: Path, backend: BackendConfig) -> None:
    """Raises ``CaptureError`` when an installed distribution is not the locked version."""
    if backend.lock is None:
        raise CaptureError("[backend] lock is not set: the installed versions cannot be checked against a lock")
    lock = repo_root / backend.lock
    try:
        locked = toolchain_pins.locked_versions(lock, tuple(capture["distributions"]))
    except toolchain_pins.PinError as exc:
        raise CaptureError(str(exc)) from exc
    drift = [
        f"{name} {installed} is installed but {backend.lock} locks {locked[name]}"
        for name, installed in capture["distributions"].items()
        if installed != locked[name]
    ]
    if drift:
        directory = PurePosixPath(backend.lock).parent
        raise CaptureError(
            "the environment does not match the lock, so the capture would describe code the lock does not install: "
            + "; ".join(drift)
            + f". Run `uv sync --locked` in {directory} and capture again."
        )


def make_capture(
    repo_root: Path, backend: BackendConfig, capture_config: CaptureConfig, python: Path
) -> tuple[LibraryCapture, list[str]]:
    """Runs the app under ``python`` and returns the capture of the gaps found statically, with notes on the unfilled ones."""
    gaps = static_gaps(repo_root, backend)
    arguments = [
        "--import-root",
        str(repo_root / backend.root),
        "--repo-root",
        str(repo_root),
        "--app",
        backend.app,
    ]
    try:
        result = runner.run_oracle(python, arguments, dict(capture_config.env), request=_request(backend, gaps))
        capture, notes = _assemble(backend, gaps, result)
    except (runner.OracleRunError, CaptureFormatError) as exc:
        raise CaptureError(str(exc)) from exc
    check_versions(capture, repo_root, backend)
    return capture, notes


def compare(path: Path, capture: LibraryCapture) -> list[str]:
    """What differs between the file at ``path`` and ``capture``; empty when they are equal."""
    expected = library_capture.dumps(capture)
    try:
        committed = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"{path.name} does not exist"]
    except OSError as exc:
        raise CaptureError(f"{path}: cannot read it ({type(exc).__name__}: {exc})") from exc
    if committed == expected:
        return []
    try:
        lines = library_capture.differences(library_capture.loads(committed), capture)
    except CaptureFormatError as exc:
        return [f"the committed file is not valid: {exc}"]
    return lines or ["the file's bytes differ from what capture writes"]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--check", action="store_true", help="compare with the committed file and write nothing")
    parser.add_argument("--python", type=Path, help="a Python with the app's dependencies (default: the config's)")


def run(args: argparse.Namespace, context: Context) -> int:
    repo_root, backend = context.repo_root, context.config.backend
    if (refusal := problems.runtime_refusal(repo_root, backend.python_version_file)) is not None:
        print(refusal, file=sys.stderr)
        return 2
    capture_config = backend.capture
    if capture_config is None:
        print(f"capture: {context.config.path}: needs a [backend.capture] table with python = ...", file=sys.stderr)
        return 2
    try:
        python = runner.interpreter(args.python, capture_config, repo_root)
        capture, notes = make_capture(repo_root, backend, capture_config, python)
        path = repo_root / capture_config.file
        different = compare(path, capture) if args.check else []
    except (CaptureError, runner.OracleRunError) as exc:
        print(f"capture: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"capture: not captured, {note}", file=sys.stderr)
    routes = sum(len(include["routes"]) for include in capture["includes"])
    summary = f"{len(capture['includes'])} includes ({routes} routes), {len(capture['dependencies'])} dependencies"
    if args.check:
        if different:
            print(f"capture: {capture_config.file} is out of date; run code-intel capture", file=sys.stderr)
            for line in different:
                print(f"  {line}")
            return 1
        print(f"capture: {capture_config.file} is up to date ({summary})")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(library_capture.dumps(capture), encoding="utf-8")
    print(f"capture: wrote {capture_config.file}: {summary}")
    return 0
