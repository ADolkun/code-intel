"""``code-intel setup``: install the TypeScript the frontend analysis needs.

The helper's pinned ``package.json`` and ``package-lock.json`` are installed with
``npm ci --ignore-scripts`` into ``install_dir()`` in the user cache. Every
checkout, worktree and repository shares that install, and a second run finds it
and does not call npm. The install is built in a staging directory and renamed
into place, so a run that fails or is killed leaves nothing half-installed and
two runs at once cannot corrupt each other.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from code_intel import ts_bridge

# `code-intel setup` works without a repository or a config file.
NEEDS_CONFIG = False

NPM_TIMEOUT_S = 600.0
NPM_ARGS = ("ci", "--ignore-scripts", "--no-audit", "--no-fund")
PACKAGED_FILES = ("package.json", "package-lock.json")


class SetupError(RuntimeError):
    """Setup cannot finish; ``str()`` says why and what to do."""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    del parser


def _toolchain(
    env: Mapping[str, str], which: Callable[[str], str | None], run: Callable[..., Any]
) -> ts_bridge.Toolchain:
    """The toolchain as ``setup`` would leave it: the cache install, whatever
    ``CODE_INTEL_TYPESCRIPT_DIR`` says."""
    return ts_bridge.find_toolchain(Path.cwd(), env={**env, ts_bridge.TS_DIR_ENV: ""}, which=which, run=run)


def _npm_failure(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or b"").decode("utf-8", "replace") or (proc.stdout or b"").decode("utf-8", "replace")
    tail = "\n".join(text.strip().splitlines()[-10:]) or "(no output)"
    return f"npm ci exited with code {proc.returncode}:\n{tail}"


def _build(target: Path, npm: str, run: Callable[..., Any]) -> None:
    """``npm ci`` in a staging directory, then rename it to ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=target.parent))
    try:
        for name in PACKAGED_FILES:
            shutil.copyfile(ts_bridge.HELPER_DIR / name, stage / name)
        try:
            proc = run([npm, *NPM_ARGS], cwd=str(stage), capture_output=True, timeout=NPM_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SetupError(f"could not run npm ({type(exc).__name__}: {exc})") from exc
        if proc.returncode != 0:
            raise SetupError(_npm_failure(proc))
        try:
            os.rename(stage, target)
        except OSError:
            # Another run installed it first; theirs stays.
            if not target.exists():
                raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def install(
    env: Mapping[str, str] = os.environ,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[ts_bridge.Toolchain, bool]:
    """Make the cache install usable. Returns the toolchain and whether npm ran.

    Raises ``SetupError`` when node is missing or too old, npm is missing or
    fails, or the install does not pass the toolchain probe afterwards.
    """
    toolchain = _toolchain(env, which, run)
    if toolchain.available:
        return toolchain, False
    problem = toolchain.problem
    if problem is not None and problem.code.startswith("node_"):
        raise SetupError(f"{problem.message}. Fix: {problem.fix}")
    npm = which("npm")
    if npm is None:
        raise SetupError("npm not found on PATH (it ships with Node.js)")
    target = ts_bridge.install_dir(env)
    if target.exists():
        shutil.rmtree(target)
    _build(target, npm, run)
    toolchain = _toolchain(env, which, run)
    if not toolchain.available:
        detail = f"{toolchain.problem.message}. Fix: {toolchain.problem.fix}" if toolchain.problem else "unknown"
        raise SetupError(f"npm ci finished but the install is not usable: {detail}")
    return toolchain, True


def run(args: argparse.Namespace, context: object = None) -> int:
    del args, context
    try:
        toolchain, installed = install()
    except (SetupError, OSError) as exc:
        print(f"code-intel setup: {exc}", file=sys.stderr)
        return 1
    verb = "installed" if installed else "already installed"
    print(
        f"ok: typescript {toolchain.typescript_version} {verb} at {toolchain.typescript_dir} (node {toolchain.node_version})"
    )
    return 0
