"""Ground-truth oracles and their synthetic cases.

``fastapi_cases.CASES`` and ``fastapi_dependency_cases.DEPENDENCY_CASES`` map a
case id (``"c01"``) to a ``Case``. Each case is one importable package named
``case_<id>``; every key of ``files`` is a path relative to the import root and
starts with ``case_<id>/``. ``app`` is ``"case_<id>.<module>:<attribute>"``.
``allow`` lists limitation kinds the analyzer may report on that case where it is
sound but not exact.
"""

from __future__ import annotations

from typing import NamedTuple


class Case(NamedTuple):
    files: dict[str, str]
    app: str
    allow: frozenset[str] = frozenset()


def case_package(case_id: str) -> str:
    return f"case_{case_id}"
