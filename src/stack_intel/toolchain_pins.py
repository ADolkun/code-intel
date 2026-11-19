"""Pinned tool versions read from lockfiles: the consumer's ``uv.lock`` (the
FastAPI that decides the include model) and the TypeScript helper's own
``ts/package-lock.json``."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

BACKEND_PINNED = ("fastapi", "starlette")
HELPER_LOCK = Path(__file__).resolve().parent / "ts" / "package-lock.json"


class PinError(LookupError):
    """A lockfile is unreadable or does not pin a required package."""


def backend_pins(lock_path: Path) -> dict[str, str]:
    """``{"fastapi": ..., "starlette": ...}`` from the backend's ``uv.lock``."""
    return locked_versions(lock_path, BACKEND_PINNED)


def locked_versions(lock_path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """The one version a ``uv.lock`` locks for each of ``names``."""
    try:
        with lock_path.open("rb") as fh:
            lock = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        raise PinError(f"{lock_path}: could not read lockfile ({type(exc).__name__}: {exc})") from exc
    versions: dict[str, set[str]] = {}
    packages = lock.get("package", [])
    for package in packages if isinstance(packages, list) else ():
        name = package.get("name") if isinstance(package, dict) else None
        if name in names and isinstance(package.get("version"), str):
            versions.setdefault(name, set()).add(package["version"])
    pins: dict[str, str] = {}
    for name in names:
        found = versions.get(name)
        if not found:
            raise PinError(f"{lock_path}: package {name!r} not found")
        if len(found) > 1:
            raise PinError(f"{lock_path}: package {name!r} is locked at several versions {sorted(found)}")
        pins[name] = next(iter(found))
    return pins


def helper_typescript_version(lock_path: Path = HELPER_LOCK) -> str | None:
    """``packages["node_modules/typescript"].version`` from the helper's ``ts/package-lock.json``.

    None when the lockfile is missing, unreadable or has no TypeScript entry.
    """
    try:
        with lock_path.open("rb") as fh:
            lock = json.load(fh)
    except OSError, ValueError:
        return None
    entry = lock.get("packages", {}).get("node_modules/typescript") if isinstance(lock, dict) else None
    version = entry.get("version") if isinstance(entry, dict) else None
    return version if isinstance(version, str) else None
