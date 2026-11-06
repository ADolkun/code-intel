"""The consumer's ``.code-intel/config.toml``: every repository-specific fact the analyzers use.

All paths are repository-relative. Unknown keys, unknown kinds and missing
required keys are errors that name the key and the config file.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from code_intel.result_types import ClientSpec

CONFIG_REL = ".code-intel/config.toml"

DOC_CHECKS: tuple[str, ...] = ("links", "paths", "routes", "symbols", "scripts")
REFERENCE_KINDS: tuple[str, ...] = ("link", "path", "route", "symbol", "script")
PageKind = Literal["next-app", "react-router", "none"]
PAGE_KINDS: tuple[PageKind, ...] = ("next-app", "react-router", "none")
HTTP_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

_APP_RE = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*(?:\(\))?")


class ConfigError(ValueError):
    """The config cannot be read or is invalid; ``str()`` is ``<file>: <reason>``."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


CAPTURE_FILE = ".code-intel/library-capture.json"
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class CaptureConfig:
    """``[backend.capture]``: how ``capture``, ``verify`` and ``runtime-check`` start the app."""

    python: str  # an interpreter with the app's dependencies
    file: str = CAPTURE_FILE
    env: tuple[tuple[str, str], ...] = ()  # synthetic values; the import sees nothing else of the environment


@dataclass(frozen=True)
class BackendConfig:
    root: str  # import root: the directory holding the package
    package: str
    app: str  # module:attribute, or module:factory()
    lock: str | None  # the locked FastAPI decides the include model
    python_version_file: str | None
    capture: CaptureConfig | None = None


@dataclass(frozen=True)
class PagesConfig:
    kind: PageKind = "none"
    modules: tuple[str, ...] = ()
    public_dir: str | None = None


@dataclass(frozen=True)
class FrontendConfig:
    root: str
    tsconfig: str  # options only; the file set is the walk
    lockfile: str | None
    clients: tuple[ClientSpec, ...]
    pages: PagesConfig


@dataclass(frozen=True)
class SymbolsConfig:
    primary_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reachability:
    root: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class DocRule:
    """Docs matching any of ``paths`` get only ``checks``; the first matching rule wins."""

    paths: tuple[str, ...]
    checks: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DocIgnore:
    file: str
    kind: str
    text: str
    reason: str


@dataclass(frozen=True)
class DocsConfig:
    scope: tuple[str, ...]
    route_prefixes: tuple[str, ...]  # a mention may add or omit any of these
    reachability: Reachability | None  # without it there is no orphan check
    rules: tuple[DocRule, ...]
    ignore: tuple[DocIgnore, ...]


@dataclass(frozen=True)
class InstructionMap:
    file: str
    heading: str


@dataclass(frozen=True)
class AgentsConfig:
    rules_dir: str
    skills_dir: str
    instruction_map: InstructionMap | None


@dataclass(frozen=True)
class Config:
    path: Path  # absolute path of the file this was read from
    backend: BackendConfig
    frontend: FrontendConfig
    symbols: SymbolsConfig
    docs: DocsConfig | None
    agents: AgentsConfig | None


class _Table:
    """A TOML table whose keys are read one by one; ``done()`` rejects the rest."""

    def __init__(self, data: object, name: str, path: str, index: int | None = None) -> None:
        self.name = name  # dotted, as the docs spell it: frontend.clients
        self.where = "the config" if not name else f"[{name}]" if index is None else f"[[{name}]] #{index + 1}"
        self.path = path
        if not isinstance(data, dict):
            raise ConfigError(path, f"{self.where} must be a table")
        self._data: dict[str, object] = dict(data)

    def fail(self, reason: str) -> ConfigError:
        return ConfigError(self.path, reason)

    def _bad(self, key: str, expected: str) -> ConfigError:
        return self.fail(f"{key!r} in {self.where} must be {expected}")

    def _required(self, key: str) -> object:
        if key not in self._data:
            raise self.fail(f"missing required key {key!r} in {self.where}")
        return self._data.pop(key)

    def text(self, key: str) -> str:
        return self._text(key, self._required(key))

    def opt_text(self, key: str, default: str | None = None) -> str | None:
        return self._text(key, self._data.pop(key)) if key in self._data else default

    def _text(self, key: str, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self._bad(key, "a non-empty string")
        return value

    def relpath(self, key: str) -> str:
        return self._relative(key, self.text(key))

    def opt_relpath(self, key: str) -> str | None:
        value = self.opt_text(key)
        return None if value is None else self._relative(key, value)

    def _relative(self, key: str, value: str) -> str:
        if PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or "\\" in value:
            raise self._bad(key, "a repository-relative path with forward slashes and no '..'")
        return value

    def strings(self, key: str, *, required: bool = False, relative: bool = False) -> tuple[str, ...]:
        value = self._required(key) if required else self._data.pop(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise self._bad(key, "a list of non-empty strings")
        if required and not value:
            raise self._bad(key, "a non-empty list")
        if relative:
            for item in value:
                self._relative(key, item)
        return tuple(value)

    def integer(self, key: str, default: int) -> int:
        value = self._data.pop(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise self._bad(key, "a non-negative integer")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._data.pop(key, default)
        if not isinstance(value, bool):
            raise self._bad(key, "true or false")
        return value

    def choice(self, key: str, allowed: tuple[str, ...]) -> str:
        return self._choice(key, allowed, self._required(key))

    def opt_choice[C: str](self, key: str, allowed: tuple[C, ...], default: C) -> C:
        return self._choice(key, allowed, self._data.pop(key, default))

    def _choice[C: str](self, key: str, allowed: tuple[C, ...], value: object) -> C:
        for choice in allowed:
            if value == choice:
                return choice
        raise self.fail(f"invalid value for {key!r} in {self.where}: {value!r} is not one of {', '.join(allowed)}")

    def strings_by_name(self, key: str) -> tuple[tuple[str, str], ...]:
        """A table of environment variables: a name matching ``[A-Za-z_][A-Za-z0-9_]*`` to a string."""
        value = self._data.pop(key, {})
        if not isinstance(value, dict):
            raise self._bad(key, "a table of strings")
        for name, item in value.items():
            if not _ENV_NAME_RE.fullmatch(name):
                raise self.fail(f"{name!r} in {key!r} of {self.where} is not an environment variable name")
            if not isinstance(item, str):
                raise self.fail(f"{name!r} in {key!r} of {self.where} must be a string")
        return tuple(sorted(value.items()))

    def table(self, key: str) -> _Table | None:
        return _Table(self._data.pop(key), self._child(key), self.path) if key in self._data else None

    def required_table(self, key: str) -> _Table:
        return _Table(self._required(key), self._child(key), self.path)

    def tables(self, key: str) -> list[_Table]:
        value = self._data.pop(key, [])
        if not isinstance(value, list):
            raise self._bad(key, "an array of tables")
        return [_Table(item, self._child(key), self.path, index) for index, item in enumerate(value)]

    def _child(self, key: str) -> str:
        return f"{self.name}.{key}" if self.name else key

    def unknown_keys(self, allowed: tuple[str, ...]) -> list[str]:
        return sorted(set(self._data) - set(allowed))

    def done(self) -> None:
        if self._data:
            raise self.fail(f"unknown key {sorted(self._data)[0]!r} in {self.where}")


def _parse_capture(table: _Table) -> CaptureConfig:
    capture = CaptureConfig(
        python=table.relpath("python"),
        file=table.opt_relpath("file") or CAPTURE_FILE,
        env=table.strings_by_name("env"),
    )
    table.done()
    return capture


def _parse_backend(table: _Table) -> BackendConfig:
    root = table.relpath("root")
    package = table.text("package")
    app = table.text("app")
    if not _APP_RE.fullmatch(app):
        raise table.fail(f"'app' in {table.where} must be module:attribute or module:factory(), not {app!r}")
    config = BackendConfig(
        root=root,
        package=package,
        app=app,
        lock=table.opt_relpath("lock"),
        python_version_file=table.opt_relpath("python_version_file"),
        capture=_parse_capture(capture) if (capture := table.table("capture")) is not None else None,
    )
    table.done()
    return config


_CLIENT_KEYS: Mapping[str, tuple[str, ...]] = {
    "call": ("module", "export", "url_arg", "init_arg", "default_method"),
    "options": ("module", "export", "options_arg", "url_property", "default_method"),
    "instance": ("module", "package", "export", "base_path"),
    "global": ("name", "url_arg", "init_arg", "same_origin_only", "default_method"),
}


def _parse_client(table: _Table) -> ClientSpec:
    kind = table.choice("kind", tuple(_CLIENT_KEYS))
    allowed = _CLIENT_KEYS[kind]
    if extra := table.unknown_keys(allowed):
        raise table.fail(f"unknown key {extra[0]!r} in {table.where} (kind {kind!r} takes {', '.join(allowed)})")
    method = table.opt_choice("default_method", HTTP_METHODS, "GET")
    match kind:
        case "call":
            spec = ClientSpec(
                kind="call",
                module=table.relpath("module"),
                export=table.text("export"),
                url_arg=table.integer("url_arg", 0),
                init_arg=table.integer("init_arg", 1),
                default_method=method,
            )
        case "options":
            spec = ClientSpec(
                kind="options",
                module=table.relpath("module"),
                export=table.text("export"),
                options_arg=table.integer("options_arg", 0),
                url_property=table.opt_text("url_property", "url"),
                default_method=method,
            )
        case "instance":
            base_path = table.opt_text("base_path", "") or ""
            if base_path and not base_path.startswith("/"):
                raise table.fail(f"'base_path' in {table.where} must start with '/'")
            package = table.opt_text("package")
            if package is None:
                module, export = table.relpath("module"), table.text("export")
            else:
                if table.opt_relpath("module") is not None:
                    raise table.fail(f"'module' and 'package' in {table.where} are mutually exclusive")
                module, export = None, table.opt_text("export", "default")
            spec = ClientSpec(kind="instance", module=module, package=package, export=export, base_path=base_path)
        case _:
            spec = ClientSpec(
                kind="global",
                name=table.text("name"),
                url_arg=table.integer("url_arg", 0),
                init_arg=table.integer("init_arg", 1),
                same_origin_only=table.boolean("same_origin_only", False),
                default_method=method,
            )
    table.done()
    return spec


def _parse_pages(table: _Table | None) -> PagesConfig:
    if table is None:
        return PagesConfig()
    kind: PageKind = table.opt_choice("kind", PAGE_KINDS, "none")
    modules = table.strings("modules", relative=True)
    public_dir = table.opt_relpath("public_dir")
    if kind == "react-router" and not modules:
        raise table.fail(f"'modules' in {table.where} is required for kind 'react-router'")
    if kind != "react-router" and modules:
        raise table.fail(f"'modules' in {table.where} is only for kind 'react-router'")
    table.done()
    return PagesConfig(kind=kind, modules=modules, public_dir=public_dir)


def _parse_frontend(table: _Table) -> FrontendConfig:
    config = FrontendConfig(
        root=table.relpath("root"),
        tsconfig=table.relpath("tsconfig"),
        lockfile=table.opt_relpath("lockfile"),
        clients=tuple(_parse_client(client) for client in table.tables("clients")),
        pages=_parse_pages(table.table("pages")),
    )
    table.done()
    return config


def _parse_reachability(table: _Table) -> Reachability:
    reachability = Reachability(
        root=table.relpath("root"),
        include=table.strings("include", relative=True),
        exclude=table.strings("exclude", relative=True),
    )
    table.done()
    return reachability


def _parse_rule(table: _Table) -> DocRule:
    paths = table.strings("paths", required=True, relative=True)
    checks = table.strings("checks", required=True)
    if bad := [check for check in checks if check not in DOC_CHECKS]:
        raise table.fail(
            f"invalid value for 'checks' in {table.where}: {bad[0]!r} is not one of {', '.join(DOC_CHECKS)}"
        )
    rule = DocRule(paths, checks, table.text("reason"))
    table.done()
    return rule


def _parse_ignore(table: _Table) -> DocIgnore:
    entry = DocIgnore(
        file=table.relpath("file"),
        kind=table.choice("kind", REFERENCE_KINDS),
        text=table.text("text"),
        reason=table.text("reason"),
    )
    table.done()
    return entry


def _parse_docs(table: _Table) -> DocsConfig:
    scope = table.strings("scope", required=True, relative=True)
    prefixes = table.strings("route_prefixes")
    if bad := [p for p in prefixes if not p.startswith("/") or (len(p) > 1 and p.endswith("/"))]:
        raise table.fail(f"'route_prefixes' in {table.where}: {bad[0]!r} must start with '/' and not end with '/'")
    reachability_table = table.table("reachability")
    docs = DocsConfig(
        scope=scope,
        route_prefixes=prefixes,
        reachability=_parse_reachability(reachability_table) if reachability_table is not None else None,
        rules=tuple(_parse_rule(rule) for rule in table.tables("rules")),
        ignore=tuple(_parse_ignore(entry) for entry in table.tables("ignore")),
    )
    table.done()
    return docs


def _parse_agents(table: _Table) -> AgentsConfig:
    rules_dir = table.relpath("rules_dir")
    skills_dir = table.relpath("skills_dir")
    map_table = table.table("instruction_map")
    instruction_map = None
    if map_table is not None:
        instruction_map = InstructionMap(file=map_table.relpath("file"), heading=map_table.text("heading"))
        map_table.done()
    table.done()
    return AgentsConfig(rules_dir, skills_dir, instruction_map)


def _parse_symbols(table: _Table | None) -> SymbolsConfig:
    if table is None:
        return SymbolsConfig()
    symbols = SymbolsConfig(table.strings("primary_prefixes", relative=True))
    table.done()
    return symbols


def parse_config(text: str, display: str = CONFIG_REL, path: Path | None = None) -> Config:
    """Validate the config text. ``display`` is how errors name the file. Raises ``ConfigError``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(display, f"invalid TOML: {exc}") from exc
    top = _Table(data, "", display)
    config = Config(
        path=path or Path(display),
        backend=_parse_backend(top.required_table("backend")),
        frontend=_parse_frontend(top.required_table("frontend")),
        symbols=_parse_symbols(top.table("symbols")),
        docs=_parse_docs(docs) if (docs := top.table("docs")) is not None else None,
        agents=_parse_agents(agents) if (agents := top.table("agents")) is not None else None,
    )
    top.done()
    return config


@dataclass(frozen=True)
class ConfigFile:
    """Where the config is read from: ``path`` (absolute) and ``display``, how errors name it."""

    path: Path
    display: str

    @classmethod
    def locate(cls, repo_root: Path, config_path: Path | None = None) -> ConfigFile:
        """``config_path``, or ``<repo_root>/.code-intel/config.toml``."""
        if config_path is not None:
            return cls(config_path.resolve(), str(config_path))
        return cls((repo_root / CONFIG_REL).resolve(), CONFIG_REL)

    def read_bytes(self) -> bytes:
        """The file's bytes. Raises ``ConfigError``."""
        try:
            return self.path.read_bytes()
        except OSError as exc:
            raise ConfigError(self.display, f"cannot read the config ({type(exc).__name__}: {exc})") from exc

    def parse(self, data: bytes) -> Config:
        """``data`` (this file's bytes) validated. Raises ``ConfigError``."""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConfigError(self.display, f"the config is not UTF-8 ({exc})") from exc
        return parse_config(text, self.display, self.path)


def load_config(repo_root: Path, config_path: Path | None = None) -> Config:
    """Read and validate the config: ``config_path``, or ``<repo_root>/.code-intel/config.toml``.

    Raises ``ConfigError``.
    """
    file = ConfigFile.locate(repo_root, config_path)
    return file.parse(file.read_bytes())
