"""FastAPI dependency map: every live route with its dependencies in FastAPI order.

Routes, routers and their ``dependencies=`` layers come from the shared router
graph (``router_graph.build_backend_model``); dependency targets, parameter
semantics and closures come from ``dependency_resolver``. Dependencies are
identified by ``module:qualname`` and displayed by their short name.

Usage:
    code-intel dump di           # human-readable report
    code-intel dump di --json    # DI JSON (schema v2)

Exit status: 0 complete, 1 the analysis could not run, 2 the result is
incomplete (diagnostics are printed to stderr).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from code_intel import include_model as include_models
from code_intel import problems
from code_intel.backend_model import BackendModel, DepTarget, DepUse, LiveRoute, NameResolver, Unknown
from code_intel.config import BackendConfig
from code_intel.dependency_resolver import STAGE, DependencyResolver
from code_intel.problems import min_python_version, runtime_is_compatible
from code_intel.result_types import (
    SCHEMA_VERSION,
    DiDependencyFunction,
    DiDepRow,
    DiResult,
    DiRoute,
    DiRouter,
)

if TYPE_CHECKING:
    from code_intel.cli import Context

# ── DI JSON v2 ───────────────────────────────────────────────────────────


def _dep_row(use: DepUse) -> DiDepRow:
    return {
        "id": use.target.id,
        "display": use.display,
        "via": use.via,
        "param": use.param,
        "alias": use.alias,
        "marker": use.marker,
        "file": use.file,
        "line": use.line,
    }


def _route_row(route: LiveRoute, uses: list[DepUse], closure: list[DepTarget]) -> DiRoute:
    def displays(via: str) -> list[str]:
        return [u.display for u in uses if u.via == via]

    return {
        "method": route.method,
        "path": route.path,
        "kind": route.kind,
        "handler": route.handler,
        "handler_id": route.handler_id,
        "file": route.file,
        "line": route.line,
        "mount": route.mount,
        "order": route.order,
        "chain": list(route.chain),
        "app_deps": displays("app"),
        "router_deps": displays("router"),
        "decorator_deps": displays("route decorator"),
        "direct_deps": displays("direct"),
        "deps": [_dep_row(u) for u in uses],
        "all_deps": [t.id for t in closure],
    }


def _unique_ids(uses: Iterable[DepUse]) -> list[str]:
    out: list[str] = []
    for use in uses:
        if use.target.id not in out:
            out.append(use.target.id)
    return out


def _mounted_routers(model: BackendModel) -> set[str]:
    """Router ids reachable from the app through include edges or live route chains."""
    children: dict[str, list[str]] = {}
    for edge in model.edges:
        children.setdefault(edge.parent, []).append(edge.child)
    mounted = {node for route in model.routes for node in route.chain}
    reached = {model.app_id}
    stack = [model.app_id]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in reached:
                reached.add(child)
                stack.append(child)
    return mounted | reached


_FORMS = {
    "function": "the function",
    "class": "the class",
    "instance": "an instance",
    "method": "the method",
    "lambda": "a lambda",
    "factory_result": "a factory's result",
    "unresolved": "an unresolved target",
}


def _form(target: DepTarget) -> str:
    form = _FORMS.get(target.kind, target.kind)
    return f"{form} through functools.partial at {target.variant}" if target.variant else form


def _registry(
    dep: DependencyResolver, targets: Iterable[DepTarget], uses: Iterable[DepUse]
) -> tuple[dict[str, DiDependencyFunction], list[problems.Problem]]:
    """The registry entries, and a ``shared_dependency_id`` limitation for each id
    whose forms have different sub-dependencies (the runtime gives a partial,
    a closure and its factory, the same id), as one entry lists only one form."""
    aliases: dict[str, set[str]] = {}
    forms: dict[str, list[DepTarget]] = {}
    pending = list(uses)
    for target in targets:
        seen = forms.setdefault(target.id, [])
        if target not in seen:
            seen.append(target)
    # A form used directly lists its own dependencies; a partial of it, only if nothing uses it directly.
    ordered = {i: next((t for t in found if not t.variant), found[0]) for i, found in forms.items()}
    for target in list(ordered.values()):
        pending.extend(dep.sub_deps(target))
    for use in pending:
        if use.alias:
            aliases.setdefault(use.target.id, set()).add(use.alias)
    out: dict[str, DiDependencyFunction] = {}
    shared: list[problems.Problem] = []
    for target_id in sorted(ordered, key=lambda i: (ordered[i].name, ordered[i].module, i)):
        target = ordered[target_id]
        subs = dep.sub_deps(target)
        listed = _unique_ids(subs)
        for other in forms[target_id]:
            other_subs = _unique_ids(dep.sub_deps(other))
            if other is target or other_subs == listed:
                continue
            where = f"{target.file}:{target.line}" if target.file and target.line else (target.file or target.module)
            shared.append(
                problems.limitation(
                    "shared_dependency_id",
                    stage=STAGE,
                    file=target.file,
                    line=target.line,
                    message=(
                        f"{where}: dependency {target_id}: {_form(other)} has sub-dependencies "
                        f"[{', '.join(other_subs)}] and {_form(target)} [{', '.join(listed)}], which FastAPI "
                        f"identifies by the same id; its entry lists {_form(target)}'s; each route's closure uses the form it declares"
                    ),
                )
            )
        transitive = [t.id for t in dep.closure(subs) if t.id != target_id]
        out[target_id] = {
            "id": target_id,
            "name": target.name,
            "module": target.module,
            "qualname": target.qualname,
            "kind": target.kind,
            "file": target.file,
            "line": target.line,
            "sub_deps": listed,
            "transitive_deps": transitive,
            "aliases": sorted(aliases.get(target_id, ())),
        }
    return out, shared


def _dedupe(found: Iterable[problems.Problem]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for problem in found:
        row = problem.as_dict()
        key = tuple(sorted((k, str(v)) for k, v in row.items()))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def build_di(model: BackendModel, resolver: DependencyResolver | NameResolver | None = None) -> DiResult:
    """DI JSON v2 for a backend model.

    ``resolver`` is the ``DependencyResolver`` to use, or the name resolver to
    build one over (default: the model's own).
    """
    dep = (
        resolver
        if isinstance(resolver, DependencyResolver)
        else DependencyResolver(resolver if resolver is not None else model.resolver, model.sources)
    )
    targets: list[DepTarget] = []
    all_uses: list[DepUse] = []
    reverse: dict[str, set[str]] = {}

    def rows(routes: Sequence[LiveRoute], *, live: bool) -> list[DiRoute]:
        out = []
        for route in sorted(routes, key=lambda r: r.order):
            uses = dep.route_deps(route)
            closure = dep.closure(uses)
            all_uses.extend(uses)
            targets.extend(dep.forms(uses))
            if live:
                for target in closure:
                    reverse.setdefault(target.id, set()).add(f"{route.method} {route.path}")
            out.append(_route_row(route, uses, closure))
        return out

    route_rows = rows(model.routes, live=True)
    unmounted_rows = rows(model.unmounted_routes, live=False)

    mounted = _mounted_routers(model)
    routers: list[DiRouter] = []
    for node in model.routers:
        own: list[DepUse] = []
        if not isinstance(node.dependencies, Unknown):
            own = dep.layer_uses(node.dependencies, "app" if node.kind == "FastAPI" else "router")
        all_uses.extend(own)
        targets.extend(dep.forms(own))
        prefix_resolved = not isinstance(node.prefix, Unknown)
        routers.append(
            {
                "id": node.id,
                "file": node.file,
                "line": node.line,
                "kind": node.kind,
                "prefix": node.prefix if isinstance(node.prefix, str) else None,
                "prefix_resolved": prefix_resolved,
                "deps": [u.display for u in own],
                "mounted": node.id in mounted,
            }
        )

    registry, shared = _registry(dep, targets, all_uses)
    diagnostics = _dedupe(model.diagnostics)
    limitations = _dedupe([*model.limitations, *dep.problems, *shared])
    return {
        "schema_version": SCHEMA_VERSION,
        "complete": not diagnostics,
        "diagnostics": diagnostics,  # type: ignore[typeddict-item]
        "analysis_limitations": limitations,  # type: ignore[typeddict-item]
        "app": include_models.app_block(model),
        "dependency_functions": registry,
        "routes": route_rows,
        "unmounted_routes": unmounted_rows,
        "routers": routers,
        "reverse_index": {k: sorted(v) for k, v in sorted(reverse.items())},
        "summary": {
            "routes": len(route_rows),
            "http": sum(1 for r in route_rows if r["kind"] == "http"),
            "websocket": sum(1 for r in route_rows if r["kind"] == "websocket"),
            "dependency_functions": len(registry),
            "files_scanned": model.files_scanned,
        },
    }


# ── Text report ──────────────────────────────────────────────────────────


def dep_label(row: DiDepRow) -> str:
    """``display`` plus ``[as alias]`` when the dependency is reached through an alias."""
    return f"{row['display']} [as {row['alias']}]" if row["alias"] else row["display"]


def format_text(result: DiResult) -> str:
    lines: list[str] = []
    if result["diagnostics"]:
        lines.append(problems.incomplete_banner(result["diagnostics"]))
    if result["analysis_limitations"]:
        lines.append(problems.limitations_banner(result["analysis_limitations"]))
    funcs = result["dependency_functions"]
    names: dict[str, int] = {}
    for entry in funcs.values():
        names[entry["name"]] = names.get(entry["name"], 0) + 1

    def label(target_id: str) -> str:
        entry = funcs.get(target_id)
        if entry is None:
            return target_id
        return target_id if names[entry["name"]] > 1 else entry["name"]

    lines.append(f"\nDependency targets ({len(funcs)})")
    lines.append("─" * 72)
    for target_id, entry in funcs.items():
        where = f"{entry['file']}:{entry['line']}" if entry["file"] else "?"
        subs = ", ".join(label(s) for s in entry["sub_deps"]) or "(leaf)"
        aliases = f"  [aliases: {', '.join(entry['aliases'])}]" if entry["aliases"] else ""
        lines.append(f"  {label(target_id):<40} {entry['kind']:<14} {where}  → {subs}{aliases}")

    lines.append(f"\nRoutes ({len(result['routes'])})")
    lines.append("─" * 72)
    for route in result["routes"]:
        # pyrefly: ignore[bad-argument-type]
        lines.extend(_route_lines(route, funcs, names, label))
    if result["unmounted_routes"]:
        lines.append(f"\nUnmounted routes ({len(result['unmounted_routes'])}), not live")
        lines.append("─" * 72)
        for route in result["unmounted_routes"]:
            lines.append(f"  {route['method']:<6} {route['path']}  [{route['file']}:{route['line']}]")
    lines.extend(_summary_lines(result))
    return "\n".join(lines)


def _route_lines(route: dict, funcs: dict, names: dict[str, int], label: Callable[[str], str]) -> list[str]:
    """One route: its handler, then its dependencies by where they are declared."""
    lines = [
        f"\n  {route['method']:<6} {route['path']}",
        f"    Handler: {route['handler']}  [{route['file']}:{route['line']}]",
    ]
    for via, title in (("app", "App"), ("router", "Router"), ("route decorator", "Route"), ("direct", "Direct")):
        labels = [
            f"{dep_label(d)} ({d['id']})" if d["id"] in funcs and names[funcs[d["id"]]["name"]] > 1 else dep_label(d)
            for d in route["deps"]
            if d["via"] == via
        ]
        if labels:
            lines.append(f"    {title + ':':<9}{', '.join(labels)}")
    first = {d["id"] for d in route["deps"]}
    inherited = [label(i) for i in route["all_deps"] if i not in first]
    if inherited:
        lines.append(f"    Inherited: {', '.join(inherited)}")
    return lines


def _summary_lines(result: DiResult) -> list[str]:
    summary = result["summary"]
    lines = [
        "\nSummary",
        f"  {summary['routes']} routes ({summary['http']} HTTP, {summary['websocket']} WebSocket), "
        f"{summary['dependency_functions']} dependency targets, {summary['files_scanned']} files scanned",
    ]
    if result["complete"]:
        lines.append("  Status: COMPLETE, 0 diagnostics")
    else:
        lines.append(f"  Status: INCOMPLETE, {len(result['diagnostics'])} diagnostic(s)")
    return lines


# ── Entry point ──────────────────────────────────────────────────────────


def _refuse_incompatible_runtime(minimum: tuple[int, int]) -> int:
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    required = "{}.{}".format(*minimum)
    print(
        f"Error: Python {running} is older than the repo minimum {required}. "
        f"Backend source uses syntax this interpreter cannot parse, so results would be "
        f"silently incomplete. Re-run with Python >={required} (e.g. `uv run --python {required}`).",
        file=sys.stderr,
    )
    return 1


def compute(repo_root: Path, backend: BackendConfig) -> DiResult:
    """Build the backend model for ``repo_root`` and return its DI JSON."""
    # Imported here so consumers that bring their own model do not load the graph builder.
    from code_intel import router_graph

    model = router_graph.build_backend_model(
        repo_root / backend.root, backend.package, backend.app, repo_root, lock=backend.lock
    )
    return build_di(model)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print DI JSON instead of the text report")


def run(args: argparse.Namespace, context: Context) -> int:
    repo_root, backend = context.repo_root, context.config.backend
    version_file = repo_root / backend.python_version_file if backend.python_version_file else None
    minimum = min_python_version(version_file)
    if not runtime_is_compatible(minimum):
        return _refuse_incompatible_runtime(minimum)
    package_dir = repo_root / backend.root / backend.package
    if not package_dir.is_dir():
        print(f"Error: {package_dir}: backend package directory not found", file=sys.stderr)
        return 1

    result = compute(repo_root, backend)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_text(result))
    if result["diagnostics"]:
        print(problems.incomplete_banner(result["diagnostics"]), file=sys.stderr)
        for diagnostic in result["diagnostics"]:
            print(f"  [{diagnostic['kind']}] {diagnostic['message']}", file=sys.stderr)
        return 2
    return 0
