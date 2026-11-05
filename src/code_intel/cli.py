"""The ``code-intel`` command line.

``config-check`` lives here. Every other subcommand is a module in the table
below that provides two functions:

- ``add_arguments(parser)``: its options. The common ``--repo-root`` and
  ``--config`` are already on the parser.
- ``run(args, context) -> int``: the exit code. ``context`` is a ``Context``.
  A module with ``NEEDS_CONFIG = False`` (``setup``) is run without a
  repository or config file, and ``context`` is ``None``.

The table lists the planned interface; ``--help`` shows only the commands whose
module exists. Modules are imported to register their options, so a heavy
import belongs inside ``run``.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from code_intel.code_paths import GIT_CREATIONFLAGS, GIT_ERRORS
from code_intel.config import Config, ConfigError, load_config

# Subcommand -> (module in this package, one-line help).
COMMANDS: dict[str, tuple[str, str]] = {
    "mcp": ("server", "serve the tools over stdio"),
    "doc-check": ("doc_check", "check the docs against the code"),
    "setup": ("setup", "install the TypeScript compiler the frontend analysis needs"),
    "dump di": ("di_map", "dependency-injection map of the backend routes"),
    "dump routes": ("route_map", "backend routes matched with frontend calls"),
    "dump jsx": ("jsx_usage", "JSX usage of a component"),
    "dump symbols": ("symbol_resolve", "where a symbol is defined"),
    "verify": ("verify", "check the analyzers against each other on this repository"),
    "summary": ("summary", "counts and coverage of this repository's analysis"),
    "runtime-check": ("runtime_check", "compare the static route analysis with the running app"),
    "capture": ("capture", "record what installed code contributes to the app, for the analysis to apply"),
}


class UsageError(Exception):
    """A problem with the invocation rather than with the config; exit code 2."""


@dataclass(frozen=True)
class Context:
    repo_root: Path
    config: Config


def resolve_repo_root(repo_root: Path | None) -> Path:
    """``--repo-root``, or the work tree containing the current directory."""
    if repo_root is not None:
        return repo_root.expanduser().resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=GIT_CREATIONFLAGS,
        )
    except GIT_ERRORS as exc:
        raise UsageError(f"cannot find the repository root ({type(exc).__name__}: {exc}); pass --repo-root") from exc
    if proc.returncode != 0:
        raise UsageError("not inside a git work tree; pass --repo-root")
    return Path(proc.stdout.strip()).resolve()


def _available() -> dict[str, tuple[str, str]]:
    return {
        name: entry
        for name, entry in COMMANDS.items()
        if importlib.util.find_spec(f"code_intel.{entry[0]}") is not None
    }


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo-root", type=Path, help="repository to analyze (default: the current git work tree)")
    common.add_argument("--config", type=Path, help="config file (default: <repo-root>/.code-intel/config.toml)")
    parser = argparse.ArgumentParser(prog="code-intel", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("config-check", parents=[common], help="validate the config file")
    groups: dict[str, argparse._SubParsersAction] = {}
    for name, (module, help_text) in _available().items():
        group, _, leaf = name.rpartition(" ")
        if group and group not in groups:
            groups[group] = commands.add_parser(group, help=f"{group} commands").add_subparsers(
                dest="subcommand", required=True, metavar="subcommand"
            )
        sub = (groups[group] if group else commands).add_parser(leaf, parents=[common], help=help_text)
        importlib.import_module(f"code_intel.{module}").add_arguments(sub)
        sub.set_defaults(handler=module)
    return parser


def _config_check(context: Context) -> int:
    print(f"ok: {context.config.path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = None if args.command == "config-check" else importlib.import_module(f"code_intel.{args.handler}")
    if handler is not None and not getattr(handler, "NEEDS_CONFIG", True):
        return handler.run(args, None)
    try:
        repo_root = resolve_repo_root(args.repo_root)
        config = load_config(repo_root, args.config)
    except UsageError as exc:
        print(f"code-intel: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"code-intel: {exc}", file=sys.stderr)
        return 1 if args.command == "config-check" else 2
    context = Context(repo_root, config)
    if handler is None:
        return _config_check(context)
    return handler.run(args, context)


if __name__ == "__main__":
    sys.exit(main())
