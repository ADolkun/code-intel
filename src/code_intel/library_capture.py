"""The library capture file: facts about installed code that static analysis cannot see.

``code-intel capture`` writes ``.code-intel/library-capture.json`` from a running
app (``format: 1``); the analysis reads it without running anything.

- ``includes``: the routes of a router an ``include_router`` call mounts when the
  router comes from installed code, found by the call's file, the text of its
  router argument and its ``occurrence`` among the calls of that file with the
  same text.
- ``dependencies``: the dependency trees of module-level names bound to a call
  result of installed code (``binding``, ``module:name``, with ``bound_to``, the
  text of the expression it is bound to) and of installed callables named in the
  package (``external``, a dotted name).
- ``callables``: where each installed dependency callable is defined.

A dependency node with ``sub`` is a callable of installed code and the capture
owns its subtree; a node without it is a callable of the backend package, whose
subtree the analysis reads from source. New top-level keys are additive within
``format: 1``; a reader ignores the ones it does not know.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

FORMAT = 1
ROUTE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WS")
Via = Literal["router", "route decorator"]
VIAS: tuple[Via, ...] = ("router", "route decorator")
Kind = Literal["http", "websocket"]
KINDS: tuple[Kind, ...] = ("http", "websocket")


class CaptureFormatError(ValueError):
    """The text is not a valid capture file; ``str()`` says where and why."""


class Location(TypedDict):
    dist: str
    file: str  # relative to the distribution's install directory
    line: int


class DepNode(TypedDict):
    id: str
    param: NotRequired[str]  # a parameter's dependency; route-level ones have none
    via: NotRequired[Via]  # route-level dependencies only, relative to the child router
    sub: NotRequired[list[DepNode]]


class CapturedRoute(TypedDict):
    method: str
    path: str  # relative to the child router
    kind: Kind
    handler_id: str
    location: Location
    route_deps: list[DepNode]
    param_deps: list[DepNode]


class CapturedInclude(TypedDict):
    file: str
    router: str  # ast.unparse of the include's router argument
    occurrence: int
    routes: list[CapturedRoute]


class CapturedDependency(TypedDict):
    id: str  # the runtime id of the callable FastAPI reads
    sub: list[DepNode]
    binding: NotRequired[str]
    bound_to: NotRequired[str]  # the expression the binding is bound to, as the analysis reads it
    external: NotRequired[str]


class LibraryCapture(TypedDict):
    format: int
    app: str
    distributions: dict[str, str]
    includes: list[CapturedInclude]
    dependencies: list[CapturedDependency]
    callables: dict[str, Location]


def empty(app: str) -> LibraryCapture:
    return {"format": FORMAT, "app": app, "distributions": {}, "includes": [], "dependencies": [], "callables": {}}


# ── Include keys ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IncludeSite:
    """One ``include_router`` call: its line span, its router argument's text and
    its 1-based ``occurrence`` among the calls of the file with that text, in source order."""

    line: int
    end_line: int
    router: str
    occurrence: int


def _include_calls(tree: ast.Module) -> list[tuple[ast.Call, IncludeSite]]:
    calls: list[tuple[int, int, ast.Call, ast.expr]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "include_router":
            continue
        router = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "router"), None)
        if router is not None:
            calls.append((node.lineno, node.col_offset, node, router))
    seen: dict[str, int] = {}
    found: list[tuple[ast.Call, IncludeSite]] = []
    for line, _, call, router in sorted(calls, key=lambda item: item[:2]):
        text = ast.unparse(router)
        seen[text] = seen.get(text, 0) + 1
        found.append((call, IncludeSite(line, call.end_lineno or line, text, seen[text])))
    return found


def include_sites(tree: ast.Module) -> list[IncludeSite]:
    return [site for _, site in _include_calls(tree)]


def include_site_of(tree: ast.Module, call: ast.Call) -> IncludeSite | None:
    """The site of ``call``, one of the ``include_router`` calls of ``tree``."""
    return next((site for node, site in _include_calls(tree) if node is call), None)


# ── Reading ──────────────────────────────────────────────────────────────


def _fail(where: str, reason: str) -> CaptureFormatError:
    return CaptureFormatError(f"{where}: {reason}")


def _object(value: object, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _fail(where, "must be an object")
    missing = [key for key in required if key not in value]
    if missing:
        raise _fail(where, f"missing {missing[0]!r}")
    unknown = sorted(set(value) - {*required, *optional})
    if unknown:
        raise _fail(where, f"unknown key {unknown[0]!r}")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(where, "must be a non-empty string")
    return value


def _number(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(where, "must be a positive integer")
    return value


def _items(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise _fail(where, "must be a list")
    return value


def _location(value: object, where: str) -> Location:
    data = _object(value, where, ("dist", "file", "line"))
    return {
        "dist": _text(data["dist"], f"{where}.dist"),
        "file": _text(data["file"], f"{where}.file"),
        "line": _number(data["line"], f"{where}.line"),
    }


def _node(value: object, where: str, required: tuple[str, ...] = ()) -> DepNode:
    data = _object(value, where, ("id", *required), ("param", "via", "sub"))
    node: DepNode = {"id": _text(data["id"], f"{where}.id")}
    if "param" in data:
        node["param"] = _text(data["param"], f"{where}.param")
    if "via" in data:
        via = data["via"]
        if via == "router":
            node["via"] = "router"
        elif via == "route decorator":
            node["via"] = "route decorator"
        else:
            raise _fail(f"{where}.via", f"must be one of {', '.join(VIAS)}")
    if "sub" in data:
        node["sub"] = [_node(item, f"{where}.sub[{k}]") for k, item in enumerate(_items(data["sub"], f"{where}.sub"))]
    return node


def _nodes(value: object, where: str, required: tuple[str, ...] = ()) -> list[DepNode]:
    return [_node(item, f"{where}[{k}]", required) for k, item in enumerate(_items(value, where))]


def _route(value: object, where: str) -> CapturedRoute:
    data = _object(value, where, ("method", "path", "kind", "handler_id", "location", "route_deps", "param_deps"))
    method = _text(data["method"], f"{where}.method")
    if method not in ROUTE_METHODS:
        raise _fail(f"{where}.method", f"must be one of {', '.join(ROUTE_METHODS)}")
    kind = data["kind"]
    if kind == "http":
        route_kind: Kind = "http"
    elif kind == "websocket":
        route_kind = "websocket"
    else:
        raise _fail(f"{where}.kind", f"must be one of {', '.join(KINDS)}")
    if not isinstance(data["path"], str):
        raise _fail(f"{where}.path", "must be a string")
    return {
        "method": method,
        "path": data["path"],
        "kind": route_kind,
        "handler_id": _text(data["handler_id"], f"{where}.handler_id"),
        "location": _location(data["location"], f"{where}.location"),
        "route_deps": _nodes(data["route_deps"], f"{where}.route_deps", ("via",)),
        "param_deps": _nodes(data["param_deps"], f"{where}.param_deps", ("param",)),
    }


def _include(value: object, where: str) -> CapturedInclude:
    data = _object(value, where, ("file", "router", "occurrence", "routes"))
    return {
        "file": _text(data["file"], f"{where}.file"),
        "router": _text(data["router"], f"{where}.router"),
        "occurrence": _number(data["occurrence"], f"{where}.occurrence"),
        "routes": [
            _route(item, f"{where}.routes[{k}]") for k, item in enumerate(_items(data["routes"], f"{where}.routes"))
        ],
    }


def _dependency(value: object, where: str) -> CapturedDependency:
    data = _object(value, where, ("id", "sub"), ("binding", "bound_to", "external"))
    if ("binding" in data) == ("external" in data):
        raise _fail(where, "needs exactly one of 'binding' and 'external'")
    if ("bound_to" in data) != ("binding" in data):
        raise _fail(where, "missing 'bound_to'" if "binding" in data else "unknown key 'bound_to'")
    dependency: CapturedDependency = {
        "id": _text(data["id"], f"{where}.id"),
        "sub": _nodes(data["sub"], f"{where}.sub"),
    }
    if "binding" in data:
        dependency["binding"] = _text(data["binding"], f"{where}.binding")
        dependency["bound_to"] = _text(data["bound_to"], f"{where}.bound_to")
    if "external" in data:
        dependency["external"] = _text(data["external"], f"{where}.external")
    return dependency


def _library_ids(nodes: list[DepNode]) -> set[str]:
    ids: set[str] = set()
    for node in nodes:
        if "sub" in node:
            ids.add(node["id"])
            ids |= _library_ids(node["sub"])
    return ids


def from_json(data: object) -> LibraryCapture:
    """The validated capture. Raises ``CaptureFormatError``."""
    top = data if isinstance(data, dict) else {}
    if not isinstance(data, dict) or "format" not in top:
        raise _fail("the capture", "must be an object with 'format'")
    if type(top["format"]) is not int or top["format"] != FORMAT:
        raise _fail("format", f"is {top['format']!r}; this code-intel reads format {FORMAT}")
    for key in ("app", "distributions", "includes", "dependencies", "callables"):
        if key not in top:
            raise _fail("the capture", f"missing {key!r}")
    distributions = top["distributions"]
    if not isinstance(distributions, dict):
        raise _fail("distributions", "must be an object")
    callables = top["callables"]
    if not isinstance(callables, dict):
        raise _fail("callables", "must be an object")
    capture: LibraryCapture = {
        "format": FORMAT,
        "app": _text(top["app"], "app"),
        "distributions": {
            _text(name, "distributions"): _text(version, f"distributions.{name}")
            for name, version in distributions.items()
        },
        "includes": [_include(item, f"includes[{k}]") for k, item in enumerate(_items(top["includes"], "includes"))],
        "dependencies": [
            _dependency(item, f"dependencies[{k}]")
            for k, item in enumerate(_items(top["dependencies"], "dependencies"))
        ],
        "callables": {
            _text(callable_id, "callables"): _location(location, f"callables.{callable_id}")
            for callable_id, location in callables.items()
        },
    }
    _check_consistent(capture)
    return capture


def _check_consistent(capture: LibraryCapture) -> None:
    named: set[str] = set()
    dists = {entry["dist"] for entry in capture["callables"].values()}
    for include in capture["includes"]:
        for route in include["routes"]:
            named |= _library_ids(route["route_deps"]) | _library_ids(route["param_deps"])
            dists.add(route["location"]["dist"])
    for dependency in capture["dependencies"]:
        named |= {dependency["id"], *_library_ids(dependency["sub"])}
    missing = sorted(named - set(capture["callables"]))
    if missing:
        raise _fail("callables", f"has no location for {missing[0]!r}")
    unknown = sorted(dists - set(capture["distributions"]))
    if unknown:
        raise _fail("distributions", f"has no version for {unknown[0]!r}")


def loads(text: str) -> LibraryCapture:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _fail("the capture", f"is not valid JSON ({exc})") from exc
    return from_json(data)


def read(path: Path) -> LibraryCapture:
    """The capture in ``path``. Raises ``CaptureFormatError``, or ``OSError`` when it cannot be read."""
    return loads(path.read_text(encoding="utf-8"))


# ── Writing and comparing ────────────────────────────────────────────────


def dumps(capture: LibraryCapture) -> str:
    """The file's text; the same capture always gives the same bytes."""
    return json.dumps(capture, indent=1, sort_keys=True) + "\n"


def include_key(include: CapturedInclude) -> tuple[str, str, int]:
    return include["file"], include["router"], include["occurrence"]


def dependency_key(dependency: CapturedDependency) -> str:
    return dependency["binding"] if "binding" in dependency else f"external {dependency.get('external', '')}"


def differences(old: LibraryCapture, new: LibraryCapture) -> list[str]:
    """One line per entry that is added, removed or changed from ``old`` to ``new``."""
    lines: list[str] = []
    if old["app"] != new["app"]:
        lines.append(f"app: {old['app']} -> {new['app']}")
    for name in sorted(old["distributions"].keys() | new["distributions"].keys()):
        before, after = old["distributions"].get(name), new["distributions"].get(name)
        if before != after:
            lines.append(f"distribution {name}: {before or 'absent'} -> {after or 'absent'}")
    old_includes = {include_key(i): i for i in old["includes"]}
    new_includes = {include_key(i): i for i in new["includes"]}
    for key in sorted(old_includes.keys() | new_includes.keys()):
        label = f"include {key[0]} {key[1]} #{key[2]}"
        lines.extend(_entry_change(label, old_includes.get(key), new_includes.get(key)))
    old_deps = {dependency_key(d): d for d in old["dependencies"]}
    new_deps = {dependency_key(d): d for d in new["dependencies"]}
    for key in sorted(old_deps.keys() | new_deps.keys()):
        lines.extend(_entry_change(f"dependency {key}", old_deps.get(key), new_deps.get(key)))
    if not lines and old != new:
        lines.append("callables or entry order changed")
    return lines


def _entry_change(label: str, old: Mapping[str, object] | None, new: Mapping[str, object] | None) -> list[str]:
    if old is None:
        return [f"{label}: added"]
    if new is None:
        return [f"{label}: removed"]
    return [] if old == new else [f"{label}: changed"]
