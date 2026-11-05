"""Shared typing for ``ast`` nodes that several analysis modules inspect."""

from __future__ import annotations

import ast
from collections.abc import Sequence

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def start_position(node: ast.AST | None) -> tuple[int, int] | None:
    """``(line, column)`` of a node that has a position; None for the rest (a ``Module``, ``arguments``)."""
    lineno = getattr(node, "lineno", None)
    if lineno is None:
        return None
    return (lineno, getattr(node, "col_offset", 0) or 0)


def literal_strings(elements: Sequence[ast.expr]) -> list[str] | None:
    """The values of ``elements`` when every one is a string literal, else None."""
    values = [e.value for e in elements if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return values if len(values) == len(elements) else None


def body_nodes(fn: FunctionNode) -> list[ast.AST]:
    """The nodes a function runs: its statements, or the one expression of a lambda."""
    return [*fn.body] if isinstance(fn.body, list) else [fn.body]
