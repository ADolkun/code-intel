"""Install TypeScript on first use, so no consumer needs a manual ``code-intel setup``.

``find_toolchain`` is ``ts_bridge.find_toolchain`` that, when only the cache
install is missing, runs the install ``code-intel setup`` makes. A command
installs and waits (``background=False``); the MCP server installs in a thread
and answers with a ``frontend_provisioning`` problem until it finishes.

Each process makes one attempt per install directory (which the packaged
lockfile's digest names), so a failure is not retried in a loop; the failure
becomes the toolchain problem, with ``code-intel setup`` as the way to retry.
An explicit ``CODE_INTEL_TYPESCRIPT_DIR`` or ``CODE_INTEL_NODE``, or
``CODE_INTEL_NO_AUTO_SETUP=1``, leaves the toolchain as the probe found it.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from code_intel import setup, ts_bridge
from code_intel.ts_bridge import Toolchain, ToolchainProblem

NO_AUTO_ENV = "CODE_INTEL_NO_AUTO_SETUP"

# A missing or damaged cache install; every other problem is not the install's.
_INSTALLABLE = ("typescript_not_found", "typescript_unreadable")


@dataclasses.dataclass
class _Attempt:
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    failure: str | None = None


_lock = threading.Lock()
_attempts: dict[Path, _Attempt] = {}


def _install_problem(toolchain: Toolchain, env: Mapping[str, str]) -> ToolchainProblem | None:
    """The problem an automatic install would fix, or None when there is none or it is not ours to fix."""
    if env.get(NO_AUTO_ENV, "") not in ("", "0") or env.get(ts_bridge.TS_DIR_ENV) or env.get(ts_bridge.NODE_ENV):
        return None
    problem = toolchain.problem
    return problem if problem is not None and problem.code in _INSTALLABLE else None


def _install(
    attempt: _Attempt, env: Mapping[str, str], which: Callable[[str], str | None], run: Callable[..., Any]
) -> None:
    try:
        setup.install(env, which=which, run=run)
    except setup.SetupError as exc:
        attempt.failure = exc.summary
    except OSError as exc:
        attempt.failure = f"{type(exc).__name__}: {exc}"
    finally:
        attempt.done.set()


def _failed(toolchain: Toolchain, problem: ToolchainProblem, cause: str) -> Toolchain:
    fix = (
        f'automatic setup failed: {cause}; fix that, then run "code-intel setup" to retry '
        f"(or set {ts_bridge.TS_DIR_ENV} to a typescript package directory)"
    )
    return dataclasses.replace(toolchain, problem=dataclasses.replace(problem, fix=fix))


def find_toolchain(
    repo_root: Path,
    *,
    background: bool = False,
    env: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> Toolchain:
    def probe() -> Toolchain:
        return ts_bridge.find_toolchain(repo_root, env=env, which=which, run=run)

    toolchain = probe()
    problem = _install_problem(toolchain, env)
    if problem is None:
        return toolchain
    target = ts_bridge.install_dir(env)
    with _lock:
        attempt = _attempts.get(target)
        owner = attempt is None
        if attempt is None:
            attempt = _attempts[target] = _Attempt()
    if owner and background:
        threading.Thread(target=_install, args=(attempt, env, which, run), name="code-intel-setup", daemon=True).start()
    elif owner:
        print(f"code-intel: installing TypeScript into {target} (first use, one time)", file=sys.stderr)
        _install(attempt, env, which, run)
    elif not background:
        attempt.done.wait()
    if not attempt.done.is_set():
        provisioning = ToolchainProblem(
            "frontend_provisioning", ts_bridge.PROVISIONING_MESSAGE, None, ts_bridge.PROVISIONING_FIX
        )
        return dataclasses.replace(toolchain, problem=provisioning)
    if attempt.failure is not None:
        return _failed(toolchain, problem, attempt.failure)
    return probe()
