"""``code-intel runtime-check``: the static route analysis against the running app.

Imports the configured app with a Python that has the app's dependencies
installed (``--python``, usually the repository's virtual environment), through
the packaged FastAPI oracle, and compares what FastAPI built with the
dependency map: routes, handlers and their ``def`` locations, dispatch order,
route-level and parameter dependencies, dependency closures and
``redirect_slashes``.

The app is imported from an empty temporary working directory, so a local
``.env`` or stray file cannot change the routes; give every setting the routes
depend on with ``--set`` and ``--env-file``.

Some differences are known blind spots of a static analysis. They are listed
as explained, each with its reason, and do not fail the check:

- a route the runtime serves from an installed library, when the analysis
  reported a limitation that can hide routes (an ``unresolved_include``, an
  ``opaque_router_call``, ...): the analysis could not see inside that call;
- a dependency difference located at a dependency the analysis cannot see
  into (``external``: a module-level name bound to a call result such as
  ``fastapi_users.current_user(...)``; ``unresolved``): the runtime
  dependency standing in its place, and everything beneath it, is explained.
  Every other dependency of the route must still agree.

Exit codes: 0 no unexplained difference, 1 an unexplained difference, 2 the
check could not run (no usable ``--python``, the app did not import, the
analysis is incomplete).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from code_intel import problems
from code_intel.oracles import fastapi_oracle

if TYPE_CHECKING:
    from code_intel.cli import Context

ORACLE = Path(fastapi_oracle.__file__).resolve()
TIMEOUT_S = 300
SHOWN = 20
SHOWN_EXPLAINED = 5
CLIP = 200
OPAQUE_KINDS = frozenset({"external", "unresolved"})
INSTALLED_DIRS = frozenset({".venv", "venv", "site-packages", "dist-packages"})
DEPENDENCY_CLASSES = ("route_deps_mismatch", "param_deps_mismatch", "closure_mismatch")

Node = Mapping[str, Any]


@dataclass(frozen=True)
class Classified:
    difference: fastapi_oracle.Difference
    reason: str | None  # why it is explained; None when it is not


class RuntimeCheckError(Exception):
    """The check could not run; the message says why."""


# ── Classifying differences ──────────────────────────────────────────────


def is_installed(file: str | None) -> bool:
    """A handler defined in installed code: outside the repository, or under a virtual environment."""
    if not file:
        return True
    path = PurePosixPath(file)
    return path.is_absolute() or any(part in INSTALLED_DIRS for part in path.parts)


def opaque_ids(di: Mapping[str, Any]) -> frozenset[str]:
    """The dependency ids the analysis cannot see into."""
    return frozenset(dep_id for dep_id, entry in di["dependency_functions"].items() if entry["kind"] in OPAQUE_KINDS)


def subtree(node: Node) -> set[str]:
    """The node's id and the ids of everything beneath it."""
    ids = {node["id"]}
    for child in node.get("sub", ()):
        ids |= subtree(child)
    return ids


@dataclass(frozen=True)
class Pairing:
    """How dependencies line up with the runtime's.

    ``covered`` holds the runtime ids standing in the place of a dependency the
    analysis cannot see into. ``agrees`` is False when some other dependency
    differs.
    """

    covered: frozenset[str]
    agrees: bool


def pair_tree(node: Node, have_id: str, functions: Mapping[str, Any], opaque: frozenset[str]) -> Pairing:
    """A runtime dependency and everything beneath it against the analyzer's
    dependency ``have_id`` and its ``sub_deps``."""
    if have_id in opaque:
        return Pairing(frozenset(subtree(node)), True)
    if node["id"] != have_id:
        return Pairing(frozenset(), False)
    children = list(node.get("sub", ()))
    pending: list[str] = []
    covered: set[str] = set()
    agrees = True
    for sub_id in functions.get(have_id, {}).get("sub_deps", ()):
        match = next((c for c in children if c["id"] == sub_id), None)
        if match is None:
            pending.append(sub_id)
            continue
        children.remove(match)
        below = pair_tree(match, sub_id, functions, opaque)
        covered |= below.covered
        agrees = agrees and below.agrees
    for sub_id in pending:
        if sub_id in opaque and children:
            covered |= subtree(children.pop(0))
        else:
            agrees = False
    return Pairing(frozenset(covered), agrees and not children)


def _merge(pairings: Iterable[Pairing]) -> Pairing:
    covered: set[str] = set()
    agrees = True
    for pairing in pairings:
        covered |= pairing.covered
        agrees = agrees and pairing.agrees
    return Pairing(frozenset(covered), agrees)


def pair_route_deps(
    runtime_nodes: Sequence[Node], analyzer_ids: Sequence[str], functions: Mapping[str, Any], opaque: frozenset[str]
) -> Pairing:
    """Route-level dependencies pair by position."""
    if len(runtime_nodes) != len(analyzer_ids):
        return Pairing(frozenset(), False)
    return _merge(
        pair_tree(node, have, functions, opaque) for node, have in zip(runtime_nodes, analyzer_ids, strict=True)
    )


def pair_param_deps(
    runtime_nodes: Sequence[Node],
    analyzer: Sequence[tuple[str | None, str]],
    functions: Mapping[str, Any],
    opaque: frozenset[str],
) -> Pairing:
    """Parameter dependencies pair by parameter name."""
    left = list(runtime_nodes)
    pairings: list[Pairing] = []
    for param, have in analyzer:
        match = next((n for n in left if n["param"] == param), None)
        if match is None:
            return Pairing(frozenset(), False)
        left.remove(match)
        pairings.append(pair_tree(match, have, functions, opaque))
    merged = _merge(pairings)
    return Pairing(merged.covered, merged.agrees and not left)


def dependency_reason(cls: str, runtime_row: Node, analyzer_row: Node, di: Mapping[str, Any]) -> str | None:
    """Why a dependency difference of one route is explained, or None."""
    functions, opaque = di["dependency_functions"], opaque_ids(di)
    deps = analyzer_row.get("deps") or []
    route_pair = pair_route_deps(
        runtime_row["route_deps"], [d["id"] for d in deps if d.get("via") != "direct"], functions, opaque
    )
    param_pair = pair_param_deps(
        runtime_row["param_deps"],
        [(d.get("param"), d["id"]) for d in deps if d.get("via") == "direct"],
        functions,
        opaque,
    )
    covered = route_pair.covered | param_pair.covered
    if cls == "route_deps_mismatch":
        ok = route_pair.agrees and bool(route_pair.covered)
    elif cls == "param_deps_mismatch":
        ok = param_pair.agrees and bool(param_pair.covered)
    else:
        runtime_closure = set(runtime_row["closure"])
        analyzer_closure = set(analyzer_row.get("all_deps") or ())
        ok = (
            route_pair.agrees
            and param_pair.agrees
            and bool(covered)
            and runtime_closure - analyzer_closure <= covered
            and analyzer_closure - runtime_closure <= opaque
        )
    return "a dependency the analysis cannot see into (external or unresolved) stands in its place" if ok else None


def route_gaps(di: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [lim for lim in di["analysis_limitations"] if lim["kind"] in problems.BACKEND_ROUTE_GAP_KINDS]


def missing_route_reason(row: Node, di: Mapping[str, Any]) -> str | None:
    """Why a route the analysis lacks is explained, or None."""
    if not is_installed(row.get("file")):
        return None
    gaps = route_gaps(di)
    if not gaps:
        return None
    where = ", ".join(sorted({f"{g['kind']} at {g['file']}:{g['line']}" for g in gaps}))
    return f"the handler is defined in installed code and the analysis reported {where}"


def classify(runtime: Mapping[str, Any], di: Mapping[str, Any]) -> list[Classified]:
    """Every difference between the runtime dump and the DI JSON, each with its explanation if it has one."""
    runtime_rows = fastapi_oracle.keyed(runtime["routes"])
    analyzer_rows = fastapi_oracle.keyed(sorted(di["routes"], key=lambda r: r.get("order", 0)))
    out: list[Classified] = []
    for difference in fastapi_oracle.differences(dict(runtime), di):
        key = difference.key
        reason: str | None = None
        if key is not None and difference.cls == "route_missing":
            reason = missing_route_reason(runtime_rows[key], di)
        elif key is not None and difference.cls in DEPENDENCY_CLASSES:
            reason = dependency_reason(difference.cls, runtime_rows[key], analyzer_rows[key], di)
        out.append(Classified(difference, reason))
    return out


def unexplained(classified: Iterable[Classified]) -> list[Classified]:
    return [c for c in classified if c.reason is None]


# ── Report ───────────────────────────────────────────────────────────────


def _clip(text: str) -> str:
    return text if len(text) <= CLIP else text[: CLIP - 1] + "…"


def render(classified: Sequence[Classified]) -> str:
    by_class: dict[str, list[Classified]] = {name: [] for name in fastapi_oracle.CLASSES}
    for item in classified:
        by_class[item.difference.cls].append(item)
    lines: list[str] = []
    for name, items in by_class.items():
        explained = [i for i in items if i.reason is not None]
        lines.append(f"{name}: {len(items)}" + (f" ({len(explained)} explained)" if explained else ""))
        left = [i for i in items if i.reason is None]
        lines.extend(f"  ! {i.difference.text}" for i in left[:SHOWN])
        if len(left) > SHOWN:
            lines.append(f"  ! ... {len(left) - SHOWN} more")
        reasons: dict[str, list[str]] = defaultdict(list)
        for i in explained:
            if i.reason is not None:
                reasons[i.reason].append(i.difference.text)
        for reason, texts in reasons.items():
            lines.append(f"  explained ({len(texts)}): {reason}")
            lines.extend(f"    ~ {_clip(text)}" for text in texts[:SHOWN_EXPLAINED])
            if len(texts) > SHOWN_EXPLAINED:
                lines.append(f"    ~ ... {len(texts) - SHOWN_EXPLAINED} more")
    total = len(classified)
    open_count = len(unexplained(classified))
    lines.append(f"runtime-check: {total} differences, {total - open_count} explained, {open_count} unexplained")
    return "\n".join(lines)


def version_note(runtime: Mapping[str, Any], di: Mapping[str, Any]) -> str | None:
    ran, locked = runtime.get("fastapi_version"), (di.get("app") or {}).get("fastapi_version")
    if ran and locked and ran != locked:
        return (
            f"note: the app ran on FastAPI {ran}, but the analysis follows the locked {locked} "
            f"(include model {di['app'].get('include_model')}); install the locked version for an exact comparison"
        )
    return None


# ── Running the oracle ───────────────────────────────────────────────────


def resolve_python(value: Path, repo_root: Path) -> Path:
    """``value`` as an absolute path, taken from the current directory, else the
    repository root. The path is not resolved: a virtual environment's
    interpreter is found through the link's own location."""
    path = value.expanduser()
    for candidate in [path] if path.is_absolute() else [Path.cwd() / path, repo_root / path]:
        if candidate.is_file():
            return candidate
    raise RuntimeCheckError(
        f"--python {value}: no such file" + ("" if path.is_absolute() else f", from {Path.cwd()} or {repo_root}")
    )


def run_oracle(
    python: Path, repo_root: Path, backend_root: str, app: str, settings: Sequence[str], env_file: Path | None
) -> dict:
    with tempfile.TemporaryDirectory(prefix="code-intel-runtime-") as tmp:
        out = Path(tmp) / "runtime.json"
        command = [
            str(python),
            str(ORACLE),
            "--import-root",
            str(repo_root / backend_root),
            "--repo-root",
            str(repo_root),
            "--app",
            app,
            "--out",
            str(out),
        ]
        for item in settings:
            command += ["--set", item]
        if env_file is not None:
            command += ["--env-file", str(env_file)]
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_S, cwd=tmp, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeCheckError(f"could not run {python} ({type(exc).__name__}: {exc})") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-3000:]
            raise RuntimeCheckError(f"the app did not import under {python} (exit code {proc.returncode}):\n{tail}")
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeCheckError(f"the oracle wrote no readable result ({type(exc).__name__}: {exc})") from exc


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--python", type=Path, required=True, help="a Python with the app's dependencies installed")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="set an environment variable")
    parser.add_argument("--env-file", type=Path, help="KEY=VALUE lines to load (an existing variable wins)")


def run(args: argparse.Namespace, context: Context) -> int:
    from code_intel import di_map
    from code_intel.repo_files import RepoFilesError

    repo_root, backend = context.repo_root, context.config.backend
    if (refusal := problems.runtime_refusal(repo_root, backend.python_version_file)) is not None:
        print(refusal, file=sys.stderr)
        return 2
    try:
        python = resolve_python(args.python, repo_root)
        env_file = args.env_file.expanduser().resolve() if args.env_file is not None else None
        di = di_map.compute(repo_root, backend)
        runtime = run_oracle(python, repo_root, backend.root, backend.app, args.set, env_file)
    except (RuntimeCheckError, RepoFilesError) as exc:
        print(f"runtime-check: {exc}", file=sys.stderr)
        return 2
    classified = classify(runtime, di)
    print(
        f"runtime-check: {backend.app} imported with {python}; {len(runtime['routes'])} runtime routes, "
        f"{len(di['routes'])} analyzed"
    )
    if (note := version_note(runtime, di)) is not None:
        print(note)
    print(render(classified))
    if unexplained(classified):
        return 1
    if di["diagnostics"]:
        print(problems.incomplete_banner(di["diagnostics"]), file=sys.stderr)
        return 2
    return 0
