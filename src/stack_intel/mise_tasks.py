"""The tasks mise config files and task directories define, for the docs
check's mise references.

Which files count (from mise 2026.x): in every directory from the repository
root down to the working directory, the config files ``config_owner`` knows
(every ``mise.<env>.toml`` included, a lenient union over environments), the
file tasks under the default task directories, and the TOML task files and
task directories a config's ``[task_config] includes`` names. Files outside
the repository and environment variables do not count: the docs check judges
a fresh checkout on a clean machine.

A remote or templated include, ``[task_templates]`` and a task that
``extends`` another make a file's task set unprovable (``MiseFile.opaque``).
"""

from __future__ import annotations

import posixpath
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from stack_intel.doc_rules import STAGE
from stack_intel.problems import Problem, diagnostic

_CONFIG_RE = re.compile(
    r"(?:^|/)("
    r"\.?mise(?:\.[^/]+)?\.toml"
    r"|\.config/mise(?:\.[^/]+)?\.toml"
    r"|\.config/mise/mise\.toml"
    r"|(?:\.?mise|\.config/mise)/config(?:\.[^/]+)?\.toml"
    r"|(?:\.?mise|\.config/mise)/conf\.d/[^/]+\.toml"
    r")$"
)
TASK_DIRS: tuple[str, ...] = ("mise-tasks", ".mise-tasks", "mise/tasks", ".mise/tasks", ".config/mise/tasks")
_TASK_DIR_RE = re.compile(r"(?:^|/)(" + "|".join(re.escape(d) for d in TASK_DIRS) + r")/(.+)$")
_REMOTE_PREFIXES = ("git::", "oci::", "zip::", "http://", "https://")
_HEADER_RE = re.compile(r"^\s*(?:#|//|::)\s*(?:MISE|\[MISE\])(?:\s+(.*))?$")
_NO_ALIASES: Mapping[str, str] = MappingProxyType({})


def config_owner(rel: str) -> str | None:
    """The directory a mise config file ``rel`` configures (``""`` for the
    repository root), None when ``rel`` is not a config file name."""
    match = _CONFIG_RE.search(rel)
    if match is None:
        return None
    return rel[: match.start(1)].rstrip("/")


def task_dir_entry(rel: str) -> tuple[str, str] | None:
    """``(owning directory, path inside the task directory)`` of a file under
    a default task directory, None for any other file."""
    match = _TASK_DIR_RE.search(rel)
    if match is None:
        return None
    return rel[: match.start(1)].rstrip("/"), match.group(2)


def file_task_names(path: str) -> frozenset[str]:
    """The task names a file task at ``path`` (inside its task directory)
    answers to: ``a/b/c.sh`` is ``a:b:c.sh`` and ``a:b:c``, ``a/_default`` is ``a``."""
    parts = path.split("/")
    stem = posixpath.splitext(parts[-1])[0]
    names = {":".join(parts), ":".join([*parts[:-1], stem])}
    if stem == "_default" and len(parts) > 1:
        names.add(":".join(parts[:-1]))
    return frozenset(name for name in names if name)


@dataclass(frozen=True)
class MiseFile:
    """``tasks``: the task names and aliases the file defines. ``includes``:
    its local ``[task_config] includes`` entries. ``opaque``: why its task set
    is not provable, None when it is. ``path_aliases``: ``[monorepo.path_aliases]``."""

    tasks: frozenset[str]
    includes: tuple[str, ...] = ()
    opaque: str | None = None
    path_aliases: Mapping[str, str] = field(default=_NO_ALIASES)


def _task_names(table: Mapping[str, object], where: str) -> tuple[set[str], str | None]:
    names: set[str] = set()
    opaque = None
    for name, value in table.items():
        names.add(name)
        if not isinstance(value, dict):
            continue
        alias = value.get("alias")
        if isinstance(alias, str):
            names.add(alias)
        elif isinstance(alias, list):
            names.update(item for item in alias if isinstance(item, str))
        if "extends" in value and opaque is None:
            opaque = f"task {name!r} in {where} extends another task"
    return names, opaque


def _includes(data: Mapping[str, object], where: str) -> tuple[tuple[str, ...], str | None]:
    task_config = data.get("task_config")
    raw = task_config.get("includes") if isinstance(task_config, dict) else None
    if not isinstance(raw, list):
        return (), None
    local: list[str] = []
    opaque = None
    for entry in raw:
        if not isinstance(entry, str):
            continue
        if entry.startswith(_REMOTE_PREFIXES):
            opaque = opaque or f"{where} includes the remote tasks {entry!r}"
        elif "{{" in entry or entry.startswith(("/", "~")):
            opaque = opaque or f"{where} includes {entry!r}, outside what the docs check reads"
        else:
            local.append(entry)
    return tuple(local), opaque


def read_mise_file(data: Mapping[str, object], rel: str, *, task_file: bool) -> MiseFile:
    """A parsed config file, or (``task_file``) a TOML task file, whose
    top-level keys are its tasks."""
    if task_file:
        names, opaque = _task_names(data, rel)
        return MiseFile(frozenset(names), opaque=opaque)
    tasks = data.get("tasks")
    names, opaque = _task_names(tasks, rel) if isinstance(tasks, dict) else (set(), None)
    if "task_templates" in data:
        opaque = opaque or f"{rel} defines task templates"
    includes, include_opaque = _includes(data, rel)
    monorepo = data.get("monorepo")
    aliases = monorepo.get("path_aliases") if isinstance(monorepo, dict) else None
    path_aliases = (
        {key: value for key, value in aliases.items() if isinstance(value, str)} if isinstance(aliases, dict) else {}
    )
    return MiseFile(
        frozenset(names),
        includes,
        opaque or include_opaque,
        MappingProxyType(path_aliases) if path_aliases else _NO_ALIASES,
    )


def file_task_header(rel: str, text: str) -> MiseFile:
    """The aliases a file task's ``#MISE`` header lines declare. A header that
    is not TOML, or one that ``extends`` another task, leaves them unknown."""
    lines = [match.group(1) or "" for line in text.splitlines() if (match := _HEADER_RE.match(line))]
    if not lines:
        return MiseFile(frozenset())
    try:
        header = tomllib.loads("\n".join(lines))
    except tomllib.TOMLDecodeError:
        return MiseFile(frozenset(), opaque=f"the #MISE header of {rel} is not TOML the docs check can read")
    names, opaque = _task_names({"": header}, rel)
    return MiseFile(frozenset(names - {""}), opaque=opaque and f"file task {rel} extends another task")


def mise_file_from_bytes(rel: str, data: bytes, *, stage: str = STAGE) -> tuple[MiseFile | None, list[Problem]]:
    """``rel`` read as a config file when its name is one, as a TOML task file
    when it ends ``.toml``, else as a file task (its header); None with a
    diagnostic when a TOML file is not UTF-8 or not TOML. A file task may be any
    script or binary, so its bytes are read leniently and never fail."""
    if config_owner(rel) is None and not rel.endswith(".toml"):
        return file_task_header(rel, data.decode("utf-8", "replace")), []
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return None, [diagnostic("decode_failed", stage=stage, step="decode file", file=rel, exc=exc)]
    except tomllib.TOMLDecodeError as exc:
        return None, [diagnostic("parse_failed", stage=stage, step="parse file", file=rel, exc=exc)]
    return read_mise_file(parsed, rel, task_file=config_owner(rel) is None), []
