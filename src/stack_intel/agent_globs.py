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
work bound (longer than 256 characters, more than 64 brace expansions).
Matching never backtracks for long (``_Matcher``).
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
    return (
        glob.startswith("!")
        or "\\" in glob
        or _EXTGLOB.search(glob) is not None
        or "[[:" in glob
        or any(".." in match.group(1) for match in _BRACES.finditer(glob))
        or len(glob) > _MAX_LENGTH
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


_Token = tuple[str, str]  # (kind, a regex for one character when kind is "one")
_State = tuple[int, bool]  # (token index, inside a ``**/`` that has consumed characters)
_Pending = frozenset[_State]
_FLAGS = re.IGNORECASE | re.DOTALL
_Engine = Literal["auto", "regex", "automaton"]
_WILDCARDS = {"star": "[^/]*", "gstar": ".*", "gslash": "(?:.*/)?"}


def _class_regex(body: str) -> str:
    """A ``[...]`` body as a regex for one character other than ``/``,
    bounds kept in their case; raises ``re.error`` for a reversed range
    (``[z-a]``), which the readers' matchers reject."""
    negate = body[:1] in ("!", "^")
    members = body[1:] if negate else body
    out: list[str] = []
    i = 0
    while i < len(members):
        if i + 2 < len(members) and members[i + 1] == "-":
            low, high = members[i], members[i + 2]
            if low > high:
                raise re.error(f"bad character range {low}-{high}")
            out.append(f"{re.escape(low)}-{re.escape(high)}")
            i += 3
        else:
            out.append(re.escape(members[i]))
            i += 1
    return f"[^{''.join(out)}/]" if negate else f"(?!/)[{''.join(out)}]"


class _Matcher:
    """A glob matched case-insensitively, one character at a time as ``re``
    folds case (so a path is never lowered and lengths always line up).

    With at most two unbounded wildcards the glob runs as one regex, which
    backtracks at most quadratically. With more it runs as an NFA over the
    path once, each set of states' move on a character memoized, its
    single-character tests the same regexes: linear, never backtracking."""

    def __init__(self, tokens: list[_Token], engine: _Engine) -> None:
        self.tokens = tokens
        wildcards = sum(kind in _WILDCARDS for kind, _ in tokens)
        source = "".join(_WILDCARDS.get(kind, one) for kind, one in tokens)
        use_regex = engine == "regex" or (engine == "auto" and wildcards <= 2)
        self.regex = re.compile(source, _FLAGS) if use_regex else None
        self.ones = [re.compile(one, _FLAGS) if kind == "one" else None for kind, one in tokens]
        self.end: _State = (len(tokens), False)
        self.moves: dict[tuple[_Pending, str], _Pending] = {}
        self.start = self._closure({(0, False)})

    def _closure(self, states: set[_State]) -> _Pending:
        pending = list(states)
        while pending:
            index, inside = pending.pop()
            if not inside and index < len(self.tokens) and self.tokens[index][0] in _WILDCARDS:
                skipped = (index + 1, False)
                if skipped not in states:
                    states.add(skipped)
                    pending.append(skipped)
        return frozenset(states)

    def _move(self, states: _Pending, char: str) -> _Pending:
        out: set[_State] = set()
        for index, _inside in states:
            if index == len(self.tokens):
                continue
            kind = self.tokens[index][0]
            one = self.ones[index]
            if kind == "gslash":
                out.add((index, True))
                if char == "/":
                    out.add((index + 1, False))
            elif kind == "gstar" or (kind == "star" and char != "/"):
                out.add((index, False))
            elif one is not None and one.fullmatch(char) is not None:
                out.add((index + 1, False))
        return self._closure(out)

    def matches(self, path: str) -> bool:
        if self.regex is not None:
            return self.regex.fullmatch(path) is not None
        states = self.start
        for char in path:
            key = (states, char)
            moved = self.moves.get(key)
            if moved is None:
                moved = self.moves[key] = self._move(states, char)
            if not moved:
                return False
            states = moved
        return self.end in states


@functools.lru_cache(maxsize=4096)
def _matcher(pattern: str, classes: bool, engine: _Engine = "auto") -> _Matcher:
    """Raises ``re.error`` for a class that cannot compile (``[z-a]``).
    ``engine`` forces one engine, for testing them against each other."""
    tokens: list[_Token] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            tokens.append(("gslash", ""))
            i += 3
        elif pattern.startswith("**", i):
            tokens.append(("gstar", ""))
            i += 2
        elif pattern[i] == "*":
            tokens.append(("star", ""))
            i += 1
        elif pattern[i] == "?":
            tokens.append(("one", "[^/]"))
            i += 1
        elif pattern[i] == "[" and classes and (end := _class_end(pattern, i)) is not None:
            tokens.append(("one", _class_regex(pattern[i + 1 : end])))
            i = end + 1
        else:
            tokens.append(("one", re.escape(pattern[i])))
            i += 1
    return _Matcher(tokens, engine)


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
    matchers: list[_Matcher] = []
    for form in dict.fromkeys(reading for alternative in expanded for reading in _readings(alternative)):
        for classes in (True, False):
            try:
                matchers.append(_matcher(form, classes))
            except re.error:
                continue
    return "matches" if any(matcher.matches(rel) for matcher in matchers for rel in paths) else "no_match"


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
