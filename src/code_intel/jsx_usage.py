"""Find JSX element usage across frontend `.tsx`, `.jsx`, `.js`, `.mjs` and `.cjs` files.

Reports every render site of a component with file:line:column, the enclosing
named function and the tag kind, so occurrence-level questions ("where is <X/>
rendered, and by what?") get exact answers rather than file-level importers.

Elements come from the TypeScript compiler (the helper's ``facts`` mode, cached
per file by ``ts_facts.FactsCache``). ``.ts``/``.mts``/``.cts`` files are not
read: TypeScript parses ``<X>`` there as a type assertion, never JSX.

``parent_function`` follows the README rule: the innermost function declaration
or expression with a name, or arrow/function expression that initializes a
variable directly or as a direct call argument, whose span from its parameter
list on holds the element. Methods, class fields and object-literal properties
are not named functions, and fragments (``<>``) have no tag to query.

CLI (verification): ``code-intel dump jsx --all --json [--include-tests] [--root DIR]``
prints every element under the root (default: ``frontend.root``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from code_intel.cli import Context
from code_intel.code_paths import JSX_SUFFIXES, SourceFile, is_test_path, iter_source_files
from code_intel.problems import Problem, diagnostic, partition
from code_intel.provision import find_toolchain
from code_intel.repo_files import RepoFilesError, list_repo_files
from code_intel.result_types import SCHEMA_VERSION
from code_intel.ts_bridge import (
    HelperError,
    ToolchainProblem,
    frontend_not_found_problem,
    incomplete_toolchain_problem,
)
from code_intel.ts_facts import FactsCache, FileFacts, ToolchainUnavailable, facts_problems

__all__ = [
    "JSX_SUFFIXES",
    "JsxElementRow",
    "JsxRootError",
    "JsxUsage",
    "TagKind",
    "ToolchainUnavailable",
    "all_jsx_elements",
    "find_jsx_usage",
    "resolve_root",
]

TagKind = Literal["open", "self_close"]

STAGE = "jsx usage"


@dataclass
class JsxUsage:
    """One JSX usage site for a component."""

    file: str
    line: int
    column: int
    snippet: str
    parent_function: str | None
    tag_kind: TagKind
    is_test: bool = field(default=False)


@dataclass(frozen=True)
class JsxElementRow:
    """One element of any tag, for ``--all`` dumps and oracle comparison."""

    file: str
    tag: str
    line: int
    column: int
    kind: TagKind
    parent: str | None
    is_test: bool
    snippet: str


class JsxRootError(ValueError):
    """``root`` does not resolve to a directory inside the repo."""


def resolve_root(root: Path, repo_root: Path) -> Path:
    """``root`` resolved (relative roots are taken from ``repo_root``), checked to
    be a directory inside the repo."""
    base = repo_root.resolve()
    scan_root = (root if root.is_absolute() else base / root).resolve()
    if not scan_root.is_relative_to(base):
        raise JsxRootError(f"root '{root}' resolves to '{scan_root}', which is outside the repo root '{base}'")
    if not scan_root.is_dir():
        raise JsxRootError(f"root '{root}' resolves to '{scan_root}', which is not a directory")
    return scan_root


def _scan(
    root: Path,
    *,
    facts: FactsCache,
    repo_root: Path,
    frontend_root: str,
    include_tests: bool,
    problems: list[Problem],
    visible: AbstractSet[str] | None,
) -> list[tuple[SourceFile, FileFacts, bool]]:
    scan_root = resolve_root(root, repo_root)
    files = iter_source_files(
        scan_root,
        JSX_SUFFIXES,
        repo_root.resolve(),
        problems,
        stage=STAGE,
        generated_parent=repo_root.resolve() / frontend_root,
        visible=visible,
    )
    tagged = [(source, is_test_path(source.rel)) for source in files]
    # Test files are not read at all unless asked for, so excluding them also
    # costs nothing. They still count as walked, so their cached facts (the
    # symbol index reads them) are not evicted.
    wanted = [(source, is_test) for source, is_test in tagged if include_tests or not is_test]
    by_rel = facts.facts_for(
        [source for source, _ in wanted],
        root=scan_root,
        suffixes=JSX_SUFFIXES,
        walked=files,
    )
    out = []
    for source, is_test in wanted:
        file_facts = by_rel[source.rel]
        problems.extend(facts_problems(file_facts, stage=STAGE))
        out.append((source, file_facts, is_test))
    return out


def find_jsx_usage(
    component_name: str,
    root: Path,
    *,
    facts: FactsCache,
    repo_root: Path,
    frontend_root: str,
    include_self_close: bool = True,
    include_tests: bool = False,
    problems: list[Problem],
    visible: AbstractSet[str] | None = None,
) -> list[JsxUsage]:
    """Every JSX element whose tag equals ``component_name`` exactly, under ``root``.

    ``component_name`` is matched by exact equality with the element's tag as
    written, whitespace removed (``DataTable``, ``Ctx.Provider``, ``svg:rect``).
    ``frontend_root`` is the configured frontend root, repo-relative: the generated
    directories directly under it are not walked. ``visible`` (``RepoFiles.files``)
    limits the walk to git-visible files; None walks every file. Test files are skipped unless ``include_tests``; each usage carries
    ``is_test``. Unreadable files, files the helper could not analyze and files
    with TypeScript syntax errors are appended to ``problems`` as diagnostics.

    Raises ``JsxRootError`` for a root outside the repo or not a directory,
    ``ToolchainUnavailable`` when node or TypeScript is missing, and
    ``ts_bridge.HelperError`` when the helper fails.
    """
    if not component_name:
        # No tag is empty; the root is still checked so a bad root is never silent.
        resolve_root(root, repo_root)
        return []
    scanned = _scan(
        root,
        facts=facts,
        repo_root=repo_root,
        frontend_root=frontend_root,
        include_tests=include_tests,
        problems=problems,
        visible=visible,
    )
    usages: list[JsxUsage] = []
    for source, file_facts, is_test in scanned:
        for element in file_facts.elements:
            if element.tag != component_name:
                continue
            if element.kind == "self_close" and not include_self_close:
                continue
            usages.append(
                JsxUsage(
                    file=source.rel,
                    line=element.line,
                    column=element.column,
                    snippet=element.snippet,
                    parent_function=element.parent,
                    tag_kind=element.kind,
                    is_test=is_test,
                )
            )
    usages.sort(key=lambda u: (u.file, u.line, u.column))
    return usages


def all_jsx_elements(
    root: Path,
    *,
    facts: FactsCache,
    repo_root: Path,
    frontend_root: str,
    include_tests: bool = False,
    problems: list[Problem],
    visible: AbstractSet[str] | None = None,
) -> list[JsxElementRow]:
    """Every JSX element under ``root``, sorted by file, line and column; ``visible`` as in ``find_jsx_usage``."""
    rows = [
        JsxElementRow(
            file=source.rel,
            tag=element.tag,
            line=element.line,
            column=element.column,
            kind=element.kind,
            parent=element.parent,
            is_test=is_test,
            snippet=element.snippet,
        )
        for source, file_facts, is_test in _scan(
            root,
            facts=facts,
            repo_root=repo_root,
            frontend_root=frontend_root,
            include_tests=include_tests,
            problems=problems,
            visible=visible,
        )
        for element in file_facts.elements
    ]
    rows.sort(key=lambda r: (r.file, r.line, r.column))
    return rows


def add_arguments(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("component", nargs="?", help="exact tag name, e.g. DataTable or Ctx.Provider")
    target.add_argument("--all", action="store_true", help="every element under the root")
    parser.add_argument("--root", help="directory to scan, relative to the repo root (default: frontend.root)")
    parser.add_argument("--include-tests", action="store_true")
    parser.add_argument("--no-self-close", action="store_true", help="omit <X /> elements")
    parser.add_argument("--json", action="store_true")


def _unavailable(problem: ToolchainProblem, *, as_json: bool) -> int:
    """Report that the frontend could not be analyzed; exit code 2."""
    if as_json:
        found = diagnostic(
            "frontend_unavailable",
            stage=STAGE,
            step="run typescript helper (facts mode)",
            file=problem.missing,
            message=f"frontend could not be analyzed: {problem.message}",
            missing=problem.missing,
            fix=problem.fix,
            problem=problem.code,
        )
        print(_document([found], [], "elements", []))
    else:
        print(f"code-intel: frontend could not be analyzed: {problem.message}. Fix: {problem.fix}", file=sys.stderr)
    return 2


def _document(diagnostics: list[Problem], limitations: list[Problem], key: str, rows: list) -> str:
    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "complete": not diagnostics,
            "diagnostics": [p.as_dict() for p in diagnostics],
            "analysis_limitations": [p.as_dict() for p in limitations],
            key: [asdict(row) for row in rows],
        },
        ensure_ascii=False,
    )


def run(args: argparse.Namespace, context: Context) -> int:
    repo_root = context.repo_root
    frontend_root = context.config.frontend.root
    missing = frontend_not_found_problem(repo_root, frontend_root)
    if missing is not None:
        return _unavailable(missing, as_json=args.json)
    toolchain = find_toolchain(repo_root)
    if not toolchain.available:
        return _unavailable(incomplete_toolchain_problem(toolchain), as_json=args.json)
    facts = FactsCache(repo_root, lambda: toolchain)
    problems: list[Problem] = []
    root = Path(args.root or frontend_root)
    try:
        visible = list_repo_files(repo_root).files
    except RepoFilesError as exc:
        print(f"code-intel: {exc}", file=sys.stderr)
        return 2
    try:
        if args.all:
            rows: list[JsxElementRow] | list[JsxUsage] = all_jsx_elements(
                root,
                facts=facts,
                repo_root=repo_root,
                frontend_root=frontend_root,
                include_tests=args.include_tests,
                problems=problems,
                visible=visible,
            )
        else:
            rows = find_jsx_usage(
                args.component,
                root,
                facts=facts,
                repo_root=repo_root,
                frontend_root=frontend_root,
                include_self_close=not args.no_self_close,
                include_tests=args.include_tests,
                problems=problems,
                visible=visible,
            )
    except JsxRootError as exc:
        print(f"code-intel: {exc}", file=sys.stderr)
        return 2
    except ToolchainUnavailable as exc:
        return _unavailable(exc.problem, as_json=args.json)
    except HelperError as exc:
        return _unavailable(exc.problem(), as_json=args.json)

    diagnostics, limitations = partition(problems)
    if args.json:
        print(_document(diagnostics, limitations, "elements" if args.all else "usages", rows))
    else:
        for problem in problems:
            print(f"warning: {problem.message}", file=sys.stderr)
        for row in rows:
            if isinstance(row, JsxElementRow):
                print(f"{row.file}:{row.line}:{row.column} <{row.tag}> {row.kind} in {row.parent or '-'}")
            else:
                print(
                    f"{row.file}:{row.line}:{row.column} {row.tag_kind} in {row.parent_function or '-'}: {row.snippet}"
                )
    return 0
