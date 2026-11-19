"""A definition file (``package.json``, ``.npmrc``, makefile or mise file) the
docs check could not read, decode or parse.

It is not a run-level diagnostic: only the command mentions that depend on the
file become ``not_checked``, with ``Unreadable.reason`` as the reason.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unreadable:
    file: str
    error: str

    @property
    def reason(self) -> str:
        return f"{self.file} could not be read: {self.error}"


def unreadable(rel: str, exc: BaseException) -> Unreadable:
    if isinstance(exc, RecursionError):
        return Unreadable(rel, "nested too deeply to parse")
    if isinstance(exc, UnicodeDecodeError):
        return Unreadable(rel, f"not UTF-8 (byte {exc.object[exc.start]:#04x} at offset {exc.start})")
    if isinstance(exc, OSError):
        return Unreadable(rel, exc.strerror or type(exc).__name__)
    return Unreadable(rel, str(exc) or type(exc).__name__)
