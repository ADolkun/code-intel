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

# An unquoted glob as the value of a glob key (`paths: **/*.ts`, `applyTo: {a,b}/**`)
# is a YAML alias or flow mapping. Whether the agents' own parsers read such
# frontmatter is unconfirmed, so outside `strict` it is not reported as broken.
_GLOB_KEY_VALUE = re.compile(r"^((?:paths|applyTo)\s*:\s+)([*{].*?)\s*$")
_GLOB_ITEM = re.compile(r"^(\s*-\s+)([*{].*?)\s*$")
_TOP_LEVEL_KEY = re.compile(r"^[^\s#-][^:]*:")
_GLOB_KEY = re.compile(r"^(?:paths|applyTo)\s*:")
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
    reads as a mapping and ``error`` when it cannot be read. ``glob_quirk``:
    it reads once the unquoted glob values of ``paths`` and ``applyTo`` are
    quoted, so whether the agents read it is unconfirmed."""

    present: bool
    meta: Frontmatter | None = None
    error: FrontmatterError | None = None
    glob_quirk: bool = False

    def failure(self, *, strict: bool) -> FrontmatterError | None:
        """The error to report: a glob quirk only under ``strict``."""
        return None if self.glob_quirk and not strict else self.error


def _quote_glob_values(body: str) -> str:
    def quote(match: re.Match[str]) -> str:
        return match.group(1) + "'" + match.group(2).replace("'", "''") + "'"

    out: list[str] = []
    in_glob_key = False
    for line in body.split("\n"):
        if _TOP_LEVEL_KEY.match(line):
            in_glob_key = _GLOB_KEY.match(line) is not None
            line = _GLOB_KEY_VALUE.sub(quote, line)
        elif in_glob_key:
            line = _GLOB_ITEM.sub(quote, line)
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
        return FrontmatterRead(True, error=error, glob_quirk=_reads(_quote_glob_values(body)))
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
