"""A checkout whose app mounts routers and reads dependencies from an installed
library, for the capture command.

``extlib`` stands for an installed distribution: it lives in a ``site``
directory outside the import root, with ``.dist-info`` metadata, and reaches the
capture's interpreter through ``[backend.capture] env``'s ``PYTHONPATH``. Its
shapes are the ones a static reader cannot follow: a router factory that is a
method of a generic-subscripted class, a module-level factory, a router that
includes a nested one, a signature set at runtime, a closure, a class used as
``Depends()``, and a consumer callable passed into the library.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
from pathlib import Path

EXTLIB_VERSION = "1.2.3"

EXTLIB = """\
import inspect
from typing import Generic, TypeVar

from fastapi import APIRouter, Depends

T = TypeVar("T")


def audit() -> str:
    return "audit"


def count_hits() -> int:
    return 1


class Settings:
    def __init__(self, audit_value: str = Depends(audit)) -> None:
        self.audit_value = audit_value


def signed(**kwargs):
    return kwargs


signed.__signature__ = inspect.Signature(
    [inspect.Parameter("audit_value", inspect.Parameter.KEYWORD_ONLY, default=Depends(audit))]
)


class Users(Generic[T]):
    def __init__(self, manager) -> None:
        self.manager = manager

    def current(self, active: bool = False):
        def current_dependency(token: str = Depends(audit), manager=Depends(self.manager)):
            return token

        return current_dependency

    def get_router(self, schema) -> APIRouter:
        router = APIRouter(dependencies=[Depends(audit)])

        @router.get("/me")
        def me(user=Depends(self.manager)):
            return user

        @router.post("/rename", dependencies=[Depends(count_hits)])
        def rename():
            return None

        return router


def make_status_router() -> APIRouter:
    router = APIRouter()

    @router.get("/ping")
    def ping():
        return "pong"

    @router.websocket("/live")
    async def live(websocket):
        await websocket.accept()

    return router


def make_nested_router() -> APIRouter:
    inner = APIRouter(prefix="/inner", dependencies=[Depends(audit)])

    @inner.get("/item")
    def item():
        return 1

    outer = APIRouter(dependencies=[Depends(count_hits)])
    outer.include_router(inner, prefix="/v1", dependencies=[Depends(signed)])
    return outer


def make_mixed_router(handler) -> APIRouter:
    router = APIRouter()

    @router.get("/lib")
    def lib():
        return 1

    router.add_api_route("/own", handler)
    return router
"""

DEPS = """\
def get_manager():
    return "manager"
"""

MAIN = """\
import extlib
from fastapi import Depends, FastAPI

from pkg.deps import get_manager

app = FastAPI()
users = extlib.Users[dict](get_manager)
current = users.current(active=True)


def own_handler():
    return "own"


@app.get("/own")
def own(user=Depends(current)):
    return user


@app.get("/signed")
def signed(values=Depends(extlib.signed), settings=Depends(extlib.Settings)):
    return values


app.include_router(users.get_router(dict), prefix="/users", dependencies=[Depends(get_manager)])
app.include_router(extlib.make_status_router(), prefix="/status")
app.include_router(extlib.make_nested_router(), prefix="/nested", dependencies=[Depends(get_manager)])
app.include_router(extlib.make_mixed_router(own_handler), prefix="/mixed")
"""

CONFIG = """\
[backend]
root = "backend"
package = "pkg"
app = "pkg.main:app"
lock = "backend/uv.lock"

[backend.capture]
python = "backend/.venv/bin/python"
env = {{ PYTHONPATH = {site}, SYNTHETIC = "1" }}

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"
"""


def write_extlib(site: Path) -> Path:
    """The installed distribution in ``site``; returns ``site``."""
    (site / "extlib").mkdir(parents=True, exist_ok=True)
    (site / "extlib" / "__init__.py").write_text(EXTLIB, encoding="utf-8")
    info = site / f"extlib-{EXTLIB_VERSION}.dist-info"
    info.mkdir(exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: extlib\nVersion: {EXTLIB_VERSION}\n", encoding="utf-8"
    )
    (info / "top_level.txt").write_text("extlib\n", encoding="utf-8")
    return site


def lock_text(fastapi: str | None = None, extlib: str = EXTLIB_VERSION) -> str:
    fastapi = fastapi or importlib.metadata.version("fastapi")
    return (
        f'[[package]]\nname = "fastapi"\nversion = "{fastapi}"\n\n[[package]]\nname = "extlib"\nversion = "{extlib}"\n'
    )


def checkout(root: Path, site: Path, *, lock: str | None = None) -> Path:
    """A git work tree with the app, a lock, the config and the installed library in ``site``."""
    write_extlib(site)
    files = {
        ".code-intel/config.toml": CONFIG.format(site=json.dumps(str(site))),
        "backend/pkg/__init__.py": "",
        "backend/pkg/deps.py": DEPS,
        "backend/pkg/main.py": MAIN,
        "backend/uv.lock": lock if lock is not None else lock_text(),
        "frontend/tsconfig.json": "{}\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    return root
