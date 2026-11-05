"""Running the packaged FastAPI oracle under the consumer's Python.

This is the only place code of the analyzed repository runs, for exactly three
commands: ``capture``, ``verify`` (its ``library-capture`` check) and
``runtime-check``. The oracle runs in a subprocess from an empty temporary
directory, with a timeout, and with a clean environment: ``PATH``, ``HOME``,
``TMPDIR``, ``LANG`` and ``LC_ALL`` when set, plus the configured ``env``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from code_intel.config import CaptureConfig
from code_intel.oracles import fastapi_oracle

ORACLE = Path(fastapi_oracle.__file__).resolve()
TIMEOUT_S = 300
INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")


class OracleRunError(Exception):
    """The oracle could not run or failed; the message says why."""


def clean_environment(env: Mapping[str, str]) -> dict[str, str]:
    inherited = {name: os.environ[name] for name in INHERITED_ENV if name in os.environ}
    return {**inherited, **env}


def resolve_python(value: Path, repo_root: Path, *, label: str, from_cwd: bool) -> Path:
    """``value`` as an absolute path, taken from the repository root (and first
    from the current directory when ``from_cwd``). The path is not resolved: a
    virtual environment's interpreter is found through the link's own location."""
    path = value.expanduser()
    bases = [] if path.is_absolute() else [*([Path.cwd()] if from_cwd else []), repo_root]
    for candidate in [path] if path.is_absolute() else [base / path for base in bases]:
        if candidate.is_file():
            return candidate
    where = f", from {' or '.join(str(base) for base in bases)}" if bases else ""
    raise OracleRunError(f"{label} {value}: no such file{where}")


def interpreter(option: Path | None, capture: CaptureConfig | None, repo_root: Path) -> Path:
    """``--python``, else the configured interpreter."""
    if option is not None:
        return resolve_python(option, repo_root, label="--python", from_cwd=True)
    if capture is None:
        raise OracleRunError("give --python, or set python in [backend.capture]")
    return resolve_python(Path(capture.python), repo_root, label="[backend.capture] python", from_cwd=False)


def _run(command: Sequence[str], cwd: str, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Runs ``command`` in a session of its own; on timeout the whole process group is killed, so a
    child that inherited the output pipes cannot keep this call waiting."""
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=dict(env),
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_oracle(
    python: Path, arguments: Sequence[str], env: Mapping[str, str], *, request: object | None = None
) -> dict:
    """The oracle's ``--out`` JSON for ``arguments``. ``request`` is written as JSON and passed as ``--capture``."""
    with tempfile.TemporaryDirectory(prefix="code-intel-oracle-") as tmp:
        out = Path(tmp) / "result.json"
        command = [str(python), str(ORACLE), *arguments, "--out", str(out)]
        if request is not None:
            request_file = Path(tmp) / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            command += ["--capture", str(request_file)]
        try:
            proc = _run(command, tmp, clean_environment(env))
        except (OSError, subprocess.SubprocessError) as exc:
            raise OracleRunError(f"could not run {python} ({type(exc).__name__}: {exc})") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-3000:]
            raise OracleRunError(f"the app did not import under {python} (exit code {proc.returncode}):\n{tail}")
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OracleRunError(f"the oracle wrote no readable result ({type(exc).__name__}: {exc})") from exc
