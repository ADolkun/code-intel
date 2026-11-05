#!/usr/bin/env python3
"""FastAPI runtime oracle: import an app and dump what FastAPI actually built.

Run modes:

    fastapi_oracle.py --import-root DIR --app module:attr [--repo-root R] [--package P]
                      [--env-file F] [--set K=V ...] --out OUT.json
    fastapi_oracle.py --import-root DIR --app module:attr --repo-root R
                      --capture REQUEST.json [--env-file F] [--set K=V ...] --out OUT.json
    fastapi_oracle.py --cases DIR [--repo-root R] --out OUT.json
    fastapi_oracle.py --compare RUNTIME.json DI.json

The first imports one app with ``DIR`` first on ``sys.path``. ``--env-file``
loads ``KEY=VALUE`` lines into the environment (existing values win, and no
value is ever printed); ``--set`` overrides. ``--cases`` imports every case
package of ``oracles/fastapi_cases.py`` and ``oracles/fastapi_dependency_cases.py``
present under ``DIR`` in one process, keyed by case id.

For each route, in Starlette dispatch order (``app.routes``, entering mounted
sub-applications, skipping FastAPI's own docs routes; from FastAPI 0.137, where
``include_router`` keeps the child router live, the routes
``fastapi.routing.iter_route_contexts`` gives, each as served), and the
``fastapi_version`` that built them:

- ``method``, ``path``, ``kind`` (``http``/``websocket``), ``handler``,
  ``handler_id`` (``module:qualname``), ``file`` (repo-relative POSIX when
  ``--repo-root`` is given) and ``line`` (the ``def`` line, not a decorator);
- ``library`` (with ``--package``, the backend package's name): where installed
  code defines the handler (``dist``, ``file`` relative to the distribution,
  ``line``), null for a handler of the package;
- ``route_deps``: the route-level dependencies (the first
  ``len(route.dependencies)`` of ``route.dependant.dependencies``) in FastAPI's
  order, each with ``via``: ``app``, ``router`` or ``route decorator``;
- ``param_deps``: the handler parameters' dependencies, each with its
  sub-dependencies recursively (``sub``);
- ``closure``: every dependency id reachable from the route, first-seen order.

A dependency callable is identified as ``module:qualname``: a class as ``C``,
an instance as ``C()`` (FastAPI reads its ``__call__``, not ``__init__``), a
method bound to an instance as ``C().m`` (FastAPI skips its first parameter)
and through the class or bound to the class as ``C.m``, a ``functools.partial``
by what it wraps, a closure ``X.<locals>.Y`` by ``X``, and a lambda as
``<lambda>@L<line>``.

``--capture`` imports the app like the first mode, and instead of the routes
records what installed code contributed to the places REQUEST.json names
(``includes``: ``{file, start, end}`` line spans of ``include_router`` calls;
``dependencies``: ``{binding: "module:name"}`` or ``{external: "dotted.name"}``):
the routes of each included router and the dependency trees, where a callable
of the package (``package`` in the request) is a leaf, with the definition
(distribution, file, line) of every installed callable and the version of each
distribution named. It writes one result per requested item, or the reason.

``--compare`` checks DI JSON (schema v2) against a runtime dump, reading an
analyzer id that names a dependency the library capture filled (``runtime_id``)
as the id FastAPI reports, and prints the
differences by class: ``route_missing``, ``route_extra``, ``order_mismatch``
(Starlette dispatch order), ``handler_mismatch``, ``location_mismatch`` (kind,
file and ``def`` line), ``route_deps_mismatch``, ``via_mismatch`` (app, router
or route decorator per route-level dependency), ``param_deps_mismatch``,
``closure_mismatch`` and ``redirect_slashes_mismatch``. It exits 1 when there
is any. A route registered twice is compared per occurrence (``#2``).
"""

from __future__ import annotations

import argparse
import functools
import importlib
import inspect
import json
import os
import re
import sys
import typing
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

ORACLES_DIR = Path(__file__).resolve().parent
CASE_MODULES = (("fastapi_cases", "CASES"), ("fastapi_dependency_cases", "DEPENDENCY_CASES"))
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s")
_SOURCE_ERRORS = (OSError, TypeError)
_SIGNATURE_ERRORS = (NameError, TypeError, ValueError, SyntaxError)
CLASSES = (
    "route_missing",
    "route_extra",
    "order_mismatch",
    "handler_mismatch",
    "location_mismatch",
    "route_deps_mismatch",
    "via_mismatch",
    "param_deps_mismatch",
    "closure_mismatch",
    "redirect_slashes_mismatch",
)


# ── Instrumentation ──────────────────────────────────────────────────────


class _Registrations:
    """How many route-level dependencies each endpoint got from its decorator,
    recorded when a route is first registered (not when an include copies it).

    FastAPI builds ``route.dependencies`` as app deps, then each include's and
    router's deps, then the decorator's, so position tells where each came from.
    """

    def __init__(self) -> None:
        self.decorator_counts: dict[int, set[int]] = defaultdict(set)  # id(endpoint) -> counts seen
        # id(endpoint) -> (path as registered, methods, count) per registration, so
        # stacked decorators on one endpoint keep their own counts.
        self.registrations: dict[int, list[tuple[str, frozenset[str], int]]] = defaultdict(list)
        self.depth = 0

    def install(self) -> None:
        from fastapi import routing

        original_add = routing.APIRouter.add_api_route
        original_ws = routing.APIRouter.add_api_websocket_route
        original_include = routing.APIRouter.include_router
        registrations = self

        def add_api_route(router, path, endpoint, *args, **kwargs):
            registrations.record(endpoint, kwargs.get("dependencies"), path, kwargs.get("methods"))
            return original_add(router, path, endpoint, *args, **kwargs)

        def add_api_websocket_route(router, path, endpoint, *args, **kwargs):
            registrations.record(endpoint, kwargs.get("dependencies"), path, None)
            return original_ws(router, path, endpoint, *args, **kwargs)

        def include_router(router, *args, **kwargs):
            registrations.depth += 1
            try:
                return original_include(router, *args, **kwargs)
            finally:
                registrations.depth -= 1

        routing.APIRouter.add_api_route = add_api_route
        routing.APIRouter.add_api_websocket_route = add_api_websocket_route
        routing.APIRouter.include_router = include_router

    def record(self, endpoint, dependencies, path, methods) -> None:
        if self.depth == 0:
            count = len(dependencies or ())
            self.decorator_counts[id(endpoint)].add(count)
            upper = frozenset(m.upper() for m in methods or ())
            self.registrations[id(endpoint)].append((str(path), upper, count))

    def decorator_count(self, endpoint, path: str, methods) -> int | None:
        """The decorator's dependency count for this route, or None when ambiguous."""
        counts = self.decorator_counts.get(id(endpoint), set())
        if len(counts) == 1:
            return next(iter(counts))
        # Stacked decorators: an include only prepends a prefix, so the registered
        # path is a suffix of the live one and the methods are unchanged.
        upper = frozenset(m.upper() for m in methods or ())
        matching = {
            count
            for registered, registered_methods, count in self.registrations.get(id(endpoint), ())
            if path.endswith(registered) and registered_methods == upper
        }
        return next(iter(matching)) if len(matching) == 1 else None


REGISTRATIONS = _Registrations()


# ── Identity ─────────────────────────────────────────────────────────────


def _outer_qualname(qualname: str) -> str:
    """``X.<locals>.Y`` -> ``X``: a closure is identified by the function that made it."""
    return qualname.split(".<locals>", 1)[0]


def _instance_qualname(qualname: str, *, member: bool) -> str:
    """``C`` -> ``C()`` for an instance, ``C.m`` -> ``C().m`` for a method bound to one."""
    owner, dot, attr = qualname.rpartition(".")
    return _outer_qualname(f"{owner}().{attr}" if member and dot else f"{qualname}()")


def callable_id(call) -> str:
    if isinstance(call, functools.partial):
        return callable_id(call.func)
    if inspect.ismethod(call):
        func = call.__func__
        if inspect.isclass(call.__self__) or not inspect.isfunction(func):
            return callable_id(func)
        return f"{func.__module__}:{_instance_qualname(func.__qualname__, member=True)}"
    if inspect.isclass(call):
        return f"{call.__module__}:{_outer_qualname(call.__qualname__)}"
    if inspect.isfunction(call):
        qualname = call.__qualname__
        if "<locals>" in qualname:
            return f"{call.__module__}:{_outer_qualname(qualname)}"
        if call.__name__ == "<lambda>":
            return f"{call.__module__}:<lambda>@L{call.__code__.co_firstlineno}"
        return f"{call.__module__}:{qualname}"
    if inspect.isbuiltin(call):
        return f"{getattr(call, '__module__', None) or 'builtins'}:{call.__qualname__}"
    cls = type(call)
    return f"{cls.__module__}:{_instance_qualname(cls.__qualname__, member=False)}"


def endpoint_id(func) -> str:
    """``module:qualname`` of a route handler, ``<locals>`` kept: handlers are not
    merged like closures. A lambda is ``<lambda>@L<line>``, as two lambdas share a name."""
    target = inspect.unwrap(func)
    qualname = target.__qualname__
    if getattr(target, "__name__", "") == "<lambda>" and qualname.endswith("<lambda>"):
        qualname = f"{qualname}@L{target.__code__.co_firstlineno}"
    return f"{target.__module__}:{qualname}"


def _rel(path: str | None, repo_root: Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path).resolve()
    if repo_root is not None:
        try:
            return p.relative_to(repo_root).as_posix()
        except ValueError:
            pass
    return p.as_posix()


def def_location(func, repo_root: Path | None) -> tuple[str | None, int | None]:
    """(file, line of the ``def``) for a function, skipping its decorators."""
    target = inspect.unwrap(func)
    try:
        path = inspect.getsourcefile(target)
        lines, start = inspect.getsourcelines(target)
    except _SOURCE_ERRORS:
        return None, None
    line = start
    if getattr(target, "__name__", "") != "<lambda>":
        for offset, text in enumerate(lines):
            if _DEF_RE.match(text):
                line = start + offset
                break
    return _rel(path, repo_root), line


# ── Walk ─────────────────────────────────────────────────────────────────


def _param_kind(endpoint, name: str | None) -> str:
    from fastapi import params

    try:
        signature = inspect.signature(endpoint, eval_str=True)
    except _SIGNATURE_ERRORS:
        signature = inspect.signature(endpoint)
    parameter = signature.parameters.get(name or "")
    if parameter is None:
        return "unknown"
    if isinstance(parameter.default, params.Depends):
        return "default"
    if typing.get_origin(parameter.annotation) is typing.Annotated:
        return "annotated"
    return "unknown"


def _dep_tree(dependant, seen: tuple[int, ...] = ()) -> dict:
    node = {"id": callable_id(dependant.call), "param": dependant.name, "sub": []}
    if id(dependant) in seen:
        return node
    node["sub"] = [_dep_tree(d, (*seen, id(dependant))) for d in dependant.dependencies if d.call is not None]
    return node


def _closure(nodes: list[dict]) -> list[str]:
    out: list[str] = []
    todo = list(reversed(nodes))
    while todo:
        node = todo.pop()
        if node["id"] not in out:
            out.append(node["id"])
        todo.extend(reversed(node["sub"]))
    return out


def _served_routes(route_list):
    """``(original route, route as served)`` for each route, in dispatch order.

    Up to FastAPI 0.136, ``include_router`` copies the child's routes into the
    parent, so a route list holds them as served. From 0.137 it keeps the child
    router live behind one ``_IncludedRouter`` entry, and
    ``fastapi.routing.iter_route_contexts`` (0.137.2+) gives each route as
    served: the full path and the include's dependencies. An API route is then
    its route context, which reads like an ``APIRoute``; other routes are the
    Starlette route FastAPI builds for them."""
    import fastapi
    from fastapi import routing

    iterate = getattr(routing, "iter_route_contexts", None)
    if iterate is None:
        for route in route_list:
            if type(route).__name__ == "_IncludedRouter":
                # 0.137.0 and 0.137.1 keep includes live but cannot list the routes as served.
                raise SystemExit(
                    f"walking app.routes failed: FastAPI {fastapi.__version__} keeps included routers live "
                    "but has no fastapi.routing.iter_route_contexts; use FastAPI 0.137.2 or later"
                )
            yield route, route
        return
    for context in iterate(route_list):
        original = context.original_route
        # The private field holds the effective route for an included one; there is no public accessor.
        effective = getattr(context, "_route_context", None)
        if effective is None:
            yield original, original
        elif isinstance(original, routing.APIRoute):
            yield original, context
        else:
            yield original, effective.starlette_route or original


def walk_app(app, repo_root: Path | None, installed: _Installed | None = None) -> dict:
    """Every route of ``app`` in dispatch order, entering mounted applications.
    ``installed`` attributes a handler of installed code to its distribution."""
    from fastapi import FastAPI, routing
    from starlette.routing import Mount, Route, WebSocketRoute

    routes: list[dict] = []
    mounts: list[dict] = []
    other: list[dict] = []

    def visit(route_list, prefix: str, n_app: int, app_id: str) -> None:
        for original, route in _served_routes(route_list):
            if isinstance(original, (routing.APIRoute, routing.APIWebSocketRoute)):
                is_ws = isinstance(original, routing.APIWebSocketRoute)
                routes.extend(_api_route_rows(route, is_ws, prefix, n_app, app_id, repo_root, len(routes), installed))
            elif isinstance(route, Mount):
                visit_mount(route, prefix, app_id)
            elif isinstance(route, (Route, WebSocketRoute)):
                row = _plain_route_row(route, prefix, repo_root)
                if row is not None:
                    other.append(row)
            else:
                other.append({"path": prefix + getattr(route, "path", "?"), "kind": type(route).__name__})

    def visit_mount(route, prefix: str, app_id: str) -> None:
        sub_app = route.app
        if isinstance(sub_app, FastAPI):
            mounts.append({"path": prefix + route.path, "app": "FastAPI"})
            visit(sub_app.routes, prefix + route.path, len(sub_app.router.dependencies), f"mount:{prefix + route.path}")
            return
        mounts.append({"path": prefix + route.path, "app": type(sub_app).__name__})
        if getattr(route, "routes", None):
            visit(route.routes, prefix + route.path, 0, app_id)

    visit(app.routes, "", len(app.router.dependencies), "app")
    return {
        "redirect_slashes": bool(app.router.redirect_slashes),
        "routes": routes,
        "mounts": mounts,
        "other_routes": other,
        "summary": {
            "routes": len(routes),
            "http": sum(1 for r in routes if r["kind"] == "http"),
            "websocket": sum(1 for r in routes if r["kind"] == "websocket"),
        },
    }


def _api_route_rows(
    route,
    is_ws: bool,
    prefix: str,
    n_app: int,
    app_id: str,
    repo_root: Path | None,
    order: int,
    installed: _Installed | None,
) -> list[dict]:
    """One row per method of a FastAPI route (``WS`` for a WebSocket route)."""
    endpoint = route.endpoint
    file, line = def_location(endpoint, repo_root)
    route_deps, param_deps = _route_dependencies(route, endpoint, n_app, is_ws)
    row = {
        "path": prefix + route.path,
        "kind": "websocket" if is_ws else "http",
        "handler": getattr(endpoint, "__name__", "?"),
        "handler_id": endpoint_id(endpoint),
        "file": file,
        "line": line,
        "library": _library_of(endpoint, installed),
        "app": app_id,
        "route_deps": route_deps,
        "param_deps": param_deps,
        "closure": _closure([*route_deps, *param_deps]),
    }
    methods = ["WS"] if is_ws else sorted(route.methods or ())
    return [{"method": method, "order": order + k, **row} for k, method in enumerate(methods)]


def _library_of(endpoint, installed: _Installed | None) -> dict | None:
    """Where installed code defines ``endpoint``; None for a handler of the package or without ``installed``."""
    if installed is None:
        return None
    try:
        return installed.location(endpoint)
    except _NotCapturable:
        return None


def _layered_dependants(route, endpoint, n_app: int, is_ws: bool) -> list[tuple[Any, str]]:
    """The route's dependants with the layer each comes from: ``app``, ``router``,
    ``route decorator`` (``unknown`` when stacked decorators leave it ambiguous)
    for the route-level ones, ``direct`` for those of the handler's parameters."""
    n_route = len(route.dependencies)
    n_decorator = REGISTRATIONS.decorator_count(
        endpoint, route.path, None if is_ws else getattr(route, "methods", None)
    )
    out: list[tuple[Any, str]] = []
    for index, dependant in enumerate(route.dependant.dependencies):
        if dependant.call is None:
            continue
        if index >= n_route:
            layer = "direct"
        elif index < n_app:
            layer = "app"
        elif n_decorator is None:
            layer = "unknown"
        else:
            layer = "route decorator" if index >= n_route - n_decorator else "router"
        out.append((dependant, layer))
    return out


def _route_dependencies(route, endpoint, n_app: int, is_ws: bool) -> tuple[list[dict], list[dict]]:
    """The route's dependency layers (app, router, decorator) and its parameters'
    dependencies, each a tree, in FastAPI's order."""
    route_deps: list[dict] = []
    param_deps: list[dict] = []
    for dependant, layer in _layered_dependants(route, endpoint, n_app, is_ws):
        tree = _dep_tree(dependant)
        if layer == "direct":
            param_deps.append({**tree, "how": _param_kind(endpoint, dependant.name)})
        else:
            route_deps.append({**tree, "param": None, "via": layer})
    return route_deps, param_deps


def _plain_route_row(route, prefix: str, repo_root: Path | None) -> dict | None:
    """A Starlette ``Route`` or ``WebSocketRoute``; None for FastAPI's own
    ``/docs``, ``/redoc`` and ``/openapi.json``."""
    module = getattr(route.endpoint, "__module__", "") or ""
    if module.startswith("fastapi."):
        return None
    file, line = def_location(route.endpoint, repo_root)
    return {
        "path": prefix + route.path,
        "kind": type(route).__name__,
        "methods": sorted(getattr(route, "methods", None) or ()),
        "handler_id": endpoint_id(route.endpoint),
        "file": file,
        "line": line,
    }


def load_app(spec: str):
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"--app {spec!r}: expected module:attribute")
    factory = attribute.endswith("()")
    app: Any = importlib.import_module(module_name)
    for part in attribute.removesuffix("()").split("."):
        app = getattr(app, part)
    return app() if factory else app


# ── Capture ──────────────────────────────────────────────────────────────


class _NotCapturable(Exception):
    """A requested fact cannot be recorded; the message says why."""


class _IncludeSites:
    """The router handed to each ``include_router`` call written in the package at a requested place.

    The call is attributed to the innermost frame outside FastAPI, so a router
    that library code includes while it builds is not mistaken for the consumer's.
    """

    def __init__(self, repo_root: Path, spans: list[dict]) -> None:
        self.repo_root = repo_root
        self.spans = spans
        self.routers: list[list[Any]] = [[] for _ in spans]

    def install(self) -> None:
        from fastapi import routing

        original = routing.APIRouter.include_router
        sites = self

        def include_router(router, *args, **kwargs):
            sites.record(sys._getframe(1), args[0] if args else kwargs.get("router"))
            return original(router, *args, **kwargs)

        routing.APIRouter.include_router = include_router

    def record(self, frame, child) -> None:
        while frame is not None and frame.f_globals.get("__name__", "").split(".")[0] == "fastapi":
            frame = frame.f_back
        if frame is None:
            return
        try:
            file = Path(frame.f_code.co_filename).resolve().relative_to(self.repo_root).as_posix()
        except OSError, ValueError:
            return
        for index, span in enumerate(self.spans):
            if span["file"] == file and span["start"] <= frame.f_lineno <= span["end"]:
                self.routers[index].append(child)


def _normalized(dist: str) -> str:
    return re.sub(r"[-_.]+", "-", dist).lower()


class _Installed:
    """Which installed distribution defines a callable, and the versions of those named."""

    def __init__(self, package: str) -> None:
        import importlib.metadata

        self.package = package
        self.metadata = importlib.metadata
        self.owners = importlib.metadata.packages_distributions()
        roots = {Path(entry).resolve() for entry in sys.path if entry and Path(entry).is_dir()}
        self.roots = sorted(roots, key=lambda root: len(root.parts), reverse=True)
        self.versions: dict[str, str] = {"fastapi": importlib.metadata.version("fastapi")}

    def in_package(self, callable_id: str) -> bool:
        return callable_id.split(":", 1)[0].split(".")[0] == self.package

    def location(self, call, *, folded: bool = False) -> dict:
        """``folded``: a closure is located at the function that made it, as its id names that function."""
        file, line = _definition(_outer_function(call) if folded else call)
        if file is None or line is None:
            raise _NotCapturable(f"{callable_id(call)} has no source file")
        path = Path(file).resolve()
        for root in self.roots:
            if path.is_relative_to(root):
                rel = path.relative_to(root)
                break
        else:
            raise _NotCapturable(f"{file} is not on the import path")
        top = rel.parts[0].removesuffix(".py")
        owners = self.owners.get(top)
        if top == self.package:
            raise _NotCapturable(f"{callable_id(call)} is defined in the backend package, not in installed code")
        if not owners:
            raise _NotCapturable(f"{rel.as_posix()} ({callable_id(call)}) is not in an installed distribution")
        dist = _normalized(sorted(owners)[0])
        self.versions[dist] = self.metadata.version(dist)
        return {"dist": dist, "file": rel.as_posix(), "line": line}


def _outer_function(call):
    """The function that made a closure dependency, else ``call``."""
    while isinstance(call, functools.partial):
        call = call.func
    if not (inspect.isfunction(call) and "<locals>" in call.__qualname__):
        return call
    outer: Any = sys.modules.get(call.__module__)
    for part in call.__qualname__.split(".<locals>", 1)[0].split("."):
        outer = getattr(outer, part, None)
    return call if outer is None else outer


def _definition(call) -> tuple[str | None, int | None]:
    """Where FastAPI reads ``call``: a function's ``def``, a class's ``class``
    line, an instance's class's ``__call__``."""
    if isinstance(call, functools.partial):
        return _definition(call.func)
    if inspect.ismethod(call):
        return _definition(call.__func__)
    if inspect.isclass(call):
        try:
            lines, start = inspect.getsourcelines(call)
            file = inspect.getsourcefile(call)
        except _SOURCE_ERRORS:
            return None, None
        offset = next((k for k, text in enumerate(lines) if text.lstrip().startswith("class ")), 0)
        return file, start + offset
    if inspect.isroutine(call):
        return def_location(call, None)
    return def_location(type(call).__call__, None)


class _Recording:
    """The dependency trees of one captured entry and the installed callables they name."""

    def __init__(self, installed: _Installed) -> None:
        self.installed = installed
        self.callables: dict[str, dict] = {}

    def node(self, dependant, *, via: str | None = None, seen: tuple[int, ...] = ()) -> dict:
        call = dependant.call
        node: dict = {"id": callable_id(call)}
        if via is not None:
            node["via"] = via
        elif dependant.name is not None:
            node["param"] = dependant.name
        if self.installed.in_package(node["id"]):
            return node
        self.callables.setdefault(node["id"], self.installed.location(call, folded=True))
        node["sub"] = (
            []
            if id(dependant) in seen
            else [self.node(d, seen=(*seen, id(dependant))) for d in dependant.dependencies if d.call is not None]
        )
        return node

    def routes(self, child) -> list[dict]:
        from fastapi import routing

        rows: list[dict] = []
        for original, route in _served_routes(child.routes):
            if not isinstance(original, (routing.APIRoute, routing.APIWebSocketRoute)):
                continue
            is_ws = isinstance(original, routing.APIWebSocketRoute)
            endpoint = route.endpoint
            location = self.installed.location(endpoint)
            route_deps: list[dict] = []
            param_deps: list[dict] = []
            for dependant, layer in _layered_dependants(route, endpoint, 0, is_ws):
                if layer == "direct":
                    param_deps.append(self.node(dependant))
                elif layer == "unknown":
                    raise _NotCapturable(
                        f"{route.path}: which router or decorator a dependency comes from is ambiguous"
                    )
                else:
                    route_deps.append(self.node(dependant, via=layer))
            for method in ["WS"] if is_ws else sorted(route.methods or ()):
                rows.append(
                    {
                        "method": method,
                        "path": route.path,
                        "kind": "websocket" if is_ws else "http",
                        "handler_id": endpoint_id(endpoint),
                        "location": location,
                        "route_deps": route_deps,
                        "param_deps": param_deps,
                    }
                )
        if not rows:
            raise _NotCapturable("the router has no routes")
        return rows

    def dependency(self, call) -> dict:
        from fastapi.dependencies.utils import get_dependant

        runtime_id = callable_id(call)
        if self.installed.in_package(runtime_id):
            raise _NotCapturable(f"it is {runtime_id}, a callable of the package, which the analysis reads from source")
        dependant = get_dependant(path="", call=call)
        return self.node(dependant)


def _import_dotted(dotted: str):
    parts = dotted.split(".")
    for count in range(len(parts), 0, -1):
        try:
            obj: Any = importlib.import_module(".".join(parts[:count]))
        except ModuleNotFoundError:
            continue
        for part in parts[count:]:
            obj = getattr(obj, part)
        return obj
    raise _NotCapturable(f"no module named {parts[0]!r}")


def _resolve_dependency(item: dict):
    if "binding" in item:
        module_name, _, name = item["binding"].partition(":")
        obj: Any = importlib.import_module(module_name)
        for part in name.split("."):
            obj = getattr(obj, part)
        return obj
    return _import_dotted(item["external"])


def capture_library(request: dict, sites: _IncludeSites, installed: _Installed) -> dict:
    """What the app built that the request asks for, one result per requested item, in order.

    A result is ``{"routes": [...], "callables": {...}}`` / ``{"sub": ...}`` or ``{"reason": ...}``.
    """
    includes: list[dict] = []
    for routers in sites.routers:
        if len(routers) != 1:
            reason = (
                "the call did not run while the app was imported"
                if not routers
                else f"the call ran {len(routers)} times"
            )
            includes.append({"reason": reason})
            continue
        recording = _Recording(installed)
        try:
            includes.append({"routes": recording.routes(routers[0]), "callables": recording.callables})
        except _NotCapturable as exc:
            includes.append({"reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one entry that cannot be read must not hide the others
            includes.append({"reason": f"{type(exc).__name__}: {exc}"})
    dependencies: list[dict] = []
    for item in request["dependencies"]:
        recording = _Recording(installed)
        try:
            node = recording.dependency(_resolve_dependency(item))
            dependencies.append({"id": node["id"], "sub": node["sub"], "callables": recording.callables})
        except _NotCapturable as exc:
            dependencies.append({"reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one entry that cannot be read must not hide the others
            dependencies.append({"reason": f"{type(exc).__name__}: {exc}"})
    return {"includes": includes, "dependencies": dependencies, "versions": installed.versions}


# ── Environment ──────────────────────────────────────────────────────────


def load_env_file(path: Path) -> int:
    """Set ``KEY=VALUE`` lines that are not already in the environment; returns how many."""
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


# ── Cases ────────────────────────────────────────────────────────────────


def load_case_definitions() -> dict:
    cases: dict = {}
    for module_name, attribute in CASE_MODULES:
        if not (ORACLES_DIR / f"{module_name}.py").is_file():
            continue
        module = importlib.import_module(f"code_intel.oracles.{module_name}")
        cases.update(getattr(module, attribute))
    return cases


def run_cases(root: Path, repo_root: Path | None) -> dict:
    out: dict = {}
    for case_id, case in sorted(load_case_definitions().items()):
        package = case.app.split(":", 1)[0].split(".", 1)[0]
        if not (root / package).is_dir():
            continue
        try:
            out[case_id] = {"app": case.app, **walk_app(load_app(case.app), repo_root)}
        except Exception as exc:  # noqa: BLE001 - one broken case must not hide the others
            out[case_id] = {"app": case.app, "error": f"{type(exc).__name__}: {exc}"}
    return {"cases": out}


# ── Compare ──────────────────────────────────────────────────────────────


def keyed(rows: list[dict]) -> dict[tuple[str, str, int], dict]:
    """Rows keyed by (method, path, occurrence), so a route registered twice compares pairwise."""
    seen: dict[tuple[str, str], int] = defaultdict(int)
    out = {}
    for row in rows:
        base = (row["method"], row["path"])
        out[(*base, seen[base])] = row
        seen[base] += 1
    return out


def _label(key: tuple[str, str, int]) -> str:
    return f"{key[0]} {key[1]}" + (f" #{key[2] + 1}" if key[2] else "")


class Difference(NamedTuple):
    """One difference between the runtime and the analyzer.

    ``key`` is the ``keyed`` key of the route it concerns, ``None`` for a
    difference in the app as a whole (dispatch order, ``redirect_slashes``).
    ``text`` is the line ``compare`` prints.
    """

    cls: str
    key: tuple[str, str, int] | None
    text: str


def differences(runtime: dict, di: Mapping) -> list[Difference]:
    out: list[Difference] = []
    expected = keyed(runtime["routes"])
    actual = keyed(sorted(di["routes"], key=lambda r: r.get("order", 0)))
    runtime_ids = {
        dep_id: entry["runtime_id"]
        for dep_id, entry in di.get("dependency_functions", {}).items()
        if "runtime_id" in entry
    }
    common = [key for key in expected if key in actual]
    analyzer_order = [key for key in actual if key in expected]
    if common != analyzer_order:
        first = next(k for k, (a, b) in enumerate(zip(common, analyzer_order, strict=False)) if a != b)
        out.append(
            Difference(
                "order_mismatch",
                None,
                f"route {first + 1} of the routes both have: runtime {_label(common[first])}, "
                f"analyzer {_label(analyzer_order[first])}",
            )
        )
    if "redirect_slashes" in runtime:
        have = (di.get("app") or {}).get("redirect_slashes")
        if have != runtime["redirect_slashes"]:
            out.append(
                Difference("redirect_slashes_mismatch", None, f"runtime {runtime['redirect_slashes']}, analyzer {have}")
            )
    for key, row in expected.items():
        got = actual.get(key)
        if got is None:
            out.append(Difference("route_missing", key, _label(key)))
        else:
            out.extend(_compare_route(key, row, got, runtime_ids))
    out.extend(Difference("route_extra", key, _label(key)) for key in actual if key not in expected)
    return out


def compare(runtime: dict, di: Mapping) -> dict[str, list[str]]:
    diffs: dict[str, list[str]] = {name: [] for name in CLASSES}
    for difference in differences(runtime, di):
        diffs[difference.cls].append(difference.text)
    return diffs


def _where(row: Mapping) -> tuple:
    """Where a handler is defined: its ``def`` in the repository, or in installed code (dist, file in the dist, line)."""
    library = row.get("library")
    if library:
        return (row.get("kind"), library["dist"], library["file"], library["line"])
    return (row.get("kind"), row.get("file"), row.get("line"))


def _compare_route(key: tuple[str, str, int], row: dict, got: dict, runtime_ids: Mapping[str, str]) -> list[Difference]:
    """One route both sides have: its handler, location and dependencies. An analyzer
    dependency the library capture filled is read as the id FastAPI reports for it (``runtime_ids``)."""

    def known(dep_id: str) -> str:
        return runtime_ids.get(dep_id, dep_id)

    label = _label(key)
    out: list[Difference] = []

    def differ(cls: str, text: str) -> None:
        out.append(Difference(cls, key, f"{label}: {text}"))

    if got.get("handler_id") != row["handler_id"]:
        differ("handler_mismatch", f"runtime {row['handler_id']}, analyzer {got.get('handler_id')}")
    want_where, have_where = _where(row), _where(got)
    if want_where != have_where:
        differ("location_mismatch", f"runtime {want_where}, analyzer {have_where}")
    deps = got.get("deps") or []
    want_route = [d["id"] for d in row["route_deps"]]
    have_route = [known(d["id"]) for d in deps if d.get("via") != "direct"]
    if want_route != have_route:
        differ("route_deps_mismatch", f"runtime {want_route}, analyzer {have_route}")
    else:
        # The runtime cannot tell a stacked decorator's dependencies apart ("unknown").
        want_via = [d["via"] for d in row["route_deps"]]
        have_via = [d.get("via") for d in deps if d.get("via") != "direct"]
        if any(w != h for w, h in zip(want_via, have_via, strict=False) if w != "unknown"):
            differ("via_mismatch", f"runtime {want_via}, analyzer {have_via}")
    want_params = sorted((d["param"], d["id"]) for d in row["param_deps"])
    have_params = sorted((d.get("param"), known(d["id"])) for d in deps if d.get("via") == "direct")
    if want_params != have_params:
        differ("param_deps_mismatch", f"runtime {want_params}, analyzer {have_params}")
    have_closure = {known(dep_id) for dep_id in got.get("all_deps") or ()}
    if set(row["closure"]) != have_closure:
        missing = sorted(set(row["closure"]) - have_closure)
        extra = sorted(have_closure - set(row["closure"]))
        differ("closure_mismatch", f"missing {missing}, extra {extra}")
    return out


def _print_compare(diffs: dict[str, list[str]], show: int = 20) -> int:
    total = 0
    for name in CLASSES:
        rows = diffs[name]
        total += len(rows)
        print(f"{name}: {len(rows)}")
        for row in rows[:show]:
            print(f"  {row}")
        if len(rows) > show:
            print(f"  ... {len(rows) - show} more")
    return 1 if total else 0


# ── CLI ──────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--import-root", type=Path)
    parser.add_argument("--app")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--package")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("RUNTIME", "DI"))
    args = parser.parse_args(argv)
    if args.compare is None and args.out is None:
        parser.error("--out is required unless --compare is given")
    if args.compare is None and args.cases is None and (args.import_root is None or args.app is None):
        parser.error("give --cases DIR, or --import-root DIR with --app module:attr")
    if args.capture is not None and (args.cases is not None or args.repo_root is None):
        parser.error("--capture needs --import-root, --app and --repo-root, and no --cases")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.compare is not None:
        runtime_path, di_path = args.compare
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        di = json.loads(di_path.read_text(encoding="utf-8"))
        return _print_compare(compare(runtime, di))

    sys.dont_write_bytecode = True
    if args.env_file is not None:
        load_env_file(args.env_file)
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"--set {item!r}: expected KEY=VALUE", file=sys.stderr)
            return 2
        os.environ[key] = value
    repo_root = args.repo_root.resolve() if args.repo_root else None
    REGISTRATIONS.install()
    if args.capture is not None and repo_root is not None:
        request = json.loads(args.capture.read_text(encoding="utf-8"))
        sites = _IncludeSites(repo_root, request["includes"])
        sites.install()
        sys.path.insert(0, str(args.import_root.resolve()))
        load_app(args.app)
        result = capture_library(request, sites, _Installed(request["package"]))
    elif args.cases is not None:
        root = args.cases.resolve()
        sys.path.insert(0, str(root))
        result = run_cases(root, repo_root)
    else:
        sys.path.insert(0, str(args.import_root.resolve()))
        app = load_app(args.app)
        result = {"app": args.app, **walk_app(app, repo_root, _Installed(args.package) if args.package else None)}
    import fastapi

    # The include model the analysis must use to compare with this run.
    result["fastapi_version"] = fastapi.__version__
    args.out.write_text(json.dumps(result, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
