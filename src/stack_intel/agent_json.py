"""The agents' JSON files: the three MCP server configs and Copilot's hooks.

- ``.mcp.json`` (Claude Code, Copilot CLI): an object with an ``mcpServers`` object.
- ``.github/mcp.json`` (Copilot CLI): ``mcpServers``, or a bare map of servers.
- ``.vscode/mcp.json`` (VS Code, JSONC): a ``servers`` object, every server
  with a ``type``, every ``${input:ID}`` declared in ``inputs``.
- ``.github/hooks/*.json`` (Copilot CLI and cloud): ``version`` 1 and a
  ``hooks`` object of arrays; Copilot rejects the whole file otherwise.

Every server is an object; a typeless or ``stdio``/``local`` one needs a
``command``, a typeless one with only a ``url`` is read as stdio and skipped,
a remote one needs a ``url``; ``args`` is a list of strings and ``env`` and
``headers`` are objects (VS Code also takes numbers and null as values). An
unknown ``type`` gets only the checks that do not depend on it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from stack_intel.agent_frontmatter import finding, is_blank
from stack_intel.doc_report import Finding

CLAUDE_MCP = ".mcp.json"
COPILOT_MCP = ".github/mcp.json"
VSCODE_MCP = ".vscode/mcp.json"
HOOKS_DIR = ".github/hooks"

_LOCAL_TYPES = frozenset({"stdio", "local"})
_REMOTE_TYPES = frozenset({"http", "streamable-http", "sse", "ws"})
_INPUT_REF = re.compile(r"\$\{input:([^}]*)\}")


def strip_jsonc(text: str) -> str:
    """``text`` with ``//`` and ``/* */`` comments and trailing commas blanked
    out, strings untouched and every line kept where it was."""
    out = list(text)
    i, in_string = 0, False
    while i < len(out):
        char = out[i]
        if in_string:
            if char == "\\":
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif text.startswith("//", i):
            while i < len(out) and out[i] != "\n":
                out[i] = " "
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            stop = len(out) if end < 0 else end + 2
            for j in range(i, stop):
                if out[j] != "\n":
                    out[j] = " "
            i = stop
            continue
        i += 1
    blanked = "".join(out)
    i, in_string = 0, False
    while i < len(out):
        char = out[i]
        if in_string:
            if char == "\\":
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == ",":
            after = blanked[i + 1 :].lstrip()
            if after[:1] in ("}", "]"):
                out[i] = " "
        i += 1
    return "".join(out)


def _string_end(text: str, start: int) -> int:
    """The offset just past the JSON string opening at ``start``."""
    i = start + 1
    while text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _key_lines(text: str) -> dict[tuple[str, ...], int]:
    """The line of each object key, by its path of keys from the root; a
    repeated key gets the line of its last occurrence, the value ``json``
    keeps. Keys inside arrays are not recorded. ``text`` is valid JSON, so no
    string holds a raw newline and one pass counts the lines."""
    lines: dict[tuple[str, ...], int] = {}
    # Per open container: the key path to it (None inside an array), whether it
    # is an object expecting a key next, and its latest key.
    paths: list[tuple[str, ...] | None] = []
    expecting: list[bool] = []
    latest: list[str | None] = []
    line, i = 1, 0
    while i < len(text):
        char = text[i]
        if char == '"':
            end = _string_end(text, i)
            if expecting and expecting[-1]:
                key = json.loads(text[i:end])
                latest[-1] = key
                expecting[-1] = False
                if paths[-1] is not None:
                    lines[(*paths[-1], key)] = line
            i = end
            continue
        if char == "\n":
            line += 1
        elif char in "{[":
            path: tuple[str, ...] | None = None
            if char == "{" and not paths:
                path = ()
            elif char == "{" and (parent := paths[-1]) is not None and (key := latest[-1]) is not None:
                path = (*parent, key)
            paths.append(path)
            expecting.append(char == "{")
            latest.append(None)
        elif char in "}]":
            paths.pop()
            expecting.pop()
            latest.pop()
        elif char == "," and expecting:
            expecting[-1] = latest[-1] is not None
        i += 1
    return lines


@dataclass(frozen=True)
class _Source:
    rel: str
    lines: dict[tuple[str, ...], int]

    def line(self, *path: str) -> int:
        """The line of the key at ``path``; 1 when not found."""
        return self.lines.get(path, 1)


def _load(rel: str, text: str, findings: list[Finding], *, jsonc: bool, reader: str) -> tuple[object, _Source] | None:
    parsed = strip_jsonc(text) if jsonc else text
    try:
        return json.loads(parsed), _Source(rel, _key_lines(parsed))
    except json.JSONDecodeError as exc:
        findings.append(finding(rel, exc.lineno, rel, f"invalid JSON: {exc.msg}; {reader} cannot read the file"))
        return None


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_server(
    source: _Source, name: str, server: object, line: int, findings: list[Finding], *, typeless_url: str | None
) -> None:
    """``typeless_url``: why a typeless server with only a ``url`` is skipped,
    or None where a missing ``type`` is reported on its own."""
    rel = source.rel

    def report(message: str) -> None:
        findings.append(finding(rel, line, name, f"MCP server {name!r} {message}"))

    if not isinstance(server, dict):
        report("must be a JSON object")
        return
    kind = server.get("type")
    if "type" not in server:
        if "url" in server and is_blank(server.get("command")):
            if typeless_url is not None:
                report(f'has a url but no type; {typeless_url} (add "type": "http")')
        elif is_blank(server.get("command")):
            report("needs a non-blank string 'command'")
    elif kind in _LOCAL_TYPES:
        if is_blank(server.get("command")):
            report(f"of type {kind!r} needs a non-blank string 'command'")
    elif kind in _REMOTE_TYPES and is_blank(server.get("url")):
        report(f"of type {kind!r} needs a non-blank string 'url'")
    if "args" in server and not _is_string_list(server["args"]):
        report("has 'args' that is not a list of strings")
    for key in ("env", "headers"):
        if key in server and not isinstance(server[key], dict):
            report(f"has {key!r} that is not an object")


def _check_servers(
    source: _Source, servers: dict[str, object], container: str | None, findings: list[Finding], typeless_url: str
) -> None:
    for name, server in servers.items():
        line = source.line(name) if container is None else source.line(container, name)
        _check_server(source, name, server, line, findings, typeless_url=typeless_url)


def check_claude_mcp(text: str, findings: list[Finding]) -> None:
    loaded = _load(CLAUDE_MCP, text, findings, jsonc=False, reader="Claude Code")
    if loaded is None:
        return
    data, source = loaded
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        findings.append(
            finding(CLAUDE_MCP, 1, "mcpServers", "the file must be a JSON object with an 'mcpServers' object")
        )
        return
    reason = "Claude Code reads it as a stdio server and skips it"
    _check_servers(source, servers, "mcpServers", findings, reason)


def check_copilot_mcp(text: str, findings: list[Finding]) -> None:
    loaded = _load(COPILOT_MCP, text, findings, jsonc=False, reader="Copilot CLI")
    if loaded is None:
        return
    data, source = loaded
    if not isinstance(data, dict):
        findings.append(finding(COPILOT_MCP, 1, COPILOT_MCP, "the file must be a JSON object"))
        return
    servers = data.get("mcpServers", data)
    if not isinstance(servers, dict):
        findings.append(finding(COPILOT_MCP, source.line("mcpServers"), "mcpServers", "'mcpServers' must be an object"))
        return
    reason = "Copilot CLI reads it as a local server and skips it"
    container = "mcpServers" if "mcpServers" in data else None
    _check_servers(source, servers, container, findings, reason)


def _input_refs(value: object) -> list[str]:
    if isinstance(value, str):
        return _INPUT_REF.findall(value)
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _input_refs(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _input_refs(item)]
    return []


def check_vscode_mcp(text: str, findings: list[Finding]) -> None:
    loaded = _load(VSCODE_MCP, text, findings, jsonc=True, reader="VS Code")
    if loaded is None:
        return
    data, source = loaded
    if not isinstance(data, dict):
        findings.append(finding(VSCODE_MCP, 1, VSCODE_MCP, "the file must be a JSON object"))
        return
    if "servers" not in data:
        if "mcpServers" in data:
            findings.append(
                finding(
                    VSCODE_MCP,
                    source.line("mcpServers"),
                    "mcpServers",
                    "VS Code reads servers from 'servers', not 'mcpServers'",
                )
            )
        return
    servers = data["servers"]
    if not isinstance(servers, dict):
        findings.append(finding(VSCODE_MCP, source.line("servers"), "servers", "'servers' must be an object"))
        return
    inputs = data.get("inputs")
    declared = {item.get("id") for item in inputs if isinstance(item, dict)} if isinstance(inputs, list) else set()
    for name, server in servers.items():
        line = source.line("servers", name)
        _check_server(source, name, server, line, findings, typeless_url=None)
        if not isinstance(server, dict):
            continue
        if "type" not in server:
            findings.append(finding(VSCODE_MCP, line, name, f"MCP server {name!r} has no type; VS Code requires one"))
        for ref in dict.fromkeys(_input_refs(server)):
            if ref not in declared:
                findings.append(
                    finding(
                        VSCODE_MCP,
                        line,
                        f"${{input:{ref}}}",
                        f"MCP server {name!r} uses ${{input:{ref}}} but 'inputs' has no entry with id {ref!r}",
                    )
                )


def check_hooks(rel: str, text: str, findings: list[Finding]) -> None:
    loaded = _load(rel, text, findings, jsonc=False, reader="Copilot")
    if loaded is None:
        return
    data, source = loaded
    rejected = "; Copilot rejects the whole file"
    if not isinstance(data, dict):
        findings.append(finding(rel, 1, rel, "the file must be a JSON object" + rejected))
        return
    version = data.get("version")
    if "version" in data and (isinstance(version, bool) or version != 1):
        findings.append(finding(rel, source.line("version"), "version", f"'version' must be 1{rejected}"))
    if "hooks" not in data:
        return
    hooks = data["hooks"]
    if not isinstance(hooks, dict):
        findings.append(finding(rel, source.line("hooks"), "hooks", f"'hooks' must be an object{rejected}"))
        return
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            line = source.line("hooks", event)
            findings.append(finding(rel, line, event, f"hook event {event!r} must be an array{rejected}"))
