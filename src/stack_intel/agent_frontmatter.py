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

# A value the YAML grammar reads as an alias, a tag, a flow mapping or a
# reserved indicator, as an unquoted glob is (`paths: **/*.ts`). Whether the
# agents' own parsers read such frontmatter is unconfirmed, so it is not
# reported as broken.
_GLOB_LIKE_VALUE = re.compile(r"^(\s*(?:-\s+|[^\s#'\"-][^:]*:\s+))([*{!@%`].*?)\s*$")


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
    reads as a mapping, ``error`` when it cannot be read, and neither when
    only a glob-like unquoted value stops it from parsing (unconfirmed)."""

    present: bool
    meta: Frontmatter | None = None
    error: FrontmatterError | None = None


def _compose(body: str) -> yaml.Node | None:
    return yaml.compose(body, Loader=yaml.SafeLoader)


def _quote_glob_like(body: str) -> str:
    def quote(match: re.Match[str]) -> str:
        return match.group(1) + "'" + match.group(2).replace("'", "''") + "'"

    return "\n".join(_GLOB_LIKE_VALUE.sub(quote, line) for line in body.split("\n"))


def parse_frontmatter(text: str) -> FrontmatterRead:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return FrontmatterRead(present=False)
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        return FrontmatterRead(True, error=FrontmatterError(1, "YAML frontmatter is not closed by a --- line"))
    body = "\n".join(lines[1:end])
    try:
        root = _compose(body)
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        try:
            yaml.safe_load(_quote_glob_like(body))
        except yaml.YAMLError:
            mark = getattr(exc, "problem_mark", None)
            reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
            line = 1 if mark is None else mark.line + 2
            return FrontmatterRead(True, error=FrontmatterError(line, f"invalid YAML frontmatter: {reason}"))
        return FrontmatterRead(present=True)
    if root is None:
        return FrontmatterRead(True, meta=Frontmatter({}, {}, {}, ()))
    if not isinstance(root, yaml.MappingNode) or not isinstance(data, dict):
        return FrontmatterRead(True, error=FrontmatterError(1, "YAML frontmatter must be a mapping"))
    keys: dict[str, yaml.Node] = {}
    values: dict[str, yaml.Node] = {}
    repeats: list[tuple[str, int, int]] = []
    for key_node, value_node in root.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        key = key_node.value
        if key in keys:
            repeats.append((key, key_node.start_mark.line + 2, keys[key].start_mark.line + 2))
        else:
            keys[key], values[key] = key_node, value_node
    return FrontmatterRead(True, meta=Frontmatter(data, keys, values, tuple(repeats)))


def is_blank(value: object) -> bool:
    """Not a string with a non-space character."""
    return not isinstance(value, str) or not value.strip()
