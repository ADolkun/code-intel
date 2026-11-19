"""Whether an agent file's glob matches a git-visible file, as its readers match it.

Claude Code (``paths`` in ``.claude/rules``): relative to the repository root,
a leading ``/`` anchors, a pattern with no other ``/`` matches at any depth,
``*`` stays within a segment, ``**`` crosses segments, ``?`` is one character,
``{a,b}`` expands and ``[...]`` is a character class; a ``[`` with no closing
``]`` makes the pattern invalid.

Copilot (``applyTo``): relative to the repository root, ``*``, ``**`` and
``?`` as above, and a string is split on commas.

A glob matches no file only when it matches none under every reading its
readers may use: ``[`` is tried both as a class and as a literal (Next.js
``[id]`` directories), and dot files match. Constructs whose reading is not
modelled (negation, extglobs, escapes, POSIX classes, brace ranges, and braces
in a comma-split string) are ``UNMODELLED``: their existence check is skipped.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Collection
from typing import Literal

Verdict = Literal["matches", "no_match", "invalid", "unmodelled"]

_BRACES = re.compile(r"\{([^{}]*)\}")
_EXTGLOB = re.compile(r"[@!+*?]\(")
_MAX_EXPANSIONS = 1000


def split_globs(value: str) -> list[str]:
    """A comma-separated glob string as its non-empty globs; a comma inside
    braces does not split."""
    parts, depth, start = [], 0, 0
    for i, char in enumerate(value):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if char == "," and depth <= 0:
            parts.append(value[start:i])
            start = i + 1
    parts.append(value[start:])
    return [part.strip() for part in parts if part.strip()]


def _unmodelled(glob: str, *, split: bool) -> bool:
    return (
        glob.startswith("!")
        or "\\" in glob
        or _EXTGLOB.search(glob) is not None
        or "[[:" in glob
        or any(".." in match.group(1) for match in _BRACES.finditer(glob))
        or (split and "{" in glob)
    )


def has_unclosed_bracket(glob: str) -> bool:
    """A ``[`` that no ``]`` closes (a ``]`` right after ``[`` or ``[!`` is a member)."""
    for start, char in enumerate(glob):
        if char != "[":
            continue
        i = start + 1
        if i < len(glob) and glob[i] in "!^":
            i += 1
        if i < len(glob) and glob[i] == "]":
            i += 1
        if "]" not in glob[i:]:
            return True
    return False


def _expand_braces(pattern: str) -> list[str] | None:
    """``a/*.{ts,tsx}`` as ``a/*.ts`` and ``a/*.tsx``, innermost braces first;
    a single-option brace is kept literally too. None past the budget."""
    out: list[str] = []
    pending = [pattern]
    while pending:
        current = pending.pop()
        match = _BRACES.search(current)
        if match is None:
            out.append(current)
        else:
            head, tail = current[: match.start()], current[match.end() :]
            options = match.group(1).split(",")
            if len(options) == 1:
                pending.append(head + "\0" + options[0] + "\1" + tail)
            pending.extend(head + option + tail for option in options)
        if len(out) + len(pending) > _MAX_EXPANSIONS:
            return None
    return [p.replace("\0", "{").replace("\1", "}") for p in out]


@functools.lru_cache(maxsize=4096)
def _regex(pattern: str, classes: bool) -> re.Pattern[str]:
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
            members = body[1:] if negate else body
            members = members.replace("[", "\\[")
            out.append(f"[^{members}/]" if negate else f"[{members}]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _class_end(pattern: str, start: int) -> int | None:
    i = start + 1
    if i < len(pattern) and pattern[i] in "!^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    end = pattern.find("]", i)
    return None if end < 0 else end


def _any_match(alternatives: list[str], files: Collection[str]) -> bool:
    regexes = [_regex(alt, classes) for alt in alternatives for classes in (True, False)]
    return any(regex.fullmatch(rel) is not None for rel in files for regex in regexes)


def _claude_form(alternative: str) -> str:
    if alternative.startswith("/"):
        return alternative.lstrip("/")
    alternative = alternative.removeprefix("./")
    return alternative if "/" in alternative.rstrip("/") else "**/" + alternative


def claude_glob(glob: str, files: Collection[str], *, split: bool = False) -> Verdict:
    """``glob`` from a ``.claude/rules`` ``paths`` value; ``split`` when it came
    from a comma-separated string."""
    if has_unclosed_bracket(glob):
        return "invalid"
    if _unmodelled(glob, split=split):
        return "unmodelled"
    expanded = _expand_braces(glob)
    if expanded is None:
        return "unmodelled"
    alternatives = [_claude_form(alt) + ("**" if alt.endswith("/") else "") for alt in expanded]
    return "matches" if _any_match(alternatives, files) else "no_match"


def copilot_glob(glob: str, files: Collection[str], *, split: bool = False) -> Verdict:
    """``glob`` from an ``applyTo`` value; ``split`` when it came from a string."""
    if _unmodelled(glob, split=split):
        return "unmodelled"
    expanded = _expand_braces(glob)
    if expanded is None:
        return "unmodelled"
    alternatives: list[str] = []
    for alt in expanded:
        bare = alt.removeprefix("./").lstrip("/")
        alternatives.append(bare + ("**" if bare.endswith("/") else ""))
    return "matches" if _any_match(alternatives, files) else "no_match"
