"""What every ``agent_*`` module shares: the finding constructor, the file
reader, and the YAML frontmatter after an opening ``---`` line, with each
key's line.

Claude Code reads frontmatter as observed in Claude Code 2.1.328: the block
ends at the first ``---`` anywhere after the opening line (so ``-----``
closes it); YAML that does not parse is retried once with risky top-level
values double-quoted and leading tabs turned into spaces, and only a failed
retry drops the frontmatter; a repeated key keeps its last value. Other
readers end the block at the next ``---`` line.

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


@dataclass(frozen=True)
class FrontmatterRead:
    """``present``: line 1 is ``---``. ``meta`` is the mapping the reader
    gets. ``error``: the frontmatter does not parse, so the reader drops it.
    ``recovered``: it is not valid YAML, but Claude Code's retry reads it
    (``meta`` is what the retry reads). ``unreadable``: this check could not
    parse it (a value the YAML library rejects though the YAML may be valid),
    so nothing about it is known."""

    present: bool
    meta: Frontmatter | None = None
    error: FrontmatterError | None = None
    recovered: FrontmatterError | None = None
    unreadable: str | None = None
    has_name: bool = False  # a top-level ``name:`` line, whether or not it parses


def _retry_body(body: str) -> str:
    def quote(line: str) -> str:
        match = _RETRY_LINE.match(line)
        if match is None or _RISKY.search(match.group(2)) is None:
            return line
        value = match.group(2).replace("\\", "\\\\").replace('"', '\\"')
        return f'{match.group(1)}: "{value}"'

    return _LEADING_TABS.sub(lambda m: "  " * len(m.group(0)), "\n".join(quote(line) for line in body.split("\n")))


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
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return _EMPTY  # a `---` rule above prose, or a bare scalar: no keys
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


def _body(text: str, *, claude: bool) -> str | None:
    """The text between the opening ``---`` line and the closing marker; None when unclosed."""
    first, newline, rest = text.partition("\n")
    if claude:
        end = rest.find("---")
        return None if end < 0 else rest[:end]
    lines = rest.split("\n")
    end = next((i for i, line in enumerate(lines) if line.rstrip() == "---"), None)
    return None if end is None else "\n".join(lines[:end])


def parse_frontmatter(text: str, *, claude: bool, retry: bool | None = None) -> FrontmatterRead:
    """``claude``: end the block as Claude Code does. ``retry`` (default:
    ``claude``): apply Claude Code's retry before calling the YAML broken,
    for readers whose parser is not known to be stricter."""
    retry = claude if retry is None else retry
    if text.partition("\n")[0].rstrip() != "---":
        return FrontmatterRead(present=False)
    body = _body(text, claude=claude)
    has_name = _NAME_LINE.search(text if body is None else body) is not None
    if body is None:
        error = FrontmatterError(1, "YAML frontmatter is not closed by a --- line")
        return FrontmatterRead(True, error=error, has_name=has_name)
    try:
        return FrontmatterRead(True, meta=_parse(body), has_name=has_name)
    except _Unreadable as exc:
        return FrontmatterRead(True, unreadable=str(exc), has_name=has_name)
    except yaml.YAMLError as exc:
        error = _error(exc)
    if not retry:
        return FrontmatterRead(True, error=error, has_name=has_name)
    try:
        return FrontmatterRead(True, meta=_parse(_retry_body(body)), recovered=error, has_name=has_name)
    except _Unreadable, yaml.YAMLError:
        return FrontmatterRead(True, error=error, has_name=has_name)


def strict_parse_findings(rel: str, read: FrontmatterRead) -> list[Finding]:
    """Strict-mode findings for frontmatter the readers accept but that is not clean YAML."""
    if read.recovered is not None:
        message = f"{read.recovered.message}; Claude Code recovers by quoting the value"
        return [finding(rel, read.recovered.line, "frontmatter", message)]
    if read.unreadable is not None:
        return [finding(rel, 1, "frontmatter", f"frontmatter could not be checked ({read.unreadable})")]
    return []


def is_blank(value: object) -> bool:
    """Not a string with a non-space character."""
    return not isinstance(value, str) or not value.strip()
