"""The script names a ``package.json`` defines, and whether an ``.npmrc`` sets
``if-present``, for the docs check's package script references."""

from __future__ import annotations

import json

from stack_intel.definition_files import Unreadable, unreadable


def package_scripts_from_bytes(rel: str, data: bytes) -> frozenset[str] | Unreadable:
    """The keys of the manifest's ``scripts`` object (none when it has none)."""
    try:
        manifest = json.loads(data)
    except (ValueError, RecursionError) as exc:
        return unreadable(rel, exc)
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    return frozenset(scripts if isinstance(scripts, dict) else ())


def npmrc_sets_if_present(rel: str, data: bytes) -> bool | Unreadable:
    """Whether an ``.npmrc`` sets ``if-present``. In npm's ini format a bare
    key (``if-present``) or an array key (``if-present[]=x``) is true, and only
    ``false`` and ``0`` unset it."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return unreadable(rel, exc)
    for line in text.splitlines():
        key, equals, value = line.strip().partition("=")
        if key.strip().removesuffix("[]") != "if-present":
            continue
        if not equals or value.strip().strip("\"'").lower() not in ("false", "0"):
            return True
    return False
