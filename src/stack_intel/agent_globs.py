"""Whether an agent file's glob matches a git-visible path under any reading.

"Matches no git-visible file" is a claim about every reader, so a glob is
tried under the most lenient matching a known reader uses:

- case-insensitive (Claude Code and VS Code);
- ``**/`` prepended unless the pattern starts with ``/`` or ``**/`` (VS Code;
  for Claude Code a pattern without ``/`` matches at any depth, which this
  covers);
- a trailing ``/**`` also matches the directory itself (Claude Code strips it);
- ``./`` stripped, brace options trimmed (``{ js, ts }``);
- ``[...]`` tried both as a class and literally (Next.js ``[id]`` directories);
- dot files match.

Constructs whose reading is not modelled are ``unmodelled`` and skip the
check: negation, extglobs, backslash escapes, POSIX classes, brace ranges, a
comma inside braces or brackets of a comma-split value, and globs past the
work bound (long patterns, many wildcards), so the regex never backtracks
for long.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Collection
from typing import Literal

Verdict = Literal["matches", "no_match", "unmodelled"]

_BRACES = re.compile(r"\{([^{}]*)\}")
_EXTGLOB = re.compile(r"[@!+*?]\(")
_REPEATED_GLOBSTAR = re.compile(r"(?:\*\*/)+")
_MAX_EXPANSIONS = 64
_MAX_LENGTH = 256
_MAX_GLOBSTARS = 4
_MAX_STARS_PER_SEGMENT = 4


def split_globs(value: str) -> list[str]:
    """A comma-separated glob string as its non-empty globs; a comma inside
    braces or brackets does not split."""
    parts, depth, start, in_class = [], 0, 0, False
    for i, char in enumerate(value):
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = "]" in value[i + 1 :]
        elif char in "{}":
            depth += 1 if char == "{" else -1
        elif char == "," and depth <= 0:
            parts.append(value[start:i])
            start = i + 1
    parts.append(value[start:])
    return [part.strip() for part in parts if part.strip()]


GlobGroup = tuple[str, tuple[tuple[str, bool], ...]]


def _split(value: str) -> tuple[tuple[str, bool], ...]:
    """``(glob, unmodelled)`` per comma-split piece; a piece holding a comma
    of a split that braces or brackets prevented is unmodelled, since how a
    reader splits it is unknown."""
    globs = split_globs(value)
    nested_comma = len(globs) < len([part for part in value.split(",") if part.strip()])
    return tuple((glob, nested_comma and "," in glob) for glob in globs)


def _strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return [value] if isinstance(value, str) else []


def glob_groups(value: object) -> list[GlobGroup]:
    """A ``paths`` or ``applyTo`` value as ``(text, pieces)`` groups, each
    reported as one glob. A string value is comma-split into a group per
    glob, as the readers document. In a list (flattened recursively,
    non-strings dropped) each string is one group, comma-split only as the
    most lenient reading, so it matches when any piece does."""
    if isinstance(value, str):
        return [(glob, ((glob, unmodelled),)) for glob, unmodelled in _split(value)]
    return [(text.strip(), pieces) for text in _strings(value) if (pieces := _split(text))]


def _unmodelled(glob: str) -> bool:
    collapsed = _REPEATED_GLOBSTAR.sub("**/", glob)
    return (
        glob.startswith("!")
        or "\\" in glob
        or _EXTGLOB.search(glob) is not None
        or "[[:" in glob
        or any(".." in match.group(1) for match in _BRACES.finditer(glob))
        or len(glob) > _MAX_LENGTH
        or collapsed.count("**") > _MAX_GLOBSTARS
        or any(segment.count("*") > _MAX_STARS_PER_SEGMENT for segment in collapsed.split("/") if segment != "**")
    )


def _expand_braces(pattern: str) -> list[str] | None:
    """``a/*.{ts, tsx}`` as ``a/*.ts`` and ``a/*.tsx`` (options trimmed),
    innermost braces first; a single-option brace is kept literally too.
    None past the budget."""
    out: list[str] = []
    pending = [pattern]
    while pending:
        current = pending.pop()
        match = _BRACES.search(current)
        if match is None:
            out.append(current)
        else:
            head, tail = current[: match.start()], current[match.end() :]
            options = [option.strip() for option in match.group(1).split(",")]
            if len(options) == 1:
                pending.append(head + "\0" + match.group(1) + "\1" + tail)
            pending.extend(head + option + tail for option in options)
        if len(out) + len(pending) > _MAX_EXPANSIONS:
            return None
    return [p.replace("\0", "{").replace("\1", "}") for p in out]


def _class_end(pattern: str, start: int) -> int | None:
    i = start + 1
    if i < len(pattern) and pattern[i] in "!^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    end = pattern.find("]", i)
    return None if end < 0 else end


def _class_members(body: str) -> str:
    """Class members escaped for ``re``, keeping ``-`` as the range operator."""
    return "".join(c if c == "-" else re.escape(c) for c in body)


@functools.lru_cache(maxsize=4096)
def _regex(pattern: str, classes: bool) -> re.Pattern[str]:
    """Raises ``re.error`` for a class ``re`` cannot compile (``[z-a]``)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and classes and (end := _class_end(pattern, i)) is not None:
            body = pattern[i + 1 : end]
            negate = body[:1] in ("!", "^")
            members = _class_members(body[1:] if negate else body)
            out.append(f"[^{members}/]" if negate else f"[{members}]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def _readings(alternative: str) -> list[str]:
    bare = _REPEATED_GLOBSTAR.sub("**/", alternative.strip())
    while bare.startswith("./"):
        bare = bare[2:]
    if bare.startswith("/"):
        forms = [bare.lstrip("/")]
    else:
        forms = [bare] if bare.startswith("**/") else [bare, "**/" + bare]
    out: list[str] = []
    for form in forms:
        out.append(form)
        if form.endswith("/**"):
            out.append(form[:-3])
        elif form.endswith("/"):
            out += [form + "**", form.rstrip("/")]
    return [form for form in out if form]


def glob_verdict(glob: str, paths: Collection[str]) -> Verdict:
    """``glob`` against the git-visible files and directories, under every reading."""
    if _unmodelled(glob):
        return "unmodelled"
    expanded = _expand_braces(glob)
    if expanded is None:
        return "unmodelled"
    regexes: list[re.Pattern[str]] = []
    for form in dict.fromkeys(reading for alternative in expanded for reading in _readings(alternative)):
        for classes in (True, False):
            try:
                regexes.append(_regex(form, classes))
            except re.error:
                continue
    return "matches" if any(regex.fullmatch(rel) for rel in paths for regex in regexes) else "no_match"


_GLOB_CHARS = re.compile(r"[*?\[{\\!]")
_CLASS = re.compile(r"\[([^\]]+)\]")


def literal_directory(glob: str) -> str:
    """The directories ``glob`` names before its first glob character
    (``src/gen`` for ``/src/gen/**/*.ts``); empty when there are none."""
    parts = glob.removeprefix("./").lstrip("/").rstrip("/").split("/")[:-1]
    literal: list[str] = []
    for part in parts:
        if _GLOB_CHARS.search(part) or part in ("", ".", ".."):
            break
        literal.append(part)
    return "/".join(literal)


def sample_paths(glob: str, limit: int = 16) -> list[str]:
    """Concrete paths ``glob`` would match (``**`` and ``**/`` dropped, ``*``
    as nothing and as ``x``, ``?`` as ``x``, a class as its first member), for
    asking git whether such files are ignored."""
    samples: list[str] = []
    for alt in (_expand_braces(glob) or [glob])[:limit]:
        bare = alt.removeprefix("./").lstrip("/")
        bare = _CLASS.sub(lambda m: "x" if m.group(1)[0] in "!^" else m.group(1)[0], bare)
        bare = bare.replace("**/", "").replace("**", "x").replace("?", "x")
        for star in ("", "x"):
            path = "/".join(part for part in bare.replace("*", star).split("/") if part)
            if path and path not in samples:
                samples.append(path)
    return samples
