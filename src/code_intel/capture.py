"""``code-intel capture``: record what installed code contributes to the app, for the analysis to apply.

Static analysis cannot see into installed code: a router a library factory
builds, the dependencies a library callable reads from its signature. This
command finds those gaps statically, imports the configured app once under the
configured Python (``[backend.capture]``), and writes what the running app
shows at exactly those places to ``.code-intel/library-capture.json``.

    code-intel capture              # write the file
    code-intel capture --check      # compare with the committed file, write nothing
    code-intel capture --if-stale   # write it only when a static check says it is stale
    code-intel capture --python P   # use P instead of the configured interpreter

A dependency behind a router the capture fills shows only once that router is
applied, so the gaps are read again with the capture so far applied, until none
is new.

A gap the app could not fill (a router that is not wholly from installed code, an
include that did not run once), and an entry the analysis would not apply once
recorded, are written to the file's ``unfilled`` key by identity only (the reason
the app gives can hold a path of this machine, so it is printed and not stored).

``--if-stale`` runs the analysis's own checks with the file applied, importing
nothing, and starts the app only when one of them finds the file stale: the file
does not exist or is malformed, the analysis reports ``library_capture_stale``, a
gap has neither an entry nor an ``unfilled`` record, or an ``unfilled`` record
matches no gap. Otherwise it exits 0. It cannot see a change to anything the
library call's result depends on other than the text of the include's router
argument or the binding's expression; ``--check`` can.

It refuses (exit 2, nothing written) when an installed version of ``fastapi``
or of a captured distribution differs from the repository's lock, since the
file would describe code the lock does not install. ``--check`` exit codes:
0 equal, 1 different, 2 could not run.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from code_intel import library_capture, library_view, problems, runner, toolchain_pins
from code_intel.config import BackendConfig, CaptureConfig
from code_intel.library_capture import CaptureFormatError, LibraryCapture
from code_intel.library_view import LibraryView

if TYPE_CHECKING:
    from code_intel.backend_model import BackendModel
    from code_intel.cli import Context
    from code_intel.result_types import DiResult


class CaptureError(Exception):
    """The capture could not be made; the message says why."""


@dataclass(frozen=True)
class IncludeGap:
    file: str
    site: library_capture.IncludeSite


@dataclass(frozen=True)
class BindingGap:
    id: str  # module:name
    bound_to: str


@dataclass(frozen=True)
class Gaps:
    """What the static analysis could not see and the running app can show."""

    includes: tuple[IncludeGap, ...]
    bindings: tuple[BindingGap, ...]  # external dependency bindings
    externals: tuple[str, ...]  # dotted names of installed callables the package uses as dependencies


def _analyze(repo_root: Path, backend: BackendConfig, library: LibraryView | None) -> tuple[BackendModel, DiResult]:
    from code_intel import di_map, router_graph
    from code_intel.repo_files import RepoFilesError, list_repo_files

    try:
        visible = list_repo_files(repo_root).files
    except RepoFilesError as exc:
        raise CaptureError(str(exc)) from exc
    model = router_graph.build_backend_model(
        repo_root / backend.root,
        backend.package,
        backend.app,
        repo_root,
        lock=backend.lock,
        library=library,
        visible=visible,
    )
    di = di_map.build_di(model)
    if di["diagnostics"]:
        raise CaptureError(problems.incomplete_banner(di["diagnostics"]))
    return model, di


def _gaps(model: BackendModel, di: DiResult) -> Gaps:
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

    bindings: set[BindingGap] = set()
    externals: set[str] = set()
    for entry in di["dependency_functions"].values():
        if entry["kind"] == "external" and "runtime_id" not in entry:
            bindings.add(BindingGap(entry["id"], entry.get("bound_to", "")))
        elif "external" in entry:
            externals.add(entry["external"])
    return Gaps(tuple(includes), tuple(sorted(bindings, key=lambda gap: gap.id)), tuple(sorted(externals)))


def static_gaps(repo_root: Path, backend: BackendConfig, library: LibraryView | None = None) -> Gaps:
    """The fillable gaps of the backend, found without importing anything. ``library``
    is a capture to apply first: what is still a gap with it applied."""
    return _gaps(*_analyze(repo_root, backend, library))


Identity = tuple[str | int, ...]


def _gap_identities(gaps: Gaps) -> set[Identity]:
    """Each gap's identity, as the capture file keys an entry."""
    return {
        *(("include", gap.file, gap.site.router, gap.site.occurrence) for gap in gaps.includes),
        *(("binding", gap.id, gap.bound_to) for gap in gaps.bindings),
        *(("external", name) for name in gaps.externals),
    }


def _include_identity(entry: Mapping[str, Any]) -> Identity:
    return ("include", entry["file"], entry["router"], entry["occurrence"])


def _dependency_identity(entry: Mapping[str, Any]) -> Identity:
    if "binding" in entry:
        return ("binding", entry["binding"], entry["bound_to"])
    return ("external", entry["external"])


def _entry_identity(entry: Mapping[str, Any]) -> Identity:
    return _include_identity(entry) if "router" in entry else _dependency_identity(entry)


def _describe(identity: Identity) -> str:
    match identity:
        case ("include", file, router, occurrence):
            return f"include_router {router} #{occurrence} at {file}"
        case ("binding", binding, bound_to):
            return f"dependency {binding} (= {bound_to})"
        case _:
            return f"dependency {identity[1]}"


def stale_reasons(repo_root: Path, backend: BackendConfig, capture_config: CaptureConfig) -> list[str]:
    """Why the capture file needs refreshing, found without starting the app; empty when it is fresh.

    The file is applied as the analysis applies it, so whatever the analysis reports as
    stale is stale here. A gap the file has no entry for is stale unless the file records
    it as unfilled, and so is an unfilled record that matches no gap."""
    view = library_view.read(repo_root, capture_config.file, app=backend.app, lock=backend.lock)
    if malformed := [problem.message for problem in view.problems if problem.is_diagnostic]:
        return malformed
    if view.capture is None and not view.problems:
        return [f"{capture_config.file} does not exist"]
    model, di = _analyze(repo_root, backend, view)
    reasons = [lim["message"] for lim in di["analysis_limitations"] if lim["kind"] == "library_capture_stale"]
    gaps = _gap_identities(_gaps(model, di))
    filled: set[Identity] = set()
    unfilled: set[Identity] = set()
    if view.capture is not None:
        filled = {_include_identity(entry) for entry in view.capture["includes"]}
        filled |= {_dependency_identity(entry) for entry in view.capture["dependencies"]}
        recorded = library_capture.unfilled_of(view.capture)
        unfilled = {_include_identity(entry) for entry in recorded["includes"]}
        unfilled |= {_dependency_identity(entry) for entry in recorded["dependencies"]}
    reasons += [
        f"{_describe(gap)} has no entry in {capture_config.file}" for gap in sorted(gaps - filled - unfilled, key=str)
    ]
    reasons += [
        f"{_describe(gap)} is recorded as not captured but is no longer a gap"
        for gap in sorted(unfilled - gaps, key=str)
    ]
    return reasons


def _request(backend: BackendConfig, gaps: Gaps) -> dict[str, Any]:
    return {
        "package": backend.package,
        "includes": [{"file": gap.file, "start": gap.site.line, "end": gap.site.end_line} for gap in gaps.includes],
        "dependencies": [{"binding": gap.id} for gap in gaps.bindings]
        + [{"external": name} for name in gaps.externals],
    }


@dataclass
class _Recorded:
    """What the oracle recorded over one or more runs, and for each gap that is not filled a note and an ``unfilled`` record."""

    includes: list[Any] = field(default_factory=list)
    dependencies: list[Any] = field(default_factory=list)
    callables: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unfilled_includes: list[Any] = field(default_factory=list)
    unfilled_dependencies: list[Any] = field(default_factory=list)

    def add(self, gaps: Gaps, result: Mapping[str, Any]) -> None:
        for gap, record in zip(gaps.includes, result["includes"], strict=True):
            label = f"include_router at {gap.file}:{gap.site.line} ({gap.site.router})"
            if "reason" in record:
                self.notes.append(f"{label}: {record['reason']}")
                self.unfilled_includes.append(
                    {"file": gap.file, "router": gap.site.router, "occurrence": gap.site.occurrence}
                )
                continue
            self.callables.update(record["callables"])
            self.includes.append(
                {
                    "file": gap.file,
                    "router": gap.site.router,
                    "occurrence": gap.site.occurrence,
                    "routes": record["routes"],
                }
            )
        requested: list[dict[str, str]] = [{"binding": gap.id, "bound_to": gap.bound_to} for gap in gaps.bindings]
        requested += [{"external": name} for name in gaps.externals]
        for item, record in zip(requested, result["dependencies"], strict=True):
            if "reason" in record:
                self.notes.append(f"dependency {item.get('binding') or item['external']}: {record['reason']}")
                self.unfilled_dependencies.append(dict(item))
                continue
            self.callables.update(record["callables"])
            self.dependencies.append({**item, "id": record["id"], "sub": record["sub"]})
        self.versions.update(result["versions"])

    def move_unapplied(self, left: Gaps) -> bool:
        """Moves each recorded entry that is still among ``left``, the gaps remaining with the record applied,
        to the unfilled ones: the analysis does not apply it. Whether any moved."""
        keys = _gap_identities(left)
        moved = [entry for entry in (*self.includes, *self.dependencies) if _entry_identity(entry) in keys]
        for entry in moved:
            identity = _entry_identity(entry)
            self.notes.append(f"{_describe(identity)}: the analysis cannot apply what the app showed for it")
            if entry in self.includes:
                self.includes.remove(entry)
                self.unfilled_includes.append({key: entry[key] for key in ("file", "router", "occurrence")})
            else:
                self.dependencies.remove(entry)
                self.unfilled_dependencies.append(
                    {key: entry[key] for key in ("binding", "bound_to", "external") if key in entry}
                )
        return bool(moved)

    def capture(self, backend: BackendConfig) -> LibraryCapture:
        named = library_capture.named_callables(self.includes, self.dependencies)
        callables = {callable_id: location for callable_id, location in self.callables.items() if callable_id in named}
        used = {"fastapi", *(location["dist"] for location in callables.values())}
        for include in self.includes:
            used.update(route["location"]["dist"] for route in include["routes"])
        data: dict[str, Any] = {
            "format": library_capture.FORMAT,
            "app": backend.app,
            "distributions": {name: self.versions[name] for name in sorted(used)},
            "includes": self.includes,
            "dependencies": self.dependencies,
            "callables": callables,
        }
        if self.unfilled_includes or self.unfilled_dependencies:
            data["unfilled"] = {
                "includes": sorted(self.unfilled_includes, key=library_capture.include_key),
                "dependencies": sorted(self.unfilled_dependencies, key=library_capture.dependency_key),
            }
        return library_capture.from_json(data)


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


def _settle(repo_root: Path, backend: BackendConfig, capture_config: CaptureConfig, recorded: _Recorded) -> Gaps:
    """The gaps left with ``recorded`` applied, after moving to the unfilled ones every entry the analysis
    does not apply. Moving one can hide a dependency behind it, so the gaps are read again until none moves."""
    while True:
        left = static_gaps(repo_root, backend, LibraryView(capture_config.file, recorded.capture(backend), ()))
        if not recorded.move_unapplied(left):
            return left


def make_capture(
    repo_root: Path, backend: BackendConfig, capture_config: CaptureConfig, python: Path
) -> tuple[LibraryCapture, list[str]]:
    """Runs the app under ``python`` and returns the capture of the gaps found statically, with notes on the unfilled ones."""
    arguments = [
        "--import-root",
        str(repo_root / backend.root),
        "--repo-root",
        str(repo_root),
        "--app",
        backend.app,
    ]
    recorded = _Recorded()
    gaps = static_gaps(repo_root, backend)
    asked: set[tuple[str, str]] = set()
    try:
        # A dependency behind a router the capture fills shows only once that router is applied, so the gaps
        # are read again until none is new. Each pass asks only gaps not asked before, and the gaps are finite.
        while True:
            result = runner.run_oracle(python, arguments, dict(capture_config.env), request=_request(backend, gaps))
            recorded.add(gaps, result)
            asked |= {("binding", gap.id) for gap in gaps.bindings} | {("external", name) for name in gaps.externals}
            later = _settle(repo_root, backend, capture_config, recorded)
            gaps = Gaps(
                (),
                tuple(gap for gap in later.bindings if ("binding", gap.id) not in asked),
                tuple(name for name in later.externals if ("external", name) not in asked),
            )
            if not gaps.bindings and not gaps.externals:
                break
        capture = recorded.capture(backend)
    except (runner.OracleRunError, CaptureFormatError) as exc:
        raise CaptureError(str(exc)) from exc
    check_versions(capture, repo_root, backend)
    return capture, recorded.notes


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare with the committed file and write nothing")
    mode.add_argument(
        "--if-stale",
        action="store_true",
        help=(
            "write the file only when a static check, which does not start the app, finds it stale "
            "(it cannot see what a library call depends on beyond its own text, such as an argument from another module; --check can)"
        ),
    )
    parser.add_argument("--python", type=Path, help="a Python with the app's dependencies (default: the config's)")


def run(args: argparse.Namespace, context: Context) -> int:
    repo_root, backend = context.repo_root, context.config.backend
    if args.if_stale and backend.capture is None:
        print(f"capture: {context.config.path} has no [backend.capture] table; nothing to refresh", file=sys.stderr)
        return 0
    if (refusal := problems.runtime_refusal(repo_root, backend.python_version_file)) is not None:
        print(refusal, file=sys.stderr)
        return 2
    capture_config = backend.capture
    if capture_config is None:
        print(f"capture: {context.config.path}: needs a [backend.capture] table with python = ...", file=sys.stderr)
        return 2
    try:
        if args.if_stale:
            if not (reasons := stale_reasons(repo_root, backend, capture_config)):
                print(f"capture: {capture_config.file} is up to date (static check)")
                return 0
            print(f"capture: {capture_config.file} is stale, starting the app:", file=sys.stderr)
            for reason in reasons:
                print(f"  {reason}", file=sys.stderr)
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
