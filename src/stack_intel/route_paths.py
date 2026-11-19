"""Path helpers shared by route matching and the MCP queries."""

from __future__ import annotations

import re

# Starlette's path-parameter syntax: `{name}` or `{name:convertor}`.
PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([a-zA-Z_][a-zA-Z0-9_]*))?\}")


def _strip_query(path: str) -> str:
    return path.split("?", 1)[0]


def path_key(path: str) -> str:
    """Route identity for lookups: the query is dropped and every parameter becomes ``{}``.

    ``/articles/{article_id}`` and ``/articles/{id:int}`` share the key
    ``/articles/{}``. A trailing slash is kept because it is significant when the
    app sets ``redirect_slashes=False``.
    """
    return PARAM_RE.sub("{}", _strip_query(path))


def split_segments(path: str) -> list[str]:
    """Path segments without the query and without the leading ``/``.

    ``/a/{b}`` gives ``["a", "{b}"]``; a trailing slash keeps an empty last
    segment (``/a/`` gives ``["a", ""]``) and ``/`` gives ``[""]``.
    """
    bare = _strip_query(path)
    if bare.startswith("/"):
        bare = bare[1:]
    return bare.split("/")
