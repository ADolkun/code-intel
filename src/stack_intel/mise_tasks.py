"""The tasks mise config files and task directories define, for the docs
check's mise references.

Which files count (checked against mise 2026.11): in each directory, the
config files ``config_file`` knows, the file tasks under the default task
directories (unless a config of that directory declares ``[task_config]
includes``, which replaces them), and the TOML task files and task directories
those includes name. An environment config (``mise.<env>.toml``) counts only
for a command that selects the environment (``MISE_ENV`` or ``-E``). Files
outside the repository and the environment do not count: the docs check judges
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

from stack_intel.definition_files import Unreadable, unreadable

# For a prefix P: P.toml and P.local.toml always, P.<env>.toml and
# P.<env>.local.toml for that environment; mise/mise.toml and .mise/mise.toml
# configure the `mise` and `.mise` directories themselves.
_CONFIG_RE = re.compile(
    r"(?:^|/)(?P<file>"
    r"\.config/mise/mise(?:\.local)?\.toml"
    r"|(?:\.?mise|\.config/mise)/conf\.d/[^/]+\.toml"
    r"|(?:\.config/mise/config|\.config/mise|\.?mise/config|\.?mise)(?:\.(?P<env>[^/]+?))?\.toml"
    r")$"
)
TASK_DIRS: tuple[str, ...] = ("mise-tasks", ".mise-tasks", "mise/tasks", ".mise/tasks", ".config/mise/tasks")
_TASK_DIR_RE = re.compile(r"(?:^|/)(" + "|".join(re.escape(d) for d in TASK_DIRS) + r")/(.+)$")
_REMOTE_PREFIXES = ("git::", "oci::", "zip::", "http://", "https://")
_HEADER_RE = re.compile(r"^\s*(?:#|//|::|--)\s*(?:MISE|\[MISE\])(?:\s+(.*))?$")
# Anything shaped like a header line under another comment marker.
_HEADER_LIKE_RE = re.compile(r"^\s*[^\s\w]{1,3}\s*\[?MISE\]?(?:\s|$)")
_NO_ALIASES: Mapping[str, str] = MappingProxyType({})
# The file-task extensions mise drops from a task's name, and ones it keeps,
# as mise 2026.11 does; any other extension may go either way.
_STRIPPED_EXTENSIONS = frozenset(
    "sh bash zsh fish ksh ps1 py js ts mjs cjs mts rb pl php lua cmd bat exe nu vbs".split()
)
_KEPT_EXTENSIONS = frozenset(
    """md txt json yaml yml toml gz tar zip ex exs r go rs swift kts deno tsx jsx cts java psm1 awk tcl elv xsh
    pwsh pyw ash dash csh tcsh coffee groovy ps xonsh jl dart scala kt hs ml mk make just bun sql cs fsx vb sed
    perl ruby python node rake bb clj nim zig v d""".split()
)


@dataclass(frozen=True)
class ConfigFile:
    """``owner``: the directory a config file configures (``""`` for the
    repository root). ``env``: the environment it belongs to, None for one
    mise always reads."""

    owner: str
    env: str | None


def config_file(rel: str) -> ConfigFile | None:
    """What ``rel`` is to mise as a config file, None when it is not one."""
    match = _CONFIG_RE.search(rel)
    if match is None:
        return None
    env = match.group("env")
    if env is not None:
        env = None if env == "local" else env.removesuffix(".local")
    return ConfigFile(rel[: match.start("file")].rstrip("/"), env)


def config_owner(rel: str) -> str | None:
    found = config_file(rel)
    return None if found is None else found.owner


def task_dir_entry(rel: str) -> tuple[str, str] | None:
    """``(owning directory, path inside the task directory)`` of a file under
    a default task directory, None for any other file."""
    match = _TASK_DIR_RE.search(rel)
    if match is None:
        return None
    return rel[: match.start(1)].rstrip("/"), match.group(2)


def file_task_names(path: str) -> tuple[frozenset[str], frozenset[str]]:
    """The task names a file task at ``path`` (inside its task directory)
    answers to, and the names it may answer to. ``a/b/c.sh`` is ``a:b:c`` and
    ``a:b:c.sh``; ``README.md`` is only ``README.md``; ``a/_default`` is also
    ``a``. An extension mise is not known to drop or keep leaves the name
    without it uncertain."""
    parts = path.split("/")
    stem, ext = posixpath.splitext(parts[-1])
    ext = ext[1:]
    names = {":".join(parts)}
    maybe: set[str] = set()
    if ext in _STRIPPED_EXTENSIONS:
        names.add(":".join([*parts[:-1], stem]))
    elif ext and ext.lower() not in _KEPT_EXTENSIONS:
        maybe.add(":".join([*parts[:-1], stem]))
    if len(parts) > 1 and (parts[-1] == "_default" or (stem == "_default" and ext in _STRIPPED_EXTENSIONS)):
        names.add(":".join(parts[:-1]))
    return frozenset(names), frozenset(maybe - names)


@dataclass(frozen=True)
class MiseFile:
    """``tasks``: the task names and aliases the file defines. ``includes``:
    its local ``[task_config] includes`` entries; ``declares_includes``: it
    has the key at all, which replaces the default task directories.
    ``opaque``: why its task set is not provable, None when it is.
    ``path_aliases``: ``[monorepo.path_aliases]``. ``monorepo_root``: it sets
    ``monorepo_root`` (or ``experimental_monorepo_root``). ``config_roots``:
    ``[monorepo] config_roots``, None when absent or empty (mise then finds
    the config roots itself)."""

    tasks: frozenset[str]
    includes: tuple[str, ...] = ()
    opaque: str | None = None
    path_aliases: Mapping[str, str] = field(default=_NO_ALIASES)
    declares_includes: bool = False
    monorepo_root: bool = False
    config_roots: tuple[str, ...] | None = None


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
    task_config = data.get("task_config")
    monorepo = data.get("monorepo")
    monorepo = monorepo if isinstance(monorepo, dict) else {}
    aliases = monorepo.get("path_aliases")
    path_aliases = (
        {key: value for key, value in aliases.items() if isinstance(value, str)} if isinstance(aliases, dict) else {}
    )
    roots = monorepo.get("config_roots")
    config_roots = tuple(root for root in roots if isinstance(root, str)) if isinstance(roots, list) else ()
    return MiseFile(
        frozenset(names),
        includes,
        opaque or include_opaque,
        MappingProxyType(path_aliases) if path_aliases else _NO_ALIASES,
        declares_includes=isinstance(task_config, dict) and "includes" in task_config,
        monorepo_root=data.get("monorepo_root") is True or data.get("experimental_monorepo_root") is True,
        config_roots=config_roots or None,
    )


def file_task_header(rel: str, text: str) -> MiseFile | Unreadable:
    """The aliases a file task's ``#MISE`` header lines declare. A header that
    is not TOML, or one that ``extends`` another task, leaves them unknown."""
    lines: list[str] = []
    for line in text.splitlines():
        if (match := _HEADER_RE.match(line)) is not None:
            lines.append(match.group(1) or "")
        elif _HEADER_LIKE_RE.match(line):
            return MiseFile(frozenset(), opaque=f"{rel} has a MISE header line the docs check cannot read")
    if not lines:
        return MiseFile(frozenset())
    try:
        header = tomllib.loads("\n".join(lines))
    except tomllib.TOMLDecodeError:
        return MiseFile(frozenset(), opaque=f"the #MISE header of {rel} is not TOML the docs check can read")
    except RecursionError as exc:
        return unreadable(rel, exc)
    names, opaque = _task_names({"": header}, rel)
    return MiseFile(frozenset(names - {""}), opaque=opaque and f"file task {rel} extends another task")


def mise_file_from_bytes(rel: str, data: bytes) -> MiseFile | Unreadable:
    """``rel`` read as a config file when its name is one, as a TOML task file
    when it ends ``.toml``, else as a file task (its header). A file task may
    be any script or binary, so its bytes are decoded leniently."""
    if config_owner(rel) is None and not rel.endswith(".toml"):
        return file_task_header(rel, data.decode("utf-8", "replace"))
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        return unreadable(rel, exc)
    return read_mise_file(parsed, rel, task_file=config_owner(rel) is None)
