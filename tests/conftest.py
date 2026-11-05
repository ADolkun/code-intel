"""Shared fixtures for the code-intel tests.

``CODE_INTEL_REQUIRE_TOOLCHAIN=1`` (CI) turns a missing node, TypeScript or
fastapi into a test failure instead of a skip. Any other value, or unset, skips.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from code_intel import code_paths

REQUIRE_TOOLCHAIN_ENV = "CODE_INTEL_REQUIRE_TOOLCHAIN"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "real_ctime: keep the kernel's ctime in file stamps where ctime_follows_mtime applies"
    )


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
