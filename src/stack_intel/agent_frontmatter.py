"""What every ``agent_*`` module shares: the finding constructor, the file
reader, and the YAML frontmatter after an opening ``---`` line, with each
key's line.

Claude Code reads frontmatter as observed in Claude Code 2.1.328: the block
ends at the first ``---`` anywhere after the opening line (so ``-----``
closes it, and so does a ``---`` inside a value); YAML that does not parse is
retried once with risky top-level values double-quoted (a value already in
matching quotes, or a ``[...]`` that is a list on its own, is left alone) and
leading tabs turned into spaces, and only a failed retry drops the
frontmatter; a repeated key keeps its last value. Other readers end the
block at the next ``---`` line. Without a closing marker, an opening ``---``
followed by no ``key:`` line is a horizontal rule, not frontmatter.

Values are read as YAML 1.2 reads them: only ``true`` and ``false`` are
booleans (YAML 1.1's ``yes``, ``on`` and ``n`` stay strings) and dates stay
strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from stack_intel.doc_report import Finding

KIND = "agent"
STATUS = "invalid"

_BOOL = "tag:yaml.org,2002:bool"
_TIMESTAMP = "tag:yaml.org,2002:timestamp"
_RETRY_LINE = re.compile(r"^([a-zA-Z_-]+):\s+(\S.*)$")
_RISKY = re.compile(r"[{}\[\]*&#!|>%@`]|: ")
_LEADING_TABS = re.compile(r"^\t+", re.MULTILINE)
_NAME_LINE = re.compile(r"^name\s*:", re.MULTILINE)
_KEY_LINE = re.compile(r"^[A-Za-z_][\w.-]*\s*:(?:\s|$)", re.MULTILINE)
_KEY_PREFIX = re.compile(r"^([A-Za-z_][\w.-]*)\s*:")


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regex) for tag, regex in resolvers if tag not in (_BOOL, _TIMESTAMP)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(_BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def finding(rel: str, line: int, text: str, message: str) -> Finding:
    return Finding(rel, line, KIND, STATUS, text, message)


def read_text(repo_root: Path, rel: str, findings: list[Finding]) -> str | None:
    """The file as UTF-8 with bad bytes replaced, as the agents decode it."""
    try:
        return (repo_root / rel).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        findings.append(finding(rel, 1, rel, f"cannot read the file ({type(exc).__name__}: {exc})"))
        return None


@dataclass(frozen=True)
class FrontmatterError:
    """Why present frontmatter does not parse, at its line in the file."""

    line: int
    message: str


@dataclass(frozen=True)
class Frontmatter:
    data: dict[object, object]
    keys: dict[str, yaml.Node]
    values: dict[str, yaml.Node]
    repeats: tuple[tuple[str, int, int], ...]  # (key, line, line of the first occurrence)
    mapping: bool = True  # False for a body that parses as something other than a mapping

    def line(self, key: str) -> int:
        node = self.keys.get(key)
        return 1 if node is None else node.start_mark.line + 2

    def item_line(self, key: str, index: int) -> int:
        node = self.values.get(key)
        if isinstance(node, yaml.SequenceNode) and index < len(node.value):
            return node.value[index].start_mark.line + 2
        return self.line(key)

    def repeat_findings(self, rel: str) -> list[Finding]:
        """Strict-mode findings: a repeated key is valid for the readers, which keep the last value."""
        return [
            finding(rel, line, key, f"frontmatter key {key!r} is repeated (first at line {first}); the last value wins")
            for key, line, first in self.repeats
        ]


_EMPTY = Frontmatter({}, {}, {}, ())
_NOT_A_MAPPING = Frontmatter({}, {}, {}, (), mapping=False)


@dataclass(frozen=True)
class Culprit:
    """The top-level line whose unquoted value made the YAML invalid."""

    key: str
    line: int
    reason: str  # what in the value YAML trips on, such as ``': '``
    value: str


@dataclass(frozen=True)
class FrontmatterRead:
    """``present``: line 1 is ``---``. ``meta`` is the mapping the reader
    gets. ``error``: the frontmatter does not parse, so the reader drops it.
    ``recovered``: it is not valid YAML, but Claude Code's retry reads it
    (``meta`` is what the retry reads); ``as_text`` are the keys whose
    ``[...]`` value the retry turned into a string, and ``culprit`` the line
    that made the YAML invalid, when known. ``unreadable``: this check could
    not parse it (a value the YAML library rejects though the YAML may be
    valid), so nothing about it is known. ``cut``: the line and key whose value
    holds the ``---`` that ended a Claude Code block mid-line."""

    present: bool
    meta: Frontmatter | None = None
    error: FrontmatterError | None = None
    recovered: FrontmatterError | None = None
    as_text: frozenset[str] = frozenset()
    culprit: Culprit | None = None
    unreadable: str | None = None
    has_name: bool = False  # a top-level ``name:`` line, whether or not it parses
    cut: tuple[int, str] | None = None


def _is_list(value: str) -> bool:
    try:
        return isinstance(yaml.safe_load(value), list)
    except Exception:  # noqa: BLE001 - any failure means it is not a list on its own
        return False


def _left_alone(value: str) -> bool:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return True
    return value.startswith("[") and value.endswith("]") and _is_list(value)


def _retry_body(body: str) -> tuple[str, frozenset[str]]:
    """The body as Claude Code's retry rewrites it, and the keys whose
    ``[...]`` value it turned into a string."""
    as_text: set[str] = set()

    def quote(line: str) -> str:
        match = _RETRY_LINE.match(line)
        if match is None:
            return line
        key, value = match.groups()
        if _RISKY.search(value) is None or _left_alone(value):
            return line
        if value.startswith("[") and value.endswith("]"):
            as_text.add(key)
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}: "{escaped}"'

    quoted = "\n".join(quote(line) for line in body.split("\n"))
    return _LEADING_TABS.sub(lambda m: "  " * len(m.group(0)), quoted), frozenset(as_text)


def _culprit(body: str, line: int) -> Culprit | None:
    lines = body.split("\n")
    if not 0 <= line - 2 < len(lines):
        return None
    match = _RETRY_LINE.match(lines[line - 2])
    if match is None or (risky := _RISKY.search(match.group(2))) is None:
        return None
    reason = "': '" if ": " in match.group(2) else repr(risky.group(0))
    return Culprit(match.group(1), line, reason, match.group(2))


class _Unreadable(Exception):
    pass


def _parse(body: str) -> Frontmatter:
    """Raises ``yaml.YAMLError`` for invalid YAML and ``_Unreadable`` when the
    library fails for another reason (an out-of-range date, a huge integer,
    deep nesting)."""
    try:
        root = yaml.compose(body, Loader=_Loader)
        data = yaml.load(body, Loader=_Loader)
    except yaml.YAMLError:
        raise
    except Exception as exc:  # noqa: BLE001 - PyYAML raises ValueError, OverflowError, RecursionError
        raise _Unreadable(f"{type(exc).__name__}: {str(exc)[:80]}") from exc
    if root is None:
        return _EMPTY
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return _NOT_A_MAPPING  # a `---` rule above prose, or a bare scalar: no keys
    keys: dict[str, yaml.Node] = {}
    values: dict[str, yaml.Node] = {}
    first_lines: dict[str, int] = {}
    repeats: list[tuple[str, int, int]] = []
    for key_node, value_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        key = key_node.value
        line = key_node.start_mark.line + 2
        if key in first_lines:
            repeats.append((key, line, first_lines[key]))
        else:
            first_lines[key] = line
        # The loaded mapping keeps a repeated key's last value, so lines point there too.
        keys[key], values[key] = key_node, value_node
    return Frontmatter(data, keys, values, tuple(repeats))


def _error(exc: yaml.YAMLError) -> FrontmatterError:
    mark = getattr(exc, "problem_mark", None)
    reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    return FrontmatterError(1 if mark is None else mark.line + 2, f"invalid YAML frontmatter: {reason}")


def _body(text: str, *, claude: bool) -> tuple[str, bool] | None:
    """The text between the opening ``---`` line and the closing marker, and
    whether the marker sits inside a line; None when unclosed."""
    rest = text.partition("\n")[2]
    if claude:
        end = rest.find("---")
        if end < 0:
            return None
        return rest[:end], end > 0 and rest[end - 1] != "\n"
    lines = rest.split("\n")
    end_line = next((i for i, line in enumerate(lines) if line.rstrip() == "---"), None)
    return None if end_line is None else ("\n".join(lines[:end_line]), False)


def parse_frontmatter(text: str, *, claude: bool, retry: bool | None = None) -> FrontmatterRead:
    """``claude``: end the block as Claude Code does. ``retry`` (default:
    ``claude``): apply Claude Code's retry before calling the YAML broken,
    for readers whose parser is not known to be stricter."""
    retry = claude if retry is None else retry
    if text.partition("\n")[0].rstrip() != "---":
        return FrontmatterRead(present=False)
    found = _body(text, claude=claude)
    if found is None:
        rest = text.partition("\n")[2]
        if _KEY_LINE.search(rest) is None:
            return FrontmatterRead(present=False)
        error = FrontmatterError(1, "YAML frontmatter is not closed by a --- line")
        return FrontmatterRead(True, error=error, has_name=_NAME_LINE.search(rest) is not None)
    body, mid_line = found
    has_name = _NAME_LINE.search(body) is not None
    cut = None
    if mid_line:
        last = body.rsplit("\n", 1)[-1]
        key = _KEY_PREFIX.match(last)
        cut = (body.count("\n") + 2, key.group(1) if key else "")
    try:
        return FrontmatterRead(True, meta=_parse(body), has_name=has_name, cut=cut)
    except _Unreadable as exc:
        return FrontmatterRead(True, unreadable=str(exc), has_name=has_name, cut=cut)
    except yaml.YAMLError as exc:
        error = _error(exc)
    if not retry:
        return FrontmatterRead(True, error=error, has_name=has_name, cut=cut)
    retried, as_text = _retry_body(body)
    try:
        meta = _parse(retried)
    except _Unreadable as exc:
        return FrontmatterRead(True, unreadable=str(exc), has_name=has_name, cut=cut)
    except yaml.YAMLError:
        return FrontmatterRead(True, error=error, has_name=has_name, cut=cut)
    return FrontmatterRead(
        True,
        meta=meta,
        recovered=error,
        as_text=as_text,
        culprit=_culprit(body, error.line),
        has_name=has_name,
        cut=cut,
    )


def strict_parse_findings(rel: str, read: FrontmatterRead) -> list[Finding]:
    """Strict-mode findings for frontmatter that is not clean YAML: invalid
    YAML Claude Code's retry reads, YAML the library cannot load,
    and a body that is not a mapping of keys."""
    if read.recovered is not None:
        message = f"{read.recovered.message}; Claude Code recovers by quoting the value"
        return [finding(rel, read.recovered.line, "frontmatter", message)]
    if read.unreadable is not None:
        return [finding(rel, 1, "frontmatter", f"frontmatter could not be checked ({read.unreadable})")]
    if read.meta is not None and not read.meta.mapping:
        return [finding(rel, 2, "frontmatter", "YAML frontmatter is not a mapping of keys (strict)")]
    return []


def is_blank(value: object) -> bool:
    """Not a string with a non-space character."""
    return not isinstance(value, str) or not value.strip()
