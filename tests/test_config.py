"""Tests for the consumer config: parsing, defaults, strict errors and the config-check command."""

from __future__ import annotations

import argparse
import importlib.machinery
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest
from fixture_repo import FULL_CONFIG, MINIMAL_CONFIG, hermetic_git, make_repo

from code_intel import cli, config
from code_intel.result_types import ClientSpec


def parse(text: str) -> config.Config:
    return config.parse_config(text, "cfg.toml")


def error_of(text: str) -> str:
    with pytest.raises(config.ConfigError) as caught:
        parse(text)
    return str(caught.value)


def with_section(section: str, extra: str) -> str:
    """``MINIMAL_CONFIG`` with ``extra`` appended to ``[section]``."""
    return MINIMAL_CONFIG.replace(f"[{section}]\n", f"[{section}]\n{extra}\n", 1)


def test_full_config_is_read() -> None:
    cfg = parse(FULL_CONFIG)
    assert cfg.backend == config.BackendConfig(
        root="backend",
        package="service_backend",
        app="service_backend.main:app",
        lock="backend/uv.lock",
        python_version_file="backend/.python-version",
    )
    assert cfg.frontend.root == "frontend"
    assert cfg.frontend.lockfile == "frontend/package-lock.json"
    assert cfg.frontend.clients == (
        ClientSpec(kind="instance", module="frontend/lib/api.ts", export="api", base_path="/api"),
        ClientSpec(kind="global", name="fetch", url_arg=0, init_arg=1, same_origin_only=True),
        ClientSpec(kind="call", module="frontend/lib/http.ts", export="request", url_arg=0, init_arg=1),
        ClientSpec(
            kind="options", module="frontend/lib/download.ts", export="download", options_arg=0, url_property="url"
        ),
    )
    assert cfg.frontend.pages == config.PagesConfig("react-router", ("frontend/src/App.tsx",), "frontend/public")
    assert cfg.symbols.primary_prefixes == ("frontend/components/", "backend/service_backend/")
    assert cfg.docs is not None
    assert cfg.docs.route_prefixes == ("/api",)
    assert cfg.docs.reachability == config.Reachability("docs/README.md", ("docs/**/*.md",), ())
    assert cfg.docs.rules == (
        config.DocRule(("docs/imported.md",), ("links",), "Imported material: only its links must resolve."),
    )
    assert cfg.docs.ignore == (
        config.DocIgnore("docs/guide.md", "route", "/api/external", "Served by another service."),
    )
    assert cfg.agents == config.AgentsConfig(
        ".claude/rules", ".claude/skills", config.InstructionMap("AGENTS.md", "Scoped instruction map")
    )


def test_a_minimal_config_takes_the_defaults() -> None:
    cfg = parse(MINIMAL_CONFIG)
    assert cfg.backend.lock is None and cfg.backend.python_version_file is None
    assert cfg.frontend.clients == ()
    assert cfg.frontend.pages == config.PagesConfig("none", (), None)
    assert cfg.symbols == config.SymbolsConfig(())
    assert cfg.docs is None and cfg.agents is None


def test_docs_without_reachability_has_no_orphan_check() -> None:
    cfg = parse(MINIMAL_CONFIG + '\n[docs]\nscope = ["docs/**/*.md"]\n')
    assert cfg.docs is not None
    assert cfg.docs.reachability is None and cfg.docs.route_prefixes == () and cfg.docs.rules == ()


def test_client_defaults_by_kind() -> None:
    text = MINIMAL_CONFIG + (
        '\n[[frontend.clients]]\nkind = "global"\nname = "fetch"\n'
        '\n[[frontend.clients]]\nkind = "instance"\nmodule = "frontend/api.ts"\nexport = "api"\n'
    )
    fetch, instance = parse(text).frontend.clients
    assert (fetch.url_arg, fetch.init_arg, fetch.same_origin_only, fetch.default_method) == (0, 1, False, "GET")
    assert instance.base_path == "" and instance.default_method == "GET"


def test_unknown_keys_name_the_key_and_the_file() -> None:
    assert error_of(with_section("backend", "colour = 1")) == "cfg.toml: unknown key 'colour' in [backend]"
    assert error_of(MINIMAL_CONFIG + "\n[wat]\nx = 1\n") == "cfg.toml: unknown key 'wat' in the config"
    text = MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "global"\nname = "fetch"\nbase_path = "/api"\n'
    message = error_of(text)
    assert message.startswith("cfg.toml: unknown key 'base_path' in [[frontend.clients]] #1")
    assert "kind 'global' takes" in message


def test_unknown_nested_keys_are_named_with_their_table() -> None:
    assert "unknown key 'x' in [docs.reachability]" in error_of(
        MINIMAL_CONFIG + '\n[docs]\nscope = ["a.md"]\n[docs.reachability]\nroot = "a.md"\nx = 1\n'
    )
    assert "unknown key 'x' in [[docs.rules]] #2" in error_of(
        MINIMAL_CONFIG
        + '\n[docs]\nscope = ["a.md"]\n'
        + '[[docs.rules]]\npaths = ["a.md"]\nchecks = ["links"]\nreason = "r"\n'
        + '[[docs.rules]]\npaths = ["a.md"]\nchecks = ["links"]\nreason = "r"\nx = 1\n'
    )


def test_an_unknown_client_kind_is_an_error() -> None:
    message = error_of(MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "xhr"\nname = "x"\n')
    assert message == (
        "cfg.toml: invalid value for 'kind' in [[frontend.clients]] #1: 'xhr' is not one of "
        "call, options, instance, global"
    )


def test_an_instance_client_can_be_an_npm_package_export() -> None:
    text = (
        MINIMAL_CONFIG
        + '\n[[frontend.clients]]\nkind = "instance"\npackage = "axios"\n'
        + '\n[[frontend.clients]]\nkind = "instance"\npackage = "ky"\nexport = "ky"\nbase_path = "/api"\n'
    )
    default, named = parse(text).frontend.clients
    assert (default.package, default.module, default.export, default.base_path) == ("axios", None, "default", "")
    assert (named.package, named.module, named.export, named.base_path) == ("ky", None, "ky", "/api")
    assert default.as_request() == {
        "kind": "instance",
        "package": "axios",
        "export": "default",
        "default_method": "GET",
    }


def test_an_instance_client_names_a_module_or_a_package_not_both() -> None:
    both = '\n[[frontend.clients]]\nkind = "instance"\npackage = "axios"\nmodule = "m.ts"\nexport = "a"\n'
    assert error_of(MINIMAL_CONFIG + both) == (
        "cfg.toml: 'module' and 'package' in [[frontend.clients]] #1 are mutually exclusive"
    )
    assert "'package' in [[frontend.clients]] #1 must be a non-empty string" in error_of(
        MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "instance"\npackage = ""\n'
    )
    assert "unknown key 'package'" in error_of(
        MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "call"\nmodule = "m.ts"\nexport = "a"\npackage = "p"\n'
    )


def test_the_capture_table_is_optional_and_takes_its_defaults() -> None:
    assert parse(MINIMAL_CONFIG).backend.capture is None
    cfg = parse(MINIMAL_CONFIG + '\n[backend.capture]\npython = "backend/.venv/bin/python"\n')
    assert cfg.backend.capture == config.CaptureConfig(
        "backend/.venv/bin/python", ".code-intel/library-capture.json", ()
    )


def test_the_capture_table_reads_its_file_and_a_sorted_environment() -> None:
    cfg = parse(
        MINIMAL_CONFIG + '\n[backend.capture]\npython = "py"\nfile = "x/cap.json"\nenv = { B_KEY = "2", A_KEY = "" }\n'
    )
    assert cfg.backend.capture == config.CaptureConfig("py", "x/cap.json", (("A_KEY", ""), ("B_KEY", "2")))


@pytest.mark.parametrize(
    "table,expected",
    [
        ("", "missing required key 'python' in [backend.capture]"),
        ('python = "/abs/python"', "'python' in [backend.capture] must be a repository-relative path"),
        ('python = "../python"', "'python' in [backend.capture] must be a repository-relative path"),
        ('python = "py"\nfile = "../c.json"', "'file' in [backend.capture] must be a repository-relative path"),
        ('python = "py"\nenv = ["A=1"]', "'env' in [backend.capture] must be a table of strings"),
        ('python = "py"\nenv = { A = 1 }', "'A' in 'env' of [backend.capture] must be a string"),
        (
            'python = "py"\nenv = { "A-B" = "1" }',
            "'A-B' in 'env' of [backend.capture] is not an environment variable name",
        ),
        ('python = "py"\nvenv = "x"', "unknown key 'venv' in [backend.capture]"),
    ],
)
def test_a_bad_capture_table_is_named(table: str, expected: str) -> None:
    assert expected in error_of(MINIMAL_CONFIG + f"\n[backend.capture]\n{table}\n")


def test_missing_required_keys_are_named() -> None:
    assert error_of(MINIMAL_CONFIG.replace('package = "service_backend"\n', "")) == (
        "cfg.toml: missing required key 'package' in [backend]"
    )
    assert error_of('[backend]\nroot = "b"\npackage = "p"\napp = "p.m:a"\n') == (
        "cfg.toml: missing required key 'frontend' in the config"
    )
    assert error_of(MINIMAL_CONFIG + "\n[[frontend.clients]]\nname = 'fetch'\n") == (
        "cfg.toml: missing required key 'kind' in [[frontend.clients]] #1"
    )
    assert error_of(MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "instance"\nexport = "api"\n') == (
        "cfg.toml: missing required key 'module' in [[frontend.clients]] #1"
    )
    assert error_of(MINIMAL_CONFIG + '\n[docs]\nroute_prefixes = ["/api"]\n') == (
        "cfg.toml: missing required key 'scope' in [docs]"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (with_section("backend", "lock = 3"), "'lock' in [backend] must be a non-empty string"),
        (with_section("backend", 'lock = "/abs/uv.lock"'), "'lock' in [backend] must be a repository-relative path"),
        (with_section("backend", 'lock = "../uv.lock"'), "'lock' in [backend] must be a repository-relative path"),
        (
            MINIMAL_CONFIG.replace("service_backend.main:app", "service_backend.main"),
            "'app' in [backend] must be module:attribute",
        ),
        (MINIMAL_CONFIG + '\n[symbols]\nprimary_prefixes = "a/"\n', "'primary_prefixes' in [symbols] must be a list"),
        (
            MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "global"\nname = "fetch"\nurl_arg = -1\n',
            "'url_arg' in [[frontend.clients]] #1 must be a non-negative integer",
        ),
        (
            MINIMAL_CONFIG + '\n[[frontend.clients]]\nkind = "global"\nname = "fetch"\nsame_origin_only = "yes"\n',
            "'same_origin_only' in [[frontend.clients]] #1 must be true or false",
        ),
        (
            MINIMAL_CONFIG
            + '\n[[frontend.clients]]\nkind = "instance"\nmodule = "m.ts"\nexport = "a"\nbase_path = "api"\n',
            "'base_path' in [[frontend.clients]] #1 must start with '/'",
        ),
        (
            MINIMAL_CONFIG
            + '\n[[frontend.clients]]\nkind = "call"\nmodule = "m.ts"\nexport = "a"\ndefault_method = "get"\n',
            "invalid value for 'default_method'",
        ),
        (MINIMAL_CONFIG + '\n[frontend.pages]\nkind = "react-router"\n', "'modules' in [frontend.pages] is required"),
        (
            MINIMAL_CONFIG + '\n[frontend.pages]\nkind = "next-app"\nmodules = ["a.tsx"]\n',
            "'modules' in [frontend.pages] is only for kind 'react-router'",
        ),
        (MINIMAL_CONFIG + '\n[frontend.pages]\nkind = "svelte"\n', "invalid value for 'kind' in [frontend.pages]"),
        (
            MINIMAL_CONFIG + "\n[docs]\nscope = []\n",
            "'scope' in [docs] must be a non-empty list",
        ),
        (
            MINIMAL_CONFIG + '\n[docs]\nscope = ["a.md"]\nroute_prefixes = ["api"]\n',
            "'route_prefixes' in [docs]: 'api' must start with '/'",
        ),
        (
            MINIMAL_CONFIG
            + '\n[docs]\nscope = ["a.md"]\n[[docs.rules]]\npaths = ["a.md"]\nchecks = ["spelling"]\nreason = "r"\n',
            "invalid value for 'checks' in [[docs.rules]] #1: 'spelling' is not one of links, paths, routes, symbols, scripts",
        ),
        (
            MINIMAL_CONFIG
            + '\n[docs]\nscope = ["a.md"]\n[[docs.ignore]]\nfile = "a.md"\nkind = "word"\ntext = "t"\nreason = "r"\n',
            "invalid value for 'kind' in [[docs.ignore]] #1: 'word' is not one of link, path, route, symbol",
        ),
        (
            MINIMAL_CONFIG + '\n[docs]\nscope = ["a.md"]\n[[docs.ignore]]\nfile = "a.md"\nkind = "route"\ntext = "t"\n',
            "missing required key 'reason' in [[docs.ignore]] #1",
        ),
        ("agents = 3\n" + MINIMAL_CONFIG, "[agents] must be a table"),
    ],
)
def test_invalid_values_are_errors(text: str, expected: str) -> None:
    message = error_of(text)
    assert message.startswith("cfg.toml: ")
    assert expected in message


def test_invalid_toml_names_the_file() -> None:
    assert error_of("[backend\n").startswith("cfg.toml: invalid TOML: ")


def test_load_config_reads_the_default_file_and_an_explicit_one(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    default = config.load_config(repo)
    assert default.path == (repo / ".code-intel" / "config.toml").resolve()
    other = tmp_path / "other.toml"
    other.write_text(MINIMAL_CONFIG, encoding="utf-8")
    explicit = config.load_config(repo, other)
    assert explicit.path == other.resolve() and explicit.docs is None


def test_load_config_names_the_file_it_cannot_read(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match=r"^\.code-intel/config\.toml: cannot read the config"):
        config.load_config(tmp_path)
    missing = tmp_path / "nope.toml"
    with pytest.raises(config.ConfigError, match=f"^{missing}: cannot read the config"):
        config.load_config(tmp_path, missing)
    binary = tmp_path / "binary.toml"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(config.ConfigError, match="not UTF-8"):
        config.load_config(tmp_path, binary)


def test_load_config_error_names_the_file_as_given(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(with_section("backend", "colour = 1"), encoding="utf-8")
    with pytest.raises(config.ConfigError) as caught:
        config.load_config(tmp_path, bad)
    assert str(caught.value) == f"{bad}: unknown key 'colour' in [backend]"


# ── config-check ─────────────────────────────────────────────────────────


def test_config_check_accepts_a_valid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path / "repo")
    assert cli.main(["config-check", "--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == f"ok: {(repo / '.code-intel' / 'config.toml').resolve()}\n"


def test_config_check_takes_an_explicit_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    explicit = tmp_path / "explicit.toml"
    explicit.write_text(MINIMAL_CONFIG, encoding="utf-8")
    assert cli.main(["config-check", "--repo-root", str(repo), "--config", str(explicit)]) == 0
    assert capsys.readouterr().out == f"ok: {explicit.resolve()}\n"


def test_config_check_rejects_a_bad_config_naming_key_and_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(with_section("frontend", "colour = 1"), encoding="utf-8")
    assert cli.main(["config-check", "--repo-root", str(tmp_path), "--config", str(bad)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"code-intel: {bad}: unknown key 'colour' in [frontend]\n"


def test_config_check_finds_the_repo_root_from_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hermetic_git(monkeypatch, tmp_path / "home")
    repo = make_repo(tmp_path / "repo")
    import subprocess

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo / "frontend" / "components")
    assert cli.main(["config-check"]) == 0
    assert capsys.readouterr().out == f"ok: {(repo / '.code-intel' / 'config.toml').resolve()}\n"


def test_outside_a_git_work_tree_needs_a_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hermetic_git(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert cli.main(["config-check"]) == 2
    assert "pass --repo-root" in capsys.readouterr().err


# ── command table ────────────────────────────────────────────────────────


class _FakeCommand(types.ModuleType):
    add_arguments: Callable[[argparse.ArgumentParser], object]
    run: Callable[[argparse.Namespace, cli.Context], int]


def fake_command(monkeypatch: pytest.MonkeyPatch, name: str, calls: list) -> None:
    module = _FakeCommand(f"code_intel.{name}")
    module.__spec__ = importlib.machinery.ModuleSpec(f"code_intel.{name}", None)
    module.add_arguments = lambda parser: parser.add_argument("--flag")

    def run(args, context) -> int:
        calls.append((args.flag, context.repo_root, context.config.backend.package))
        return 7

    module.run = run
    monkeypatch.setitem(sys.modules, f"code_intel.{name}", module)


def test_only_commands_with_a_module_are_offered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.COMMANDS, "ghost", ("no_such_module", "a command without a module"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "config-check" in out and "ghost" not in out


def test_a_command_module_gets_its_options_and_the_loaded_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    fake_command(monkeypatch, "fake_dump", calls)
    fake_command(monkeypatch, "fake_top", calls)
    monkeypatch.setitem(cli.COMMANDS, "dump fake", ("fake_dump", "a fake dump"))
    monkeypatch.setitem(cli.COMMANDS, "fake", ("fake_top", "a fake command"))
    repo = make_repo(tmp_path / "repo")
    assert cli.main(["dump", "fake", "--repo-root", str(repo), "--flag", "x"]) == 7
    assert cli.main(["fake", "--repo-root", str(repo), "--flag", "y"]) == 7
    assert calls == [("x", repo.resolve(), "service_backend"), ("y", repo.resolve(), "service_backend")]


def test_a_command_with_a_bad_config_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list = []
    fake_command(monkeypatch, "fake_top", calls)
    monkeypatch.setitem(cli.COMMANDS, "fake", ("fake_top", "a fake command"))
    assert cli.main(["fake", "--repo-root", str(tmp_path)]) == 2
    assert calls == []
    assert "cannot read the config" in capsys.readouterr().err
