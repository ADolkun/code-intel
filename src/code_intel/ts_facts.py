"""Per-file TypeScript facts (JSX elements and declarations) with a per-file cache.

``FactsCache.facts_for`` returns the helper's ``facts`` mode output for a set of
files. Unchanged files come from the cache; every miss goes to one helper run.
A file is unchanged when its ``(mtime_ns, size, ino, ctime_ns)`` stamp matches
(ctime because a rewrite cannot set it back the way it can mtime), and, when
the stamp was "racy" at caching time (changed within two seconds of the
snapshot, git's racy-clean rule), its content hash matches too. The whole cache
is dropped when ``FACTS_SCHEMA`` or the toolchain stamp (node, TypeScript, the
helper's own code) changes. A failed helper run caches nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from code_intel import ts_bridge
from code_intel.code_paths import SourceFile, cache_key, content_digest, is_racy
from code_intel.problems import Problem, diagnostic
from code_intel.result_types import FACTS_SCHEMA
from code_intel.ts_bridge import HelperError, Toolchain, ToolchainUnavailable

__all__ = [
    "FACTS_SCHEMA",
    "FactsCache",
    "FileFacts",
    "HelperError",
    "JsxElement",
    "ToolchainUnavailable",
    "TsDecl",
    "facts_problems",
]

SNIPPET_MAX = 500
SIGNATURE_MAX = 200
# Reading a file for its racy-entry hash: missing, permissions, or a directory.
_HASH_ERRORS: tuple[type[Exception], ...] = (OSError,)
# A malformed helper entry: a missing key or a value of the wrong type.
_ENTRY_ERRORS: tuple[type[Exception], ...] = (KeyError, TypeError, ValueError)


@dataclass(frozen=True)
class JsxElement:
    tag: str
    line: int
    column: int
    kind: Literal["open", "self_close"]
    parent: str | None
    snippet: str
    # Only for a tag listed in ``FactsCache(attribute_tags=...)``: its
    # string-literal attributes by name, and the (line, column) of the nearest
    # enclosing element with the same tag (None at the top).
    attributes: dict[str, str] = field(default_factory=dict)
    ancestor: tuple[int, int] | None = None


@dataclass(frozen=True)
class TsDecl:
    name: str
    kind: str
    line: int
    end_line: int
    column: int
    scope: str
    container: str | None
    exported: bool
    signature: str


@dataclass(frozen=True)
class FileFacts:
    rel: str
    # decode_failed: bytes that are not UTF-8 were read as U+FFFD; the facts
    # come from that text.
    status: Literal["ok", "read_failed", "decode_failed", "parse_failed", "extract_failed"]
    error: str | None
    syntax_errors: int
    first_syntax_error: dict | None
    elements: tuple[JsxElement, ...]
    decls: tuple[TsDecl, ...]


@dataclass
class _Entry:
    stamp: Any  # code_paths.FileStamp
    racy: bool
    content_hash: str | None
    facts: FileFacts


def _file_facts(rel: str, entry: Mapping[str, Any]) -> FileFacts:
    elements = tuple(
        JsxElement(
            tag=str(e["tag"]),
            line=int(e["line"]),
            column=int(e["column"]),
            kind=e["kind"],
            parent=None if e["parent"] is None else str(e["parent"]),
            snippet=str(e["snippet"]),
            attributes={str(k): str(v) for k, v in e.get("attributes", {}).items()},
            ancestor=None if e.get("ancestor") is None else (int(e["ancestor"]["line"]), int(e["ancestor"]["column"])),
        )
        for e in entry["elements"]
    )
    decls = tuple(
        TsDecl(
            name=str(d["name"]),
            kind=str(d["kind"]),
            line=int(d["line"]),
            end_line=int(d["end_line"]),
            column=int(d["column"]),
            scope=str(d["scope"]),
            container=None if d["container"] is None else str(d["container"]),
            exported=bool(d["exported"]),
            signature=str(d["signature"]),
        )
        for d in entry["decls"]
    )
    first = entry["first_syntax_error"]
    return FileFacts(
        rel=rel,
        status=entry["status"],
        error=None if entry["error"] is None else str(entry["error"]),
        syntax_errors=int(entry["syntax_errors"]),
        first_syntax_error=dict(first) if first is not None else None,
        elements=elements,
        decls=decls,
    )


def facts_problems(facts: FileFacts, *, stage: str) -> list[Problem]:
    """Diagnostics for one file's facts: unreadable, undecodable, not
    extractable, or syntax errors.

    Syntax errors and undecodable bytes do not drop the file: its facts come
    from TypeScript's recovered tree or from the text with U+FFFD in place of
    the bad bytes, and the diagnostic says so.
    """
    rel = facts.rel
    out: list[Problem] = []
    if facts.status == "decode_failed":
        out.append(
            diagnostic(
                "decode_failed",
                stage=stage,
                step="decode file",
                file=rel,
                message=f"{rel}: could not decode file as UTF-8 ({facts.error}); facts come from the replaced text",
            )
        )
    if facts.status == "read_failed":
        return [
            diagnostic(
                "read_failed",
                stage=stage,
                step="read file",
                file=rel,
                message=f"{rel}: could not read file ({facts.error})",
            )
        ]
    if facts.status == "parse_failed":
        return [
            diagnostic(
                "parse_failed",
                stage=stage,
                step="parse file",
                file=rel,
                message=f"{rel}: could not parse file ({facts.error})",
            )
        ]
    if facts.status == "extract_failed":
        return [
            diagnostic(
                "analysis_failed",
                stage=stage,
                step="extract facts",
                file=rel,
                message=f"{rel}: could not extract JSX and declarations ({facts.error})",
            )
        ]
    if facts.syntax_errors <= 0:
        return out
    first = facts.first_syntax_error or {}
    count = facts.syntax_errors
    where = (
        f"; first at line {first.get('line')}, column {first.get('column')}: {first.get('message')}" if first else ""
    )
    return [
        *out,
        diagnostic(
            "typescript_syntax_error",
            stage=stage,
            step="parse file",
            file=rel,
            line=first.get("line") if isinstance(first.get("line"), int) else None,
            message=(
                f"TypeScript reported {count} syntax error{'' if count == 1 else 's'} in {rel}{where}; "
                "results come from TypeScript's recovered tree"
            ),
        ),
    ]


class FactsCache:
    """Per-file cache of the helper's ``facts`` output, keyed by normalized path.

    ``toolchain`` is called on every ``facts_for``; the runtime passes its own
    cached probe. ``clock`` and ``run_helper`` are injectable so tests control
    racy timing and count helper runs without patching globals.

    ``attribute_tags`` names the JSX tags whose elements also carry their
    string attributes and nearest same-tag ancestor (``JsxElement.attributes``,
    ``JsxElement.ancestor``). It is part of what the cache holds, so a cache
    serves one set of tags.
    """

    def __init__(
        self,
        repo_root: Path,
        toolchain: Callable[[], Toolchain],
        *,
        clock: Callable[[], int] = time.time_ns,
        run_helper: Callable[..., dict] = ts_bridge.run_helper,
        snippet_max: int = SNIPPET_MAX,
        signature_max: int = SIGNATURE_MAX,
        attribute_tags: Sequence[str] = (),
    ) -> None:
        self.repo_root = repo_root
        self._attribute_tags = sorted(set(attribute_tags))
        self._toolchain = toolchain
        self._clock = clock
        self._run_helper = run_helper
        self._snippet_max = snippet_max
        self._signature_max = signature_max
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._stamp: tuple | None = None
        self._hits = 0
        self._misses = 0
        self._helper_runs = 0
        self._last_helper_ms: float | None = None

    def toolchain(self) -> Toolchain:
        """The current toolchain; raises ``ToolchainUnavailable`` when it is not usable."""
        toolchain = self._toolchain()
        if not toolchain.available:
            raise ToolchainUnavailable(ts_bridge.incomplete_toolchain_problem(toolchain))
        return toolchain

    def facts_for(
        self,
        files: Sequence[SourceFile],
        *,
        root: Path | None = None,
        suffixes: tuple[str, ...] | None = None,
        walked: Sequence[SourceFile] | None = None,
    ) -> dict[str, FileFacts]:
        """Facts for every file, keyed by ``SourceFile.rel``.

        ``walked`` is everything the walk returned (default ``files``), so a
        caller may fetch a subset without dropping the rest. When ``root`` and
        ``suffixes`` describe that walk, cached entries under ``root`` with those
        suffixes that the walk no longer returned (deleted files) are evicted.
        Raises ``ToolchainUnavailable`` or ``HelperError``.
        """
        with self._lock:
            toolchain = self.toolchain()
            stamp = (FACTS_SCHEMA, toolchain.stamp())
            if stamp != self._stamp:
                self._entries.clear()
                self._stamp = stamp
            result: dict[str, FileFacts] = {}
            misses: list[SourceFile] = []
            for source in files:
                facts = self._cached(source)
                if facts is None:
                    misses.append(source)
                else:
                    result[source.rel] = facts
            self._hits += len(files) - len(misses)
            self._misses += len(misses)
            if misses:
                result.update(self._fetch(toolchain, misses))
            if root is not None and suffixes is not None:
                self._evict(root, suffixes, files if walked is None else walked)
            return result

    def _cached(self, source: SourceFile) -> FileFacts | None:
        key = cache_key(source.path)
        entry = self._entries.get(key)
        if entry is None or entry.stamp != source.stamp:
            return None
        if entry.racy:
            try:
                if content_digest(source.path) != entry.content_hash:
                    return None
            except _HASH_ERRORS:
                return None
            # Same bytes, and once the stamp is old enough a later edit must
            # change it, so stat alone decides from now on.
            if not is_racy(source.stamp, self._clock()):
                entry.racy = False
                entry.content_hash = None
        facts = entry.facts
        return facts if facts.rel == source.rel else replace(facts, rel=source.rel)

    def _fetch(self, toolchain: Toolchain, misses: list[SourceFile]) -> dict[str, FileFacts]:
        # The snapshot time precedes the helper's reads, so a file edited during
        # the run is racy relative to it and gets hash-checked next time.
        # Racy files are hashed here, before the helper reads them, rather than
        # by the helper: any later edit can then only mismatch the stored hash
        # and force a re-read, and the facts protocol stays as it is.
        racy_hashes = self._racy_hashes(misses, self._clock())
        started = time.perf_counter()
        response = self._run_helper(
            toolchain,
            "facts",
            {
                "files": [str(source.path) for source in misses],
                "snippet_max": self._snippet_max,
                "signature_max": self._signature_max,
                "attribute_tags": self._attribute_tags,
            },
            timeout_s=ts_bridge.FACTS_TIMEOUT_S,
        )
        self._helper_runs += 1
        self._last_helper_ms = round((time.perf_counter() - started) * 1000, 1)
        parsed = _parse_response(response, misses)
        for source in misses:
            facts = parsed[source.rel]
            racy = source.rel in racy_hashes
            content_hash = racy_hashes.get(source.rel)
            # A read failure can clear without changing the stamp (permissions),
            # and a racy file whose hash could not be taken cannot be validated.
            if facts.status == "read_failed" or (racy and content_hash is None):
                continue
            self._entries[cache_key(source.path)] = _Entry(source.stamp, racy, content_hash, facts)
        return parsed

    @staticmethod
    def _racy_hashes(misses: list[SourceFile], now: int) -> dict[str, str | None]:
        """The content digest of each file too recent to trust by stamp, None when unreadable."""
        out: dict[str, str | None] = {}
        for source in misses:
            if is_racy(source.stamp, now):
                try:
                    out[source.rel] = content_digest(source.path)
                except _HASH_ERRORS:
                    out[source.rel] = None
        return out

    def _evict(self, root: Path, suffixes: tuple[str, ...], seen: Iterable[SourceFile]) -> None:
        prefix = cache_key(root).rstrip("/\\")
        keep = {cache_key(source.path) for source in seen}
        lowered = tuple(cache_key(suffix) for suffix in suffixes)
        for key in list(self._entries):
            under = key.startswith(prefix + "/") or key.startswith(prefix + "\\")
            if under and key.endswith(lowered) and key not in keep:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._stamp = None

    def stats(self) -> dict[str, int | float | None]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "helper_runs": self._helper_runs,
                "last_helper_ms": self._last_helper_ms,
            }


def _parse_response(response: dict, misses: list[SourceFile]) -> dict[str, FileFacts]:
    """The facts of each requested file, checked against the facts protocol."""
    if response.get("facts_schema") != FACTS_SCHEMA:
        raise HelperError(
            "facts",
            "protocol",
            f"response has facts_schema {response.get('facts_schema')!r}, expected {FACTS_SCHEMA}",
        )
    entries = response.get("files")
    if not isinstance(entries, list):
        raise HelperError("facts", "decode output", "response has no 'files' list")
    by_path = {entry.get("path"): entry for entry in entries if isinstance(entry, dict)}
    parsed: dict[str, FileFacts] = {}
    for source in misses:
        entry = by_path.get(str(source.path))
        if entry is None:
            raise HelperError("facts", "protocol", f"response has no entry for {source.rel}")
        try:
            parsed[source.rel] = _file_facts(source.rel, entry)
        except _ENTRY_ERRORS as exc:
            raise HelperError(
                "facts", "decode output", f"{source.rel}: malformed entry ({type(exc).__name__}: {exc})"
            ) from exc
    return parsed
