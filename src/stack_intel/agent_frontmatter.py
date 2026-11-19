"""What every ``agent_*`` module shares: the finding constructor, the file
reader, and the YAML frontmatter between an opening ``---`` on line 1 and the
next ``---`` line, with each key's line."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from stack_intel.doc_report import Finding

KIND = "agent"
STATUS = "invalid"

# A plain value starting with `*` (`paths: **/*.ts`, `description: *Use
# proactively*`) is a YAML alias, and one starting with `{` under a glob key
# (`paths: {src,lib}/**`) a flow mapping. Whether the agents' own parsers read
# such frontmatter is unconfirmed, so outside `strict` it is not reported.
_KEY_LINE = re.compile(r"^(\s*)([^\s#'\"\-][^:]*?)(\s*:\s+)(\S.*?)\s*$")
_ITEM_LINE = re.compile(r"^(\s*-\s+)(\S.*?)\s*$")
_GLOB_KEYS = frozenset({"paths", "applyTo"})
_BOOL = "tag:yaml.org,2002:bool"


class _Loader(yaml.SafeLoader):
    """YAML 1.2 booleans: only true and false. YAML 1.1's yes, no, on, off, y
    and n stay strings, as the agents' YAML 1.2 readers read them."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regex) for tag, regex in resolvers if tag != _BOOL]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(_BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def finding(rel: str, line: int, text: str, message: str) -> Finding:
    return Finding(rel, line, KIND, STATUS, text, message)


def read_text(repo_root: Path, rel: str, findings: list[Finding]) -> str | None:
    try:
        return (repo_root / rel).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        findings.append(finding(rel, 1, rel, f"cannot read the file ({type(exc).__name__}: {exc})"))
        return None


@dataclass(frozen=True)
class FrontmatterError:
    """Why present frontmatter cannot be read, at its line in the file."""

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
        return [
            finding(rel, line, key, f"frontmatter key {key!r} is repeated (first at line {first})")
            for key, line, first in self.repeats
        ]


@dataclass(frozen=True)
class FrontmatterRead:
    """``present``: line 1 is ``---``. Present frontmatter has ``meta`` when it
    reads as a mapping and ``error`` when it cannot be read. ``unconfirmed``:
    it reads once unquoted ``*`` values (and ``{`` glob values) are quoted,
    so whether the agents read it is unconfirmed."""

    present: bool
    meta: Frontmatter | None = None
    error: FrontmatterError | None = None
    unconfirmed: bool = False

    def failure(self, *, strict: bool) -> FrontmatterError | None:
        """The error to report: an unconfirmed one only under ``strict``."""
        return None if self.unconfirmed and not strict else self.error


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _split_flow(inner: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, char in enumerate(inner):
        depth += {"{": 1, "[": 1, "}": -1, "]": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
    parts.append(inner[start:])
    return parts


def _quote_value(value: str, glob_key: bool) -> str:
    starts = "*{" if glob_key else "*"
    if value[0] in starts:
        return _quoted(value)
    if value.startswith("[") and value.endswith("]"):
        items = [item.strip() for item in _split_flow(value[1:-1])]
        return "[" + ", ".join(_quoted(item) if item[:1] in starts else item for item in items) + "]"
    return value


def _quote_unconfirmed(body: str) -> str:
    """``body`` with each plain value starting with ``*`` (and with ``{`` under
    ``paths`` or ``applyTo``) quoted, in block and flow lists too."""
    out: list[str] = []
    glob_key = False
    for line in body.split("\n"):
        if match := _KEY_LINE.match(line):
            indent, key, colon, value = match.groups()
            glob_key = key in _GLOB_KEYS
            line = indent + key + colon + _quote_value(value, glob_key)
        elif match := _ITEM_LINE.match(line):
            line = match.group(1) + _quote_value(match.group(2), glob_key)
        elif (bare := line.rstrip()).endswith(":") and not bare.lstrip().startswith(("#", "-")):
            glob_key = bare.strip()[:-1].strip() in _GLOB_KEYS
        out.append(line)
    return "\n".join(out)


def _reads(body: str) -> bool:
    try:
        yaml.load(body, Loader=_Loader)
    except yaml.YAMLError:
        return False
    return True


def parse_frontmatter(text: str) -> FrontmatterRead:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return FrontmatterRead(present=False)
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        return FrontmatterRead(True, error=FrontmatterError(1, "YAML frontmatter is not closed by a --- line"))
    body = "\n".join(lines[1:end])
    try:
        root = yaml.compose(body, Loader=_Loader)
        data = yaml.load(body, Loader=_Loader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        line = 1 if mark is None else mark.line + 2
        error = FrontmatterError(line, f"invalid YAML frontmatter: {reason}")
        return FrontmatterRead(True, error=error, unconfirmed=_reads(_quote_unconfirmed(body)))
    if root is None:
        return FrontmatterRead(True, meta=Frontmatter({}, {}, {}, ()))
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return FrontmatterRead(True, error=FrontmatterError(1, "YAML frontmatter must be a mapping"))
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
    return FrontmatterRead(True, meta=Frontmatter(data, keys, values, tuple(repeats)))


def is_blank(value: object) -> bool:
    """Not a string with a non-space character."""
    return not isinstance(value, str) or not value.strip()
