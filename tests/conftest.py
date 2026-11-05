"""Shared fixtures for the code-intel tests.

``CODE_INTEL_REQUIRE_TOOLCHAIN=1`` (CI) turns a missing node, TypeScript or
fastapi into a test failure instead of a skip. Any other value, or unset, skips.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import pytest

from code_intel import code_paths, provision, ts_bridge

REQUIRE_TOOLCHAIN_ENV = "CODE_INTEL_REQUIRE_TOOLCHAIN"

pytest_plugins = ["fastapi_fixtures"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_ctime: keep the kernel's ctime in file stamps where ctime_follows_mtime applies"
    )


@pytest.fixture(autouse=True)
def no_automatic_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that finds no toolchain must not start an npm install; the tests of the install pass their own env."""
    monkeypatch.setenv(provision.NO_AUTO_ENV, "1")


@pytest.fixture
def ctime_follows_mtime(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """File stamps take their ctime from mtime.

    Tests that model a file's timeline with ``os.utime`` and an injected clock
    need this: the kernel's ctime is wall-clock time and cannot be set, so it
    would make every stamp racy against the injected clock, and it would change
    the stamp of a same-size rewrite meant to keep it, so a test of the racy
    content check would pass without reaching that check. A test marked
    ``real_ctime`` keeps the kernel's value, to check what ctime itself catches.
    """
    if request.node.get_closest_marker("real_ctime") is not None:
        return

    def from_stat(cls: type[code_paths.FileStamp], st: os.stat_result) -> code_paths.FileStamp:
        return cls(st.st_mtime_ns, st.st_size, st.st_ino, st.st_mtime_ns)

    monkeypatch.setattr(code_paths.FileStamp, "from_stat", classmethod(from_stat))


def _wait_for_ctime_tick(path: Path) -> None:
    """Return once a change next to ``path`` gets a ctime other than its own.

    Filesystems with coarse timestamps give writes in one tick the same ctime;
    a rewrite in the tick of the stamp is the racy case the stamp's age guards,
    so a test of what ctime catches has to write in a later tick."""
    ctime_ns = path.stat().st_ctime_ns
    probe = path.with_name(path.name + ".tick")
    try:
        for _ in range(200):
            probe.write_bytes(b"")
            if probe.stat().st_ctime_ns != ctime_ns:
                return
            time.sleep(0.01)
    finally:
        probe.unlink(missing_ok=True)
    pytest.fail(f"the ctime next to {path} stayed at {ctime_ns} for 2 s, so no rewrite can change it")


@pytest.fixture
def rewrite_keeping_mtime() -> Callable[[Path, str], None]:
    """Rewrite a file with same-size text and set its mtime back, as ``touch -r``
    or ``cp -p`` onto the file would: mtime, size and inode end up as before,
    and only the kernel's ctime shows the write."""

    def rewrite(path: Path, text: str) -> None:
        before = path.stat()
        _wait_for_ctime_tick(path)
        path.write_text(text, encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert (after.st_mtime_ns, after.st_size, after.st_ino) == (before.st_mtime_ns, before.st_size, before.st_ino)

    return rewrite


def toolchain_required() -> bool:
    return os.environ.get(REQUIRE_TOOLCHAIN_ENV) == "1"


def unavailable(message: str) -> NoReturn:
    """Skip, or fail when ``CODE_INTEL_REQUIRE_TOOLCHAIN=1``."""
    if toolchain_required():
        pytest.fail(f"{message} ({REQUIRE_TOOLCHAIN_ENV}=1)", pytrace=False)
    pytest.skip(message)


@pytest.fixture(scope="session")
def typescript_toolchain() -> ts_bridge.Toolchain:
    """The real toolchain: ``CODE_INTEL_TYPESCRIPT_DIR``, else the install ``code-intel setup`` made."""
    toolchain = ts_bridge.find_toolchain(Path(__file__).resolve().parent.parent)
    if not toolchain.available:
        problem = toolchain.problem
        detail = (
            f"{problem.message}. Fix: {problem.fix}"
            if problem
            else "ts_bridge.find_toolchain reported no problem, yet node or TypeScript is missing"
        )
        unavailable(f"TypeScript toolchain unavailable: {detail}")
    return toolchain


@pytest.fixture(scope="module")
def module_git(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Hermetic git for a module whose trees are git work trees (``fixture_repo.git_init``).

    The TypeScript install lives under the user's cache, which hermetic git
    moves with ``HOME``, so it is pinned first."""
    from fixture_repo import hermetic_git

    cache = ts_bridge.cache_dir()
    with pytest.MonkeyPatch.context() as patch:
        hermetic_git(patch, tmp_path_factory.mktemp("home"))
        patch.setenv(ts_bridge.CACHE_ENV, str(cache))
        yield


@dataclass
class FactsRun:
    root: Path
    response: dict
    by_rel: dict[str, dict] = field(default_factory=dict)  # tree key -> file entry


@pytest.fixture(scope="session")
def facts_helper(
    typescript_toolchain: ts_bridge.Toolchain, tmp_path_factory: pytest.TempPathFactory
) -> Callable[..., FactsRun]:
    """``run(files) -> FactsRun``: write ``{relative path: source}`` to a fresh
    directory and run the helper's ``facts`` mode over every file once.

    Results are memoized per tree, so a module can batch its fixtures into one
    tree (one helper process) and look files up in ``by_rel``.
    """
    memo: dict[tuple, FactsRun] = {}

    def run(
        files: Mapping[str, str],
        *,
        snippet_max: int = 500,
        signature_max: int = 200,
        attribute_tags: tuple[str, ...] = (),
    ) -> FactsRun:
        key = (tuple(sorted(files.items())), snippet_max, signature_max, attribute_tags)
        if key in memo:
            return memo[key]
        root = tmp_path_factory.mktemp("facts")
        paths: dict[str, Path] = {}
        for rel, source in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8", newline="")
            paths[rel] = path
        response = ts_bridge.run_helper(
            typescript_toolchain,
            "facts",
            {
                "files": [str(p) for p in paths.values()],
                "snippet_max": snippet_max,
                "signature_max": signature_max,
                "attribute_tags": list(attribute_tags),
            },
            timeout_s=ts_bridge.FACTS_TIMEOUT_S,
        )
        entries = {entry["path"]: entry for entry in response["files"]}
        result = FactsRun(root=root, response=response, by_rel={rel: entries[str(p)] for rel, p in paths.items()})
        memo[key] = result
        return result

    return run
