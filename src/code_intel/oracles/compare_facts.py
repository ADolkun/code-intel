#!/usr/bin/env python3
"""Compare TypeScript facts from the tools against the AST oracles, exactly.

    python compare_facts.py jsx ORACLE.json TOOL.json
    python compare_facts.py decls ORACLE.json TOOL.json

``jsx``: ``oracles/jsx_oracle.cjs`` output against ``code-intel dump jsx --all --json``.
Rows are keyed by ``(file, line, column, tag)``; ``kind``, ``parent`` and
``is_test`` must agree. Oracle rows from ``.ts``/``.mts``/``.cts`` files are
compared too, so an element there (which the tool never scans) is reported as
missing.

``decls``: ``oracles/decl_oracle.cjs`` output against the TypeScript
declarations of ``code-intel dump symbols --json``. Rows are keyed by
``(file, line, column, name)``; ``kind``, ``end_line``, ``scope``, ``container``,
``exported`` and ``is_test`` must agree. Signatures are presentation and are not
compared.

Prints totals, missing and extra rows, and mismatches grouped by field, then
exits 1 when anything differs and 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

JSX_FIELDS = ("kind", "parent", "is_test")
DECL_FIELDS = ("kind", "end_line", "scope", "container", "exported", "is_test")
EXAMPLES = 10

Key = tuple
Row = Mapping[str, object]


def _jsx_row(row: Row) -> dict:
    """Oracle rows use ``name``/``col``; tool rows use ``tag``/``column``."""
    return {
        "file": row["file"],
        "tag": row.get("tag", row.get("name")),
        "line": row["line"],
        "column": row.get("column", row.get("col")),
        "kind": row["kind"],
        "parent": row["parent"],
        "is_test": row["is_test"],
    }


def _decl_row(row: Row) -> dict:
    return {key: row[key] for key in ("file", "name", "line", "column", *DECL_FIELDS)}


def _index(rows: Iterable[dict], key_fields: Sequence[str], label: str) -> dict[Key, dict]:
    out: dict[Key, dict] = {}
    for row in rows:
        key = tuple(row[k] for k in key_fields)
        if key in out:
            raise SystemExit(f"compare_facts: {label} has two rows for {key}")
        out[key] = row
    return out


def compare(
    oracle_rows: Iterable[dict], tool_rows: Iterable[dict], key_fields: Sequence[str], fields: Sequence[str]
) -> dict:
    """Missing, extra and per-field mismatches between two row sets."""
    oracle = _index(oracle_rows, key_fields, "oracle")
    tool = _index(tool_rows, key_fields, "tool")
    missing = sorted(set(oracle) - set(tool))
    extra = sorted(set(tool) - set(oracle))
    mismatches: dict[str, list[tuple[Key, object, object]]] = {}
    for key in sorted(set(oracle) & set(tool)):
        for field in fields:
            if oracle[key][field] != tool[key][field]:
                mismatches.setdefault(field, []).append((key, oracle[key][field], tool[key][field]))
    return {
        "oracle": len(oracle),
        "tool": len(tool),
        "missing": missing,
        "extra": extra,
        "mismatches": mismatches,
    }


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"compare_facts: {path}: could not read file ({type(exc).__name__}: {exc})") from exc
    except ValueError as exc:
        raise SystemExit(f"compare_facts: {path}: could not decode JSON ({type(exc).__name__}: {exc})") from exc


def _rows(doc: dict, key: str, path: Path) -> list:
    rows = doc.get(key)
    if not isinstance(rows, list):
        raise SystemExit(f"compare_facts: {path}: no {key!r} list in the document")
    return rows


def _report(result: dict, label: str) -> bool:
    print(f"{label}: oracle {result['oracle']}, tool {result['tool']}")
    print(f"missing (oracle only): {len(result['missing'])}")
    for key in result["missing"][:EXAMPLES]:
        print(f"  - {key}")
    print(f"extra (tool only): {len(result['extra'])}")
    for key in result["extra"][:EXAMPLES]:
        print(f"  + {key}")
    total = sum(len(v) for v in result["mismatches"].values())
    print(f"field mismatches: {total}")
    for field, items in result["mismatches"].items():
        print(f"  {field}: {len(items)}")
        for key, want, got in items[:EXAMPLES]:
            print(f"    {key}: oracle {want!r}, tool {got!r}")
    return not (result["missing"] or result["extra"] or total)


def _jsx_summary(rows: list[dict]) -> str:
    tests = sum(1 for r in rows if r["is_test"])
    null_parents = sum(1 for r in rows if r["parent"] is None)
    suffixes = Counter(Path(str(r["file"])).suffix for r in rows)
    by_suffix = ", ".join(f"{s} {n}" for s, n in sorted(suffixes.items()))
    return (
        f"elements {len(rows)} (non-test {len(rows) - tests}, test {tests}); "
        f"null parents {null_parents}; by suffix: {by_suffix}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("what", choices=("jsx", "decls"))
    parser.add_argument("oracle", type=Path)
    parser.add_argument("tool", type=Path)
    args = parser.parse_args(argv)
    oracle_doc = _load(args.oracle)
    tool_doc = _load(args.tool)
    if args.what == "jsx":
        oracle_rows = [_jsx_row(r) for r in _rows(oracle_doc, "elements", args.oracle)]
        tool_rows = [_jsx_row(r) for r in _rows(tool_doc, "elements", args.tool)]
        print(f"oracle {_jsx_summary(oracle_rows)}")
        print(f"tool   {_jsx_summary(tool_rows)}")
        result = compare(oracle_rows, tool_rows, ("file", "line", "column", "tag"), JSX_FIELDS)
    else:
        oracle_rows = [_decl_row(r) for r in _rows(oracle_doc, "decls", args.oracle)]
        tool_rows = [_decl_row(r) for r in _rows(tool_doc, "decls", args.tool)]
        kinds = Counter(str(r["kind"]) for r in oracle_rows)
        print("oracle kinds: " + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))
        result = compare(oracle_rows, tool_rows, ("file", "line", "column", "name"), DECL_FIELDS)
    return 0 if _report(result, args.what) else 1


if __name__ == "__main__":
    sys.exit(main())
