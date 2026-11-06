"""The script names a ``package.json`` defines, for the docs check's npm script references."""

from __future__ import annotations

import json

from code_intel.doc_rules import STAGE
from code_intel.problems import Problem, diagnostic


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
