"""Smoke test of an installed stack-intel wheel, run by the venv's interpreter
that has only the wheel and its dependencies installed:

    <venv>/bin/python tests/wheel_smoke.py

Uses the stdlib and ``mcp`` only, so it runs without dev dependencies and
without ``src/`` on the path. Not collected by pytest."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stack_intel

CHECKOUT = Path(__file__).resolve().parent.parent
BIN = Path(sys.executable).parent
STACK_INTEL = BIN / "stack-intel"
TIMEOUT = 120

TOOLS = {
    "di_deps",
    "di_reverse",
    "route_match",
    "route_unmatched",
    "status",
    "jsx_usage",
    "symbol_resolve",
    "doc_refs",
    "refresh",
}

CONFIG = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"
python_version_file = ".python-version"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"

[[frontend.clients]]
kind = "global"
name = "fetch"
url_arg = 0
init_arg = 1
same_origin_only = true

[docs]
scope = ["docs/**/*.md"]
route_prefixes = ["/api"]

[docs.reachability]
root = "docs/README.md"
include = ["docs/**/*.md"]
exclude = []
"""

MAIN = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/items")
def list_items() -> list[int]:
    return []
"""

ITEMS_TSX = """\
export async function loadItems(): Promise<unknown> {
  const response = await fetch("/api/items");
  return response.json();
}

export const ItemList = () => <ul />;
"""

GUIDE = """\
# Guide

`GET /api/items` lists the items; the handler is in `backend/service_backend/main.py`.

The frontend renders them with `ItemList`.

Build the frontend with `npm run build`.
"""

FILES = {
    ".stack-intel/config.toml": CONFIG,
    ".python-version": "3.14\n",
    "backend/service_backend/__init__.py": "",
    "backend/service_backend/main.py": MAIN,
    "frontend/package.json": '{"name": "web", "private": true, "scripts": {"build": "tsc"}}\n',
    "frontend/tsconfig.json": '{"compilerOptions": {"jsx": "react-jsx", "strict": true}, "include": ["src"]}\n',
    "frontend/src/items.tsx": ITEMS_TSX,
    "docs/README.md": "# Docs\n\n- [Guide](guide.md)\n",
    "docs/guide.md": GUIDE,
}

CLEAN_SUMMARY = "doc-check: 0 findings in 2 docs (5 references, 5 checked); 0 agent files"
ROUTE_NOT_FOUND = "docs/guide.md:3: route not found: GET /api/items (not in the analyzed backend routes)"


def check(condition: bool, message: str) -> None:
    if not condition:
        sys.exit(f"wheel smoke: FAIL: {message}")


def run(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(STACK_INTEL), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def check_ok(proc: subprocess.CompletedProcess[str], what: str) -> None:
    check(proc.returncode == 0, f"{what} exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def check_installed_package() -> None:
    package = Path(stack_intel.__file__).resolve().parent
    check(package.is_relative_to(Path(sys.prefix).resolve()), f"stack_intel imported from {package}, not the venv")
    check(not package.is_relative_to(CHECKOUT), f"stack_intel imported from the checkout: {package}")
    files = resources.files("stack_intel")
    for rel in ("ts/helper.cjs", "ts/package.json", "ts/package-lock.json"):
        check(files.joinpath(rel).is_file(), f"{rel} missing from the installed package")
    check(any(package.glob("ts/*.cjs")) and any(package.glob("oracles/*.cjs")), "no ts/*.cjs or oracles/*.cjs")
    leaked = [str(p) for p in package.rglob("node_modules")]
    check(not leaked, f"node_modules packaged: {leaked}")
    record = importlib.metadata.distribution("stack-intel").files or []
    packaged_cjs = {str(f) for f in record if str(f).endswith(".cjs")}
    for path in [*package.glob("ts/*.cjs"), *package.glob("oracles/*.cjs")]:
        check(path.relative_to(package.parent).as_posix() in packaged_cjs, f"{path} is not in the wheel's RECORD")


def check_version(env: dict[str, str], cwd: Path) -> None:
    proc = run(env, cwd, "--version")
    check_ok(proc, "stack-intel --version")
    expected = f"stack-intel {importlib.metadata.version('stack-intel')}"
    check(proc.stdout.strip() == expected, f"--version printed {proc.stdout!r}, expected {expected!r}")


def write_fixture(repo: Path, env: dict[str, str]) -> None:
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for git in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *git], cwd=repo, env=env, check=True)


def check_commands(env: dict[str, str], repo: Path) -> None:
    check_ok(run(env, repo, "setup"), "stack-intel setup")
    check_ok(run(env, repo, "config-check"), "stack-intel config-check")
    proc = run(env, repo, "doc-check")
    check_ok(proc, "stack-intel doc-check on the clean fixture")
    check(proc.stdout == "", f"doc-check printed findings on the clean fixture:\n{proc.stdout}")
    check(CLEAN_SUMMARY in proc.stderr.splitlines(), f"expected {CLEAN_SUMMARY!r} in:\n{proc.stderr}")


def check_doc_check_fails_on_drift(env: dict[str, str], repo: Path) -> None:
    main = repo / "backend/service_backend/main.py"
    main.write_text(MAIN.replace('"/api/items"', '"/api/things"'), encoding="utf-8")
    proc = run(env, repo, "doc-check")
    check(proc.returncode == 1, f"doc-check after renaming the route exited {proc.returncode}\n{proc.stderr}")
    check(ROUTE_NOT_FOUND in proc.stdout.splitlines(), f"expected {ROUTE_NOT_FOUND!r} in:\n{proc.stdout}")


async def check_mcp(env: dict[str, str], repo: Path) -> None:
    server = StdioServerParameters(command=str(STACK_INTEL), args=["mcp", "--repo-root", str(repo)], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        check(names == TOOLS, f"tools {sorted(names)}, expected {sorted(TOOLS)}")
        result = await session.call_tool("status", {"output_format": "json"})
        check(not result.is_error, f"status failed: {result.content}")
        (item,) = result.content
        text = getattr(item, "text", "")
        payload = json.loads(text)
        check(payload.get("repo_root") == str(repo), f"status repo_root {payload.get('repo_root')!r}, expected {repo}")


def main() -> None:
    check_installed_package()
    with tempfile.TemporaryDirectory(prefix="stack-intel-smoke-") as tmp:
        root = Path(tmp).resolve()
        (root / "config").mkdir()
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "XDG_CONFIG_HOME": str(root / "config"),
            "STACK_INTEL_CACHE_DIR": str(root / "cache"),
        }
        env.pop("STACK_INTEL_TYPESCRIPT_DIR", None)
        repo = root / "repo"
        check_version(env, root)
        write_fixture(repo, env)
        check_commands(env, repo)
        asyncio.run(asyncio.wait_for(check_mcp(env, repo), TIMEOUT))
        check_doc_check_fails_on_drift(env, repo)
    print("wheel smoke: ok")


if __name__ == "__main__":
    main()
