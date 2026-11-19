"""The JSONC reader, the three MCP server configs and Copilot's hook files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_repo_files import repo_files_on_disk
from fixture_repo import write

from stack_intel import agent_files
from stack_intel.agent_json import strip_jsonc
from stack_intel.config import AgentsConfig


def lines(root: Path) -> list[str]:
    report = agent_files.check(root, AgentsConfig(), repo_files_on_disk(root))
    return [f"{f.file}:{f.line}: {f.message}" for f in report.findings]


# ── JSONC ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('{"a": "http://x"} // trailing', {"a": "http://x"}),
        ('{"a": "/* not a comment */"}', {"a": "/* not a comment */"}),
        ('{"a": "// not a comment"}', {"a": "// not a comment"}),
        ('{"a": "say \\"hi\\" // still a string"}', {"a": 'say "hi" // still a string'}),
        ('{"a": "ends with a backslash\\\\"} // comment', {"a": "ends with a backslash\\"}),
        ('{\n  // a comment\n  "a": 1, /* block */ "b": [1, 2,],\n}', {"a": 1, "b": [1, 2]}),
        ('{"a": 1, /* multi\nline */ "b": 2 ,\n}', {"a": 1, "b": 2}),
        ('{"a": [1, // one\n 2, // two\n],}', {"a": [1, 2]}),
        ('{"a": ",}"}', {"a": ",}"}),
        ('{"a": 1 /* unclosed', None),
    ],
    ids=[
        "url",
        "block-in-string",
        "line-in-string",
        "escaped-quotes",
        "escaped-backslash",
        "comments-and-trailing-commas",
        "multi-line-block",
        "comment-before-bracket",
        "comma-brace-in-string",
        "unclosed-block",
    ],
)
def test_strip_jsonc(text: str, value: object) -> None:
    stripped = strip_jsonc(text)
    assert stripped.count("\n") == text.count("\n")
    if value is None:
        with pytest.raises(json.JSONDecodeError):
            json.loads(stripped)
    else:
        assert json.loads(stripped) == value


# ── .mcp.json ────────────────────────────────────────────────────────────

VALID_MCP = """\
{
  "mcpServers": {
    "local": {"command": "uv", "args": ["run", "server"], "env": {"TOKEN": "${TOKEN:-x}"}},
    "typed": {"type": "stdio", "command": "npx"},
    "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {"A": "b"}},
    "events": {"type": "sse", "url": "https://example.com/sse"},
    "socket": {"type": "ws", "url": "wss://example.com/ws"},
    "both": {"command": "uv", "url": "https://example.com/mcp"},
    "future": {"type": "grpc", "endpoint": "x"}
  }
}
"""


def test_a_valid_mcp_json_has_no_findings(tmp_path: Path) -> None:
    write(tmp_path / ".mcp.json", VALID_MCP)
    assert lines(tmp_path) == []


NO_SERVERS = (
    ".mcp.json:1: the file must be a JSON object with an 'mcpServers' object; Claude Code finds no servers in it"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "{",
            ".mcp.json:1: invalid JSON: Expecting property name enclosed in double quotes; Claude Code cannot read the file",
        ),
        (
            '{\n  "mcpServers": {\n    "a": {"command": "x"},\n  }\n}\n',
            ".mcp.json:3: invalid JSON: Illegal trailing comma before end of object; Claude Code cannot read the file",
        ),
        ("[]", NO_SERVERS),
        ('{"mcpServers": []}', NO_SERVERS),
        ('{"servers": {}}', NO_SERVERS),
    ],
    ids=["unclosed", "trailing-comma", "array", "servers-not-an-object", "vscode-key"],
)
def test_an_mcp_json_that_is_not_an_object_with_servers_is_reported(tmp_path: Path, text: str, expected: str) -> None:
    write(tmp_path / ".mcp.json", text)
    assert lines(tmp_path) == [expected]


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ('"uv"', "must be a JSON object"),
        ("null", "must be a JSON object"),
        ("{}", "needs a non-blank string 'command'"),
        ('{"args": ["x"]}', "needs a non-blank string 'command'"),
        ('{"command": "  "}', "needs a non-blank string 'command'"),
        ('{"command": 3}', "needs a non-blank string 'command'"),
        (
            '{"url": "https://example.com/mcp"}',
            'has a url but no type; Claude Code reads it as a stdio server and skips it (add "type": "http")',
        ),
        ('{"type": "stdio"}', "of type 'stdio' needs a non-blank string 'command'"),
        ('{"type": "http"}', "of type 'http' needs a non-blank string 'url'"),
        ('{"type": "streamable-http", "url": ""}', "of type 'streamable-http' needs a non-blank string 'url'"),
        ('{"type": "sse", "command": "x"}', "of type 'sse' needs a non-blank string 'url'"),
        ('{"command": "x", "args": "run"}', "has 'args' that is not a list of strings"),
        ('{"command": "x", "args": ["run", 3]}', "has 'args' that is not a list of strings"),
        ('{"command": "x", "env": ["A=1"]}', "has 'env' that is not an object"),
        ('{"type": "http", "url": "u", "headers": ["A: b"]}', "has 'headers' that is not an object"),
        ('{"type": "grpc", "args": "x"}', "has 'args' that is not a list of strings"),
    ],
)
def test_each_server_rule_is_reported_at_the_servers_line(tmp_path: Path, server: str, message: str) -> None:
    write(
        tmp_path / ".mcp.json",
        f'{{\n  "mcpServers": {{\n    "ok": {{"command": "uv"}},\n    "bad": {server}\n  }}\n}}\n',
    )
    assert lines(tmp_path) == [f".mcp.json:4: MCP server 'bad' {message}"]


def test_an_empty_mcp_servers_object_is_valid(tmp_path: Path) -> None:
    write(tmp_path / ".mcp.json", '{"mcpServers": {}}')
    assert lines(tmp_path) == []


def test_a_server_named_like_a_key_is_found_after_the_servers_key(tmp_path: Path) -> None:
    text = '{\n  "docs": "see mcpServers",\n  "mcpServers": {\n    "docs": {"url": "u"}\n  }\n}\n'
    write(tmp_path / ".mcp.json", text)
    assert [line.split(": ", 1)[0] for line in lines(tmp_path)] == [".mcp.json:4"]


# ── .github/mcp.json ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        '{"mcpServers": {"a": {"command": "x"}}}',
        '{"a": {"command": "x"}, "b": {"type": "http", "url": "u", "tools": ["*"]}}',
        "{}",
    ],
    ids=["mcp-servers", "bare-map", "empty"],
)
def test_a_valid_copilot_mcp_json_has_no_findings(tmp_path: Path, text: str) -> None:
    write(tmp_path / ".github/mcp.json", text)
    assert lines(tmp_path) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[1]", ".github/mcp.json:1: the file must be a JSON object"),
        ('{\n"mcpServers": 3}', ".github/mcp.json:2: 'mcpServers' must be an object"),
        (
            '{\n  "docs": {"url": "u"}\n}',
            ".github/mcp.json:2: MCP server 'docs' has a url but no type; Copilot CLI reads it as a local server "
            'and skips it (add "type": "http")',
        ),
        (
            '{"mcpServers": {\n"a": {"type": "local"}}}',
            ".github/mcp.json:2: MCP server 'a' of type 'local' needs a non-blank string 'command'",
        ),
        (
            "{\n",
            ".github/mcp.json:2: invalid JSON: Expecting property name enclosed in double quotes; Copilot CLI cannot read the file",
        ),
    ],
    ids=["not-an-object", "servers-not-an-object", "typeless-url", "local-without-command", "invalid"],
)
def test_a_broken_copilot_mcp_json_is_reported(tmp_path: Path, text: str, expected: str) -> None:
    write(tmp_path / ".github/mcp.json", text)
    assert lines(tmp_path) == [expected]


# ── .vscode/mcp.json ─────────────────────────────────────────────────────

VALID_VSCODE = """\
{
  // Servers for this workspace.
  "inputs": [{"type": "promptString", "id": "token", "description": "API token", "password": true}],
  "servers": {
    "local": {"type": "stdio", "command": "npx", "args": ["server"], "env": {"TOKEN": "${input:token}"},},
    "remote": {"type": "http", "url": "https://example.com/mcp", /* header */ "headers": {"A": "${input:token}"}},
  },
}
"""


def test_a_valid_vscode_mcp_json_has_no_findings(tmp_path: Path) -> None:
    write(tmp_path / ".vscode/mcp.json", VALID_VSCODE)
    assert lines(tmp_path) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{\n  "mcpServers": {}\n}', ".vscode/mcp.json:2: VS Code reads servers from 'servers', not 'mcpServers'"),
        ('{\n  "servers": []\n}', ".vscode/mcp.json:2: 'servers' must be an object"),
        (
            '{\n  "servers": {\n    "a": {"type": "stdio", "command": "x", "env": {"T": "${input:token}"}}\n  }\n}',
            ".vscode/mcp.json:3: MCP server 'a' uses ${input:token} but 'inputs' has no entry with id 'token'",
        ),
        (
            '{\n  "servers": {\n    "a": {"type": "http"}\n  }\n}',
            ".vscode/mcp.json:3: MCP server 'a' of type 'http' needs a non-blank string 'url'",
        ),
        ("[1, // x\n2,]", ".vscode/mcp.json:1: the file must be a JSON object"),
        (
            '{"servers": {',
            ".vscode/mcp.json:1: invalid JSON: Expecting property name enclosed in double quotes; VS Code cannot read the file",
        ),
    ],
    ids=[
        "mcp-servers-key",
        "servers-not-an-object",
        "undeclared-input",
        "http-without-url",
        "array",
        "invalid",
    ],
)
def test_a_broken_vscode_mcp_json_is_reported(tmp_path: Path, text: str, expected: str) -> None:
    write(tmp_path / ".vscode/mcp.json", text)
    assert lines(tmp_path) == [expected]


def test_a_vscode_file_without_servers_is_not_reported(tmp_path: Path) -> None:
    write(tmp_path / ".vscode/mcp.json", '{"inputs": []}')
    assert lines(tmp_path) == []


@pytest.mark.parametrize(
    "text",
    ['{"servers": {"a": {"command": "x"}, "b": {"url": "https://example.com/mcp"}}}'],
)
def test_vscode_infers_a_missing_type(tmp_path: Path, text: str) -> None:
    write(tmp_path / ".vscode/mcp.json", text)
    assert lines(tmp_path) == []


@pytest.mark.parametrize("rel", [".mcp.json", ".vscode/mcp.json", ".github/mcp.json"])
@pytest.mark.parametrize("text", ["", "  \n\t\n"])
def test_a_blank_mcp_file_is_not_reported(tmp_path: Path, rel: str, text: str) -> None:
    write(tmp_path / rel, text)
    assert lines(tmp_path) == []


def test_an_mcp_json_with_no_keys_is_not_reported(tmp_path: Path) -> None:
    write(tmp_path / ".mcp.json", "{}")
    assert lines(tmp_path) == []


def test_a_number_the_parser_cannot_hold_is_skipped(tmp_path: Path) -> None:
    write(tmp_path / ".github/hooks/h.json", '{"version": ' + "9" * 5001 + ', "hooks": {}}')
    assert lines(tmp_path) == []


@pytest.mark.parametrize(
    ("rel", "text", "parsed"),
    [
        (
            ".mcp.json",
            '{"mcpServers": {"a": {"command": "x", "args": ' + "[" * 100_000 + "]" * 100_000 + "}}}",
            [".mcp.json:1: MCP server 'a' has 'args' that is not a list of strings"],
        ),
        (".vscode/mcp.json", "[" * 100_000 + "]" * 100_000, [".vscode/mcp.json:1: the file must be a JSON object"]),
    ],
    ids=["deep-args", "deep-vscode"],
)
def test_deep_nesting_does_not_crash_the_check(tmp_path: Path, rel: str, text: str, parsed: list[str]) -> None:
    write(tmp_path / rel, text)
    # Whether the parser reaches its recursion limit depends on the platform's stack.
    assert lines(tmp_path) in ([], parsed)


# ── .github/hooks ────────────────────────────────────────────────────────

HOOKS = ".github/hooks/guard.json"


@pytest.mark.parametrize(
    "text",
    [
        '{"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "./x.sh"}]}}',
        '{"version": 1, "hooks": {"sessionStart": [{"type": "bogus"}]}}',
        '{"version": 1}',
        '{"hooks": {"preToolUse": []}}',
        '{"version": 1, "disableAllHooks": false, "hooks": {}}',
    ],
    ids=["valid", "bad-item-only-drops-the-item", "no-hooks", "no-version", "other-keys"],
)
def test_hook_files_copilot_loads_are_not_reported(tmp_path: Path, text: str) -> None:
    write(tmp_path / HOOKS, text)
    assert lines(tmp_path) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{\n"version": 2, "hooks": {}}', f"{HOOKS}:2: 'version' must be 1; Copilot rejects the whole file"),
        ('{"version": "1", "hooks": {}}', f"{HOOKS}:1: 'version' must be 1; Copilot rejects the whole file"),
        ('{"version": true, "hooks": {}}', f"{HOOKS}:1: 'version' must be 1; Copilot rejects the whole file"),
        ('{"version": 1,\n "hooks": []}', f"{HOOKS}:2: 'hooks' must be an object; Copilot rejects the whole file"),
        (
            '{"version": 1, "hooks": {\n"preToolUse": {"type": "command"}}}',
            f"{HOOKS}:2: hook event 'preToolUse' must be an array; Copilot rejects the whole file",
        ),
        ("[]", f"{HOOKS}:1: the file must be a JSON object; Copilot rejects the whole file"),
        (
            "{",
            f"{HOOKS}:1: invalid JSON: Expecting property name enclosed in double quotes; Copilot cannot read the file",
        ),
    ],
    ids=["version-2", "version-string", "version-bool", "hooks-array", "event-not-array", "array", "invalid"],
)
def test_a_hook_file_copilot_rejects_is_reported(tmp_path: Path, text: str, expected: str) -> None:
    write(tmp_path / HOOKS, text)
    assert lines(tmp_path) == [expected]


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        (".vscode/mcp.json", '{"servers":{"a":{"type":"stdio","command":"x","env":{"PORT":8080,"N":null}}}}'),
        (".mcp.json", '{"mcpServers":{"a":{"command":"x","env":{"PORT":8080,"N":null},"headers":{"X":1}}}}'),
        (".github/mcp.json", '{"a":{"command":"x","env":{"PORT":8080,"N":null}}}'),
    ],
)
def test_env_and_header_values_of_any_type_are_not_reported(tmp_path: Path, rel: str, text: str) -> None:
    write(tmp_path / rel, text)
    assert lines(tmp_path) == []


def test_a_server_line_is_its_own_key_not_a_same_named_key_in_an_earlier_server(tmp_path: Path) -> None:
    text = '{"mcpServers": {\n  "a": {"command": "x", "env": {"b": "1"}},\n  "b": {"url": "u"}\n}}\n'
    write(tmp_path / ".mcp.json", text)
    assert [line.split(": ", 1)[0] for line in lines(tmp_path)] == [".mcp.json:3"]


def test_a_hook_event_line_is_its_own_key(tmp_path: Path) -> None:
    text = '{"version": 1, "notes": {"preToolUse": []},\n "hooks": {\n  "preToolUse": {}}}\n'
    write(tmp_path / ".github/hooks/h.json", text)
    assert [line.split(": ", 1)[0] for line in lines(tmp_path)] == [".github/hooks/h.json:3"]


def test_a_repeated_server_name_is_reported_where_the_winning_value_is(tmp_path: Path) -> None:
    text = '{"mcpServers": {\n  "a": {"command": "x"},\n  "b": {"command": "y"},\n  "a": {"url": "u"}\n}}\n'
    write(tmp_path / ".mcp.json", text)
    assert [line.split(": ", 1)[0] for line in lines(tmp_path)] == [".mcp.json:4"]
