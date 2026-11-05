"""Docs-check settings for tests, built from TOML written as Python values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from code_intel.config import CONFIG_REL, parse_config
from code_intel.doc_rules import DocRefsConfig

FAKE_ROOT = Path("/fake-repo")

LAYOUT = """\
[backend]
root = "backend"
package = "service_backend"
app = "service_backend.main:app"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"

"""

DEFAULT_DOCS: dict[str, Any] = {
    "scope": ["docs/**/*.md", "AGENTS.md", ".claude/skills/**/*.md", "tools/*/README.md"],
    "route_prefixes": ["/api"],
    "reachability": {"root": "docs/README.md", "include": ["docs/**/*.md"], "exclude": ["docs/plans/**"]},
    "rules": [
        {"paths": ["docs/plans/**", "docs/vendor/**"], "checks": ["links"], "reason": "links only"},
        {"paths": [".claude/skills/generic-a/**"], "checks": ["links", "paths"], "reason": "generic"},
        {"paths": [".claude/skills/generic-b/**"], "checks": ["links", "paths", "routes"], "reason": "generic"},
    ],
    "ignore": [],
}


def _value(value: object) -> str:
    return json.dumps(value)


def _table(header: str, fields: Mapping[str, object]) -> str:
    return "".join([f"{header}\n", *(f"{key} = {_value(value)}\n" for key, value in fields.items()), "\n"])


def docs_toml(docs: Mapping[str, Any]) -> str:
    """The ``[docs]`` tables for ``docs`` (``scope``, ``route_prefixes``, ``reachability``, ``rules``, ``ignore``)."""
    top = {key: docs[key] for key in ("scope", "route_prefixes") if key in docs}
    out = [_table("[docs]", top)]
    if docs.get("reachability") is not None:
        out.append(_table("[docs.reachability]", docs["reachability"]))
    for rule in docs.get("rules", ()):
        out.append(_table("[[docs.rules]]", rule))
    for entry in docs.get("ignore", ()):
        out.append(_table("[[docs.ignore]]", entry))
    return "".join(out)


def config_text(docs: Mapping[str, Any] | None = None, *, layout: str = LAYOUT, **overrides: Any) -> str:
    """A whole config file: ``layout`` (the backend and frontend tables) and ``DEFAULT_DOCS`` with ``overrides``."""
    merged = {**DEFAULT_DOCS, **(docs or {}), **overrides}
    return layout + docs_toml(merged)


def doc_config(docs: Mapping[str, Any] | None = None, *, layout: str = LAYOUT, **overrides: Any) -> DocRefsConfig:
    """The docs settings of ``config_text``, as if the file sat at ``CONFIG_REL`` in a repository."""
    text = config_text(docs, layout=layout, **overrides)
    config = parse_config(text, CONFIG_REL, FAKE_ROOT / CONFIG_REL)
    return DocRefsConfig.from_config(config, FAKE_ROOT)
