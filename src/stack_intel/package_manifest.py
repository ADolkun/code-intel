"""The script names a ``package.json`` defines, and whether an ``.npmrc`` sets
``if-present``, for the docs check's package script references."""

from __future__ import annotations

import json

from stack_intel.doc_rules import STAGE
from stack_intel.problems import Problem, diagnostic


def package_scripts_from_bytes(
    rel: str, data: bytes, *, stage: str = STAGE
) -> tuple[frozenset[str] | None, list[Problem]]:
    """The keys of the manifest's ``scripts`` object (none when it has none), or
    None with a diagnostic when the file cannot be decoded or parsed."""
    try:
        manifest = json.loads(data)
    except UnicodeDecodeError as exc:
        return None, [diagnostic("decode_failed", stage=stage, step="decode file", file=rel, exc=exc)]
    except ValueError as exc:
        return None, [diagnostic("parse_failed", stage=stage, step="parse file", file=rel, exc=exc)]
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    return frozenset(scripts if isinstance(scripts, dict) else ()), []


def npmrc_sets_if_present(data: bytes) -> bool:
    """Whether an ``.npmrc`` sets ``if-present`` to anything but false. A file
    that is not UTF-8 counts as setting it: it cannot be shown not to."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals and key.strip() == "if-present" and value.strip().lower() not in ("false", '"false"'):
            return True
    return False
