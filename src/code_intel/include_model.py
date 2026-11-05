"""How ``include_router`` treats a child router, by the FastAPI version the backend locks.

Up to FastAPI 0.136, ``include_router`` copies the child's routes into the
parent when it runs, so a route registered on the child afterwards is not
served through that include (``copy``). From 0.137.0 the parent keeps the
child router live (``_IncludedRouter``): every route on the child is served
through the include whenever it was registered, with the include's prefix and
dependencies (``live``). When the version cannot be read the model is
``unknown``, and the router graph lists what only one model would serve as
deferred, so an unknown version can over-list but never drop a served route.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from code_intel import toolchain_pins
from code_intel.backend_model import IncludeModel

if TYPE_CHECKING:
    from code_intel.backend_model import BackendModel
    from code_intel.result_types import DiApp

# The first FastAPI release whose include_router keeps the child router live.
LIVE_SINCE: tuple[int, int, int] = (0, 137, 0)
_RELEASE_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def for_version(version: str | None, *, source: str | None, reason: str | None = None) -> IncludeModel:
    """The model for a locked FastAPI ``version``; ``unknown`` (with ``reason``)
    when there is none or it is not a plain release (a pre-release or local
    version may sit on either side of the boundary)."""
    if version is None:
        return IncludeModel("unknown", None, source, reason or "no FastAPI version given")
    match = _RELEASE_RE.fullmatch(version)
    if match is None:
        return IncludeModel("unknown", version, source, f"FastAPI {version} is not a plain release version")
    release = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    return IncludeModel("live" if release >= LIVE_SINCE else "copy", version, source)


def read(repo_root: Path, lock_rel: str | None) -> IncludeModel:
    """The model for the FastAPI version ``<repo_root>/<lock_rel>`` locks; ``unknown`` without a lock."""
    if lock_rel is None:
        return for_version(None, source=None, reason="no lockfile is configured")
    lock = repo_root / lock_rel
    try:
        version = toolchain_pins.locked_versions(lock, ("fastapi",))["fastapi"]
    except LookupError as exc:  # toolchain_pins.PinError
        return for_version(None, source=lock_rel, reason=str(exc).replace(str(lock), lock_rel))
    return for_version(version, source=lock_rel)


def app_block(model: BackendModel) -> DiApp:
    """The ``app`` block the DI and routes JSON share."""
    return {
        "id": model.app_id,
        "redirect_slashes": model.redirect_slashes,
        "fastapi_version": model.include_model.fastapi_version,
        "include_model": model.include_model.kind,
        "include_model_reason": model.include_model.reason,
    }


def describe(model: IncludeModel) -> str:
    """One line for reports: the version, where it comes from, and the model."""
    where = f" ({model.source})" if model.source else ""
    if model.kind == "copy":
        return f"FastAPI {model.fastapi_version}{where}: include_router copies a router's routes when it runs"
    if model.kind == "live":
        return f"FastAPI {model.fastapi_version}{where}: include_router serves the router live, routes added later included"
    return (
        f"FastAPI version unknown{where}: {model.reason}; routes only one include model serves are listed as deferred"
    )
