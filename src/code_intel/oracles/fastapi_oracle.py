#!/usr/bin/env python3
"""FastAPI runtime oracle: import an app and dump what FastAPI actually built.

Run modes:

    fastapi_oracle.py --import-root DIR --app module:attr [--repo-root R]
                      [--env-file F] [--set K=V ...] --out OUT.json
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

``--compare`` checks DI JSON (schema v2) against a runtime dump and prints the
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


def walk_app(app, repo_root: Path | None) -> dict:
    """Every route of ``app`` in dispatch order, entering mounted applications."""
    from fastapi import FastAPI, routing
    from starlette.routing import Mount, Route, WebSocketRoute

    routes: list[dict] = []
    mounts: list[dict] = []
    other: list[dict] = []

    def visit(route_list, prefix: str, n_app: int, app_id: str) -> None:
        for original, route in _served_routes(route_list):
            if isinstance(original, (routing.APIRoute, routing.APIWebSocketRoute)):
                is_ws = isinstance(original, routing.APIWebSocketRoute)
                routes.extend(_api_route_rows(route, is_ws, prefix, n_app, app_id, repo_root, len(routes)))
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
    route, is_ws: bool, prefix: str, n_app: int, app_id: str, repo_root: Path | None, order: int
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
        "app": app_id,
        "route_deps": route_deps,
        "param_deps": param_deps,
        "closure": _closure([*route_deps, *param_deps]),
    }
    methods = ["WS"] if is_ws else sorted(route.methods or ())
    return [{"method": method, "order": order + k, **row} for k, method in enumerate(methods)]


def _route_dependencies(route, endpoint, n_app: int, is_ws: bool) -> tuple[list[dict], list[dict]]:
    """The route's dependency layers (app, router, decorator) and its parameters'
    dependencies, each a tree, in FastAPI's order."""
    n_route = len(route.dependencies)
    n_decorator = REGISTRATIONS.decorator_count(
        endpoint, route.path, None if is_ws else getattr(route, "methods", None)
    )
    route_deps: list[dict] = []
    param_deps: list[dict] = []
    for index, dependant in enumerate(route.dependant.dependencies):
        if dependant.call is None:
            continue
        tree = _dep_tree(dependant)
        if index >= n_route:
            param_deps.append({**tree, "how": _param_kind(endpoint, dependant.name)})
        elif index < n_app:
            route_deps.append({**tree, "param": None, "via": "app"})
        elif n_decorator is None:
            route_deps.append({**tree, "param": None, "via": "unknown"})
        else:
            via = "route decorator" if index >= n_route - n_decorator else "router"
            route_deps.append({**tree, "param": None, "via": via})
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
            out.extend(_compare_route(key, row, got))
    out.extend(Difference("route_extra", key, _label(key)) for key in actual if key not in expected)
    return out


def compare(runtime: dict, di: Mapping) -> dict[str, list[str]]:
    diffs: dict[str, list[str]] = {name: [] for name in CLASSES}
    for difference in differences(runtime, di):
        diffs[difference.cls].append(difference.text)
    return diffs


def _compare_route(key: tuple[str, str, int], row: dict, got: dict) -> list[Difference]:
    """One route both sides have: its handler, location and dependencies."""
    label = _label(key)
    out: list[Difference] = []

    def differ(cls: str, text: str) -> None:
        out.append(Difference(cls, key, f"{label}: {text}"))

    if got.get("handler_id") != row["handler_id"]:
        differ("handler_mismatch", f"runtime {row['handler_id']}, analyzer {got.get('handler_id')}")
    want_where = (row.get("kind"), row.get("file"), row.get("line"))
    have_where = (got.get("kind"), got.get("file"), got.get("line"))
    if want_where != have_where:
        differ("location_mismatch", f"runtime {want_where}, analyzer {have_where}")
    deps = got.get("deps") or []
    want_route = [d["id"] for d in row["route_deps"]]
    have_route = [d["id"] for d in deps if d.get("via") != "direct"]
    if want_route != have_route:
        differ("route_deps_mismatch", f"runtime {want_route}, analyzer {have_route}")
    else:
        # The runtime cannot tell a stacked decorator's dependencies apart ("unknown").
        want_via = [d["via"] for d in row["route_deps"]]
        have_via = [d.get("via") for d in deps if d.get("via") != "direct"]
        if any(w != h for w, h in zip(want_via, have_via, strict=False) if w != "unknown"):
            differ("via_mismatch", f"runtime {want_via}, analyzer {have_via}")
    want_params = sorted((d["param"], d["id"]) for d in row["param_deps"])
    have_params = sorted((d.get("param"), d["id"]) for d in deps if d.get("via") == "direct")
    if want_params != have_params:
        differ("param_deps_mismatch", f"runtime {want_params}, analyzer {have_params}")
    have_closure = set(got.get("all_deps") or ())
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
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("RUNTIME", "DI"))
    args = parser.parse_args(argv)
    if args.compare is None and args.out is None:
        parser.error("--out is required unless --compare is given")
    if args.compare is None and args.cases is None and (args.import_root is None or args.app is None):
        parser.error("give --cases DIR, or --import-root DIR with --app module:attr")
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
    if args.cases is not None:
        root = args.cases.resolve()
        sys.path.insert(0, str(root))
        result = run_cases(root, repo_root)
    else:
        sys.path.insert(0, str(args.import_root.resolve()))
        result = {"app": args.app, **walk_app(load_app(args.app), repo_root)}
    import fastapi

    # The include model the analysis must use to compare with this run.
    result["fastapi_version"] = fastapi.__version__
    args.out.write_text(json.dumps(result, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
