"""The library capture as the analysis applies it.

``read`` loads ``.code-intel/library-capture.json`` and checks what static
checks can: the file's app against the configured one, and each captured
distribution's version against the lock. The router graph and the dependency
resolver then look entries up through the ``LibraryView`` and report the ones
that match nothing; no code runs.

- A malformed file is a ``parse_failed`` diagnostic on the file.
- A capture for another app is not applied.
- A capture whose distribution versions differ from the lock is applied, with a
  ``library_capture_stale`` limitation naming both versions (installed code may
  have changed its routes, so the figures are marked as possibly different).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from code_intel import library_capture, toolchain_pins
from code_intel.backend_model import CapturedNode, Library
from code_intel.library_capture import (
    CapturedDependency,
    CapturedInclude,
    CapturedRoute,
    CaptureFormatError,
    DepNode,
    LibraryCapture,
)
from code_intel.problems import Problem, diagnostic, limitation

STAGE = "read library capture"
STEP = "read library capture"
FIX = "run code-intel capture"


@dataclass(frozen=True)
class LibraryRoute:
    """One captured route, relative to the router the capture was made from."""

    method: str
    path: str
    kind: library_capture.Kind
    handler_id: str
    handler: str
    library: Library
    router_deps: tuple[CapturedNode, ...]
    decorator_deps: tuple[CapturedNode, ...]
    params: tuple[CapturedNode, ...]


@dataclass(frozen=True)
class LibraryView:
    """``capture`` is None when the file is missing, unreadable or for another app."""

    file: str  # repo-relative path of the capture file
    capture: LibraryCapture | None
    problems: tuple[Problem, ...]

    def absent(self, attempted: bool) -> str:
        """What a gap's message adds when the capture has no entry for it. A gap the capture
        recorded as unfilled is not helped by capturing again."""
        if attempted:
            return " (the capture could not fill it; code-intel capture prints why)"
        return f" (not in {self.file}; {FIX})"

    def _unfilled(self) -> library_capture.Unfilled:
        return (
            library_capture.unfilled_of(self.capture)
            if self.capture is not None
            else {"includes": [], "dependencies": []}
        )

    def attempted_include(self, file: str, router: str, occurrence: int) -> bool:
        return any(library_capture.include_key(i) == (file, router, occurrence) for i in self._unfilled()["includes"])

    def attempted_binding(self, binding_id: str, bound_to: str) -> bool:
        return any(
            d.get("binding") == binding_id and d.get("bound_to") == bound_to for d in self._unfilled()["dependencies"]
        )

    def attempted_external(self, dotted: str) -> bool:
        return any(d.get("external") == dotted for d in self._unfilled()["dependencies"])

    def stale(self, message: str) -> Problem:
        return limitation(
            "library_capture_stale", stage=STAGE, file=self.file, line=None, message=f"{self.file}: {message}; {FIX}"
        )

    def not_applied(self, entry: str, reason: str) -> Problem:
        return self.stale(f"{entry} is not applied: {reason}")

    def include(self, file: str, router: str, occurrence: int) -> CapturedInclude | None:
        if self.capture is None:
            return None
        key = (file, router, occurrence)
        return next((i for i in self.capture["includes"] if library_capture.include_key(i) == key), None)

    def binding(self, binding_id: str) -> CapturedDependency | None:
        dependencies = self.capture["dependencies"] if self.capture is not None else []
        return next((d for d in dependencies if d.get("binding") == binding_id), None)

    def external(self, dotted: str) -> CapturedDependency | None:
        dependencies = self.capture["dependencies"] if self.capture is not None else []
        return next((d for d in dependencies if d.get("external") == dotted), None)

    def library(self, callable_id: str) -> Library:
        assert self.capture is not None
        located = self.capture["callables"][callable_id]
        return Library(
            located["dist"], self.capture["distributions"][located["dist"]], located["file"], located["line"]
        )

    def node(self, node: DepNode) -> CapturedNode:
        sub = node.get("sub")
        return CapturedNode(
            id=node["id"],
            param=node.get("param"),
            library=self.library(node["id"]) if sub is not None else None,
            sub=tuple(self.node(child) for child in sub) if sub is not None else None,
        )

    def route(self, route: CapturedRoute) -> LibraryRoute:
        assert self.capture is not None
        located = route["location"]
        return LibraryRoute(
            method=route["method"],
            path=route["path"],
            kind=route["kind"],
            handler_id=route["handler_id"],
            handler=route["handler_id"].partition(":")[2].rsplit(".", 1)[-1],
            library=Library(
                located["dist"], self.capture["distributions"][located["dist"]], located["file"], located["line"]
            ),
            router_deps=tuple(self.node(d) for d in route["route_deps"] if d.get("via") == "router"),
            decorator_deps=tuple(self.node(d) for d in route["route_deps"] if d.get("via") != "router"),
            params=tuple(self.node(d) for d in route["param_deps"]),
        )


def package_ids(nodes: list[DepNode]) -> set[str]:
    """The ids of the callables of the backend package (leaves) in the trees ``nodes``."""
    ids: set[str] = set()
    for node in nodes:
        if "sub" in node:
            ids |= package_ids(node["sub"])
        else:
            ids.add(node["id"])
    return ids


def include_package_ids(include: CapturedInclude) -> set[str]:
    ids: set[str] = set()
    for route in include["routes"]:
        ids |= package_ids(route["route_deps"]) | package_ids(route["param_deps"])
    return ids


def read(repo_root: Path, file: str, *, app: str, lock: str | None) -> LibraryView:
    """The capture at ``<repo_root>/<file>`` for ``app``, with what its static checks found."""
    try:
        capture = library_capture.read(repo_root / file)
    except FileNotFoundError:
        return LibraryView(file, None, ())
    except (OSError, UnicodeDecodeError, CaptureFormatError) as exc:
        reason = str(exc) if isinstance(exc, CaptureFormatError) else f"cannot read it ({type(exc).__name__}: {exc})"
        found = diagnostic(
            "parse_failed",
            stage=STAGE,
            step=STEP,
            file=file,
            message=f"{file}: could not {STEP}: {reason}; {FIX}",
        )
        return LibraryView(file, None, (found,))
    view = LibraryView(file, capture, ())
    if capture["app"] != app:
        stale = view.not_applied("the capture", f"it was made for app {capture['app']}, not {app}")
        return LibraryView(file, None, (stale,))
    return LibraryView(file, capture, _version_problems(view, repo_root, lock))


def _version_problems(view: LibraryView, repo_root: Path, lock: str | None) -> tuple[Problem, ...]:
    assert view.capture is not None
    if lock is None:
        return ()
    found: list[Problem] = []
    for name, captured in view.capture["distributions"].items():
        try:
            locked = toolchain_pins.locked_versions(repo_root / lock, (name,))[name]
        except toolchain_pins.PinError as exc:
            found.append(view.stale(f"distribution {name} cannot be checked against the lock ({exc})"))
            continue
        if locked != captured:
            found.append(
                view.stale(
                    f"distribution {name} was captured at {captured} but {lock} locks {locked}, so the capture may differ from what the lock installs"
                )
            )
    return tuple(found)
