"""Fixture repositories for the tests: a backend package, a frontend and the consumer config,
written to a temporary directory, plus hermetic git and a fake clock."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import unavailable

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

# Far enough after every fixture mtime that no stamp is racy.
LATE_NS = 10_000 * 1_000_000_000

MINIMAL_CONFIG = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"
"""

FULL_CONFIG = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"
lock = "backend/uv.lock"
python_version_file = "backend/.python-version"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"
lockfile = "frontend/package-lock.json"

[[frontend.clients]]
kind = "instance"
module = "frontend/lib/api.ts"
export = "api"
base_path = "/api"

[[frontend.clients]]
kind = "global"
name = "fetch"
url_arg = 0
init_arg = 1
same_origin_only = true

[[frontend.clients]]
kind = "call"
module = "frontend/lib/http.ts"
export = "request"

[[frontend.clients]]
kind = "options"
module = "frontend/lib/download.ts"
export = "download"

[frontend.pages]
kind = "react-router"
modules = ["frontend/src/App.tsx"]
public_dir = "frontend/public"

[symbols]
primary_prefixes = ["frontend/components/", "backend/service_backend/"]

[docs]
scope = ["docs/**/*.md", "README.md"]
route_prefixes = ["/api"]

[docs.reachability]
root = "docs/README.md"
include = ["docs/**/*.md"]
exclude = []

[[docs.rules]]
paths = ["docs/imported.md"]
checks = ["links"]
reason = "Imported material: only its links must resolve."

[[docs.ignore]]
file = "docs/guide.md"
kind = "route"
text = "/api/external"
reason = "Served by another service."

[agents]
rules_dir = ".claude/rules"
skills_dir = ".claude/skills"
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
"""


def hermetic_git(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Run git with no system, global or user config. ``GIT_CONFIG_GLOBAL``
    alone still leaves git reading the default ``core.excludesFile``
    (``$XDG_CONFIG_HOME/git/ignore``, else ``~/.config/git/ignore``), whose
    patterns would change what counts as ignored, so ``HOME`` and
    ``XDG_CONFIG_HOME`` point at an empty directory too. Skips (fails under
    ``CODE_INTEL_REQUIRE_TOOLCHAIN=1``) when git is missing."""
    if shutil.which("git") is None:
        unavailable("git is not on PATH")
    home.mkdir(parents=True, exist_ok=True)
    for name, value in {**GIT_ENV, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}.items():
        monkeypatch.setenv(name, value)


def git_init(root: Path) -> Path:
    """Make ``root`` a git work tree (nothing committed): its files are untracked, so visible unless ignored."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def write(path: Path, text: str, mtime: int = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


class Clock:
    def __init__(self, now: int = LATE_NS) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def make_repo(root: Path, config: str = FULL_CONFIG) -> Path:
    """A small repository laid out as ``config`` describes, with its config file written."""
    write(root / ".code-intel" / "config.toml", config)
    write(root / "backend" / ".python-version", "3.14\n")
    write(root / "backend" / "service_backend" / "main.py", "app = None\n")
    write(root / "frontend" / "lib" / "api.ts", "export {};\n")
    write(root / "frontend" / "components" / "Page.tsx", "export const Page = () => <div/>;\n")
    write(root / "frontend" / "tsconfig.json", "{}\n")
    return root
