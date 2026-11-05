"""Fixtures that run the FastAPI runtime oracle over every oracle case.

The oracle imports each case package with the FastAPI installed in this
environment. Which ``include_router`` model that FastAPI has decides which
inexact-case table applies; the two models are installed one at a time
(dependency groups ``fastapi-live`` and ``fastapi-copy``).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import unavailable

from code_intel import include_model, oracles
from code_intel.backend_model import IncludeModel
from code_intel.oracles import Case

ORACLES_DIR = Path(oracles.__path__[0])
ORACLE_TIMEOUT_S = 600


@dataclass(frozen=True)
class FastapiRuntime:
    python: str  # sys.executable; subprocesses started with it import the same fastapi
    fastapi: str
    include_model: IncludeModel


@pytest.fixture(scope="session")
def fastapi_runtime() -> FastapiRuntime:
    try:
        importlib.import_module("fastapi")
    except ImportError as exc:
        unavailable(f"could not import fastapi ({type(exc).__name__}: {exc}); install a fastapi dependency group")
    version = importlib.metadata.version("fastapi")
    return FastapiRuntime(sys.executable, version, include_model.for_version(version, source=None))


@dataclass(frozen=True)
class OracleResults:
    root: Path  # import root holding one case_<id> package per case
    cases: dict[str, Case]
    results: dict  # the oracle's --out JSON


def _load_cases(module_name: str, attribute: str) -> dict[str, Case]:
    module = importlib.import_module(f"code_intel.oracles.{module_name}")
    cases = getattr(module, attribute, None)
    if not isinstance(cases, dict):
        pytest.fail(f"oracles/{module_name}.py: {attribute} must be a dict of case id -> Case", pytrace=False)
    return cases


@pytest.fixture(scope="session")
def fastapi_oracle_results(fastapi_runtime: FastapiRuntime, tmp_path_factory: pytest.TempPathFactory) -> OracleResults:
    """Every FastAPI oracle case written to disk and run through the runtime oracle once.

    Fails when two case modules define the same case id.
    """
    cases: dict[str, Case] = {}
    for module_name, attribute in (("fastapi_cases", "CASES"), ("fastapi_dependency_cases", "DEPENDENCY_CASES")):
        for case_id, case in _load_cases(module_name, attribute).items():
            if case_id in cases:
                pytest.fail(f"oracles/{module_name}.py: case id {case_id!r} is defined twice", pytrace=False)
            cases[case_id] = case

    root = tmp_path_factory.mktemp("fastapi_cases")
    for case_id, case in cases.items():
        prefix = f"case_{case_id}/"
        for rel, source in case.files.items():
            if not rel.startswith(prefix):
                pytest.fail(f"oracle case {case_id!r}: file {rel!r} must start with {prefix!r}", pytrace=False)
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
    out = root / "oracle_results.json"
    proc = subprocess.run(
        [
            fastapi_runtime.python,
            str(ORACLES_DIR / "fastapi_oracle.py"),
            "--cases",
            str(root),
            "--repo-root",
            str(root),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=ORACLE_TIMEOUT_S,
        cwd=str(root),
        check=False,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-20:])
        pytest.fail(f"oracles/fastapi_oracle.py exited with code {proc.returncode}:\n{tail}", pytrace=False)
    return OracleResults(root=root, cases=cases, results=json.loads(out.read_text(encoding="utf-8")))
