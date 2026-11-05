"""The include_router model chosen from the FastAPI version backend/uv.lock locks."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_intel import include_model

LOCK = "backend/uv.lock"


def _lock(root: Path, body: str) -> Path:
    path = root / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return root


def _fastapi(*versions: str) -> str:
    return "version = 1\n" + "".join(f'\n[[package]]\nname = "fastapi"\nversion = "{v}"\n' for v in versions)


@pytest.mark.parametrize(
    ("version", "kind"),
    [
        ("0.135.2", "copy"),
        ("0.136.3", "copy"),
        ("0.99", "copy"),
        ("0.137.0", "live"),
        ("0.137", "live"),
        ("0.143.0", "live"),
        ("1.0.0", "live"),
    ],
)
def test_release_versions_choose_copy_before_0_137_and_live_from_it(version: str, kind: str) -> None:
    model = include_model.for_version(version, source="backend/uv.lock")
    assert (model.kind, model.fastapi_version, model.source, model.reason) == (kind, version, "backend/uv.lock", None)


@pytest.mark.parametrize("version", ["0.137.0rc1", "0.137.0.dev3", "0.136.3+local", "latest"])
def test_a_version_that_is_not_a_plain_release_gives_the_unknown_model(version: str) -> None:
    model = include_model.for_version(version, source="backend/uv.lock")
    assert (model.kind, model.fastapi_version) == ("unknown", version)
    assert model.reason == f"FastAPI {version} is not a plain release version"


def test_read_takes_the_version_from_the_backend_lock(tmp_path: Path) -> None:
    assert include_model.read(_lock(tmp_path, _fastapi("0.135.2")), LOCK).kind == "copy"
    assert include_model.read(_lock(tmp_path, _fastapi("0.143.0")), LOCK).kind == "live"


def test_a_missing_lock_gives_the_unknown_model_naming_the_lock(tmp_path: Path) -> None:
    model = include_model.read(tmp_path, LOCK)
    assert (model.kind, model.fastapi_version, model.source) == ("unknown", None, "backend/uv.lock")
    assert model.reason is not None and model.reason.startswith("backend/uv.lock: could not read lockfile")
    assert str(tmp_path) not in model.reason


def test_a_lock_without_fastapi_gives_the_unknown_model(tmp_path: Path) -> None:
    model = include_model.read(
        _lock(tmp_path, 'version = 1\n\n[[package]]\nname = "starlette"\nversion = "1.3.1"\n'), LOCK
    )
    assert model.kind == "unknown"
    assert model.reason == "backend/uv.lock: package 'fastapi' not found"


def test_a_lock_with_two_fastapi_versions_gives_the_unknown_model(tmp_path: Path) -> None:
    model = include_model.read(_lock(tmp_path, _fastapi("0.135.2", "0.143.0")), LOCK)
    assert model.kind == "unknown"
    assert model.reason == "backend/uv.lock: package 'fastapi' is locked at several versions ['0.135.2', '0.143.0']"


def test_a_malformed_lock_gives_the_unknown_model(tmp_path: Path) -> None:
    model = include_model.read(_lock(tmp_path, "[[package]\n"), LOCK)
    assert model.kind == "unknown"
    assert model.reason is not None and "TOMLDecodeError" in model.reason


def test_describe_names_the_version_and_what_the_model_does() -> None:
    copy = include_model.for_version("0.135.2", source="backend/uv.lock")
    live = include_model.for_version("0.143.0", source="backend/uv.lock")
    unknown = include_model.for_version(
        None, source="backend/uv.lock", reason="backend/uv.lock: package 'fastapi' not found"
    )
    assert include_model.describe(copy) == (
        "FastAPI 0.135.2 (backend/uv.lock): include_router copies a router's routes when it runs"
    )
    assert "0.143.0" in include_model.describe(live) and "routes added later" in include_model.describe(live)
    assert include_model.describe(unknown).startswith("FastAPI version unknown (backend/uv.lock): backend/uv.lock:")


def test_without_a_configured_lock_the_model_is_unknown(tmp_path: Path) -> None:
    model = include_model.read(tmp_path, None)
    assert (model.kind, model.fastapi_version, model.source) == ("unknown", None, None)
    assert model.reason == "no lockfile is configured"
