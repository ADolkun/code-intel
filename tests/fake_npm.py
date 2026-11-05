"""A stand-in for ``subprocess.run`` that answers ``node --version`` and ``npm ci``."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

NODE = "/usr/local/bin/node"
NPM = "/usr/local/bin/npm"


def which(name: str) -> str | None:
    return {"node": NODE, "npm": NPM}.get(name)


class FakeNpm:
    """``npm ci`` writes what the real one would: a TypeScript package under ``node_modules``, or, with
    ``typescript``, a link to an existing one."""

    def __init__(
        self,
        *,
        npm_exit: int = 0,
        version: str = "7.1.0-beta",
        node_out: bytes = b"v24.21.0\n",
        typescript: Path | None = None,
        node: str = NODE,
    ) -> None:
        self.npm_calls: list[dict] = []
        self.npm_exit = npm_exit
        self.version = version
        self.node_out = node_out
        self.typescript = typescript
        self.node = node
        self.before_exit: Callable[[Path], object] = lambda cwd: None

    def __call__(self, cmd, **kwargs):
        if cmd[0] == self.node:
            return SimpleNamespace(returncode=0, stdout=self.node_out, stderr=b"")
        cwd = Path(kwargs["cwd"])
        self.npm_calls.append(
            {"cmd": cmd, "cwd": cwd, "env": kwargs.get("env"), "files": sorted(p.name for p in cwd.iterdir())}
        )
        self.before_exit(cwd)
        if self.npm_exit:
            return SimpleNamespace(returncode=self.npm_exit, stdout=b"", stderr=b"npm error 404\nnpm error not found\n")
        package = cwd / "node_modules" / "typescript"
        if self.typescript is not None:
            package.parent.mkdir(parents=True)
            package.symlink_to(self.typescript)
        else:
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": "typescript", "version": self.version}))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
