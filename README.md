# stack-intel

Static analysis of a FastAPI backend and its TypeScript frontend, served to coding agents over MCP.

It answers the questions general code search cannot:

- which FastAPI dependencies guard a route, and which routes a dependency reaches;
- which backend handler a frontend API call hits, and which calls a handler has;
- where a React component is rendered;
- which definition a same-name symbol means;
- which docs describe a file, route or symbol, and whether they still match the code;
- which routes and dependencies installed libraries add (a fastapi-users router, `current_user(...)`), through [a committed capture](https://github.com/ADolkun/stack-intel/blob/main/docs/analysis.md#library-routers-and-dependencies) of the running app.

The same analyses check the docs and the agent instruction files against the code in CI (`stack-intel doc-check`), check themselves against the source and against the running app (`verify`, `runtime-check`), and report their own counts (`summary`).

Every repository-specific fact (the backend package and app, the frontend root, how the frontend calls the API, which docs to check) comes from one config file in the analyzed repository, `.stack-intel/config.toml`. Nothing else is assumed.

Contents: [Install](#install) · [Quickstart](#quickstart) · [Requirements](#requirements) · [Documentation](#documentation) · [License](#license)

## Install

The package is `stack-intel` on PyPI. To try it, or to use it across repositories:

```sh
uv tool install stack-intel             # the stack-intel command, on PATH
uvx stack-intel@<version> <command>     # one run, nothing installed
```

A repository that uses it pins the version in one file, `.stack-intel/requirements.txt`, so every caller runs the same release:

```
stack-intel==<version>
```

Every caller (the MCP client, CI, a git hook) then uses the same launcher from the repository root:

```sh
uv run --no-project --python 3.14 --with-requirements .stack-intel/requirements.txt stack-intel <command>
```

The frontend analysis installs the TypeScript it needs into a user cache on first use; `stack-intel setup` does it ahead of time, for CI. Pinning a commit that is not yet released, the MCP server's environment and the other details are in [Install](https://github.com/ADolkun/stack-intel/blob/main/docs/install.md).

## Quickstart

### 1. Write the config

`.stack-intel/config.toml` configures any of `[backend]`, `[frontend]`, `[docs]` and `[agents]`, and needs at least one. Each tool answers for the tables the config has. Every path is relative to the repository root.

A full stack, with a FastAPI backend, a TypeScript frontend that calls it through an axios instance, and docs:

```toml
[backend]
root = "backend"                       # import root: the directory holding the package
package = "app"
app = "app.main:app"
lock = "backend/uv.lock"

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.app.json"

[[frontend.clients]]
kind = "instance"                      # api.get(url), api.post(url, …), …
module = "frontend/src/lib/api.ts"
export = "api"
base_path = "/api"

[docs]
scope = ["README.md", "docs/**/*.md"]
route_prefixes = ["/api"]
```

A backend-only API service:

```toml
[backend]
root = "."
package = "app"
app = "app.main:app"

[docs]
scope = ["README.md", "docs/**/*.md"]
```

Docs only, which needs neither Node nor TypeScript:

```toml
[docs]
scope = ["README.md", "docs/**/*.md"]
```

Every key, the other API client kinds, frontend pages, docs rules and ignore entries, and the `[agents]` table are in [Configuration](https://github.com/ADolkun/stack-intel/blob/main/docs/configuration.md).

### 2. Check the config

```sh
stack-intel config-check
```

It prints `ok: <path>` and the configured tables, or an error that names the key and the file.

### 3. Check the docs

```sh
stack-intel doc-check
```

Every link and anchor must resolve, every repository path, backend route and code symbol a doc names must still exist, and every `package.json` script, make target and mise task it runs must be defined. The agent instruction files Claude Code and GitHub Copilot load are checked too, with or without `[docs]`. Each finding is one `path:line: message` line; the exit code is 0 with no findings, 1 with findings and 2 when the check could not run or is incomplete. Run it on every pull request. What it checks and how to fix a finding: [Docs check](https://github.com/ADolkun/stack-intel/blob/main/docs/docs-check.md).

### 4. Register the MCP server

In the repository's `.mcp.json`:

```json
{
  "mcpServers": {
    "stack-intel": {
      "command": "git",
      "args": [
        "-c",
        "alias.stack-intel-mcp=!exec uv run --no-project --python 3.14 --with-requirements .stack-intel/requirements.txt stack-intel mcp",
        "stack-intel-mcp"
      ]
    }
  }
}
```

The git alias starts the server from the top level of the work tree, wherever the client was started. The tools are `di_deps`, `di_reverse`, `route_match`, `route_unmatched`, `jsx_usage`, `symbol_resolve`, `doc_refs`, `status` and `refresh`; see [Tools](https://github.com/ADolkun/stack-intel/blob/main/docs/tools.md). After changing the pin, restart the server in your client.

## Requirements

- **macOS or Linux.** Windows is not supported.
- **Python 3.14** and [uv](https://docs.astral.sh/uv/). The backend analysis needs an interpreter at least as new as the backend's.
- **git** on `PATH`: only files git sees (tracked, or untracked and not ignored) count, so a local run gives CI's verdict.
- **Node.js 18 or newer** and **TypeScript 7.1 or newer** for the frontend analysis only. TypeScript is installed on first use into a cache shared by every checkout. A config without `[frontend]` never needs either.

The details, and what keeps working without Node or TypeScript: [Requirements](https://github.com/ADolkun/stack-intel/blob/main/docs/install.md#requirements).

## Documentation

The full reference lives in [`docs/`](https://github.com/ADolkun/stack-intel/blob/main/docs/README.md):

| Page | Covers |
| --- | --- |
| [Install](https://github.com/ADolkun/stack-intel/blob/main/docs/install.md) | Requirements, pinning, the launcher, the MCP client entry, the TypeScript install, environment variables, worktrees |
| [Commands](https://github.com/ADolkun/stack-intel/blob/main/docs/commands.md) | Every command, its options and exit codes |
| [Configuration](https://github.com/ADolkun/stack-intel/blob/main/docs/configuration.md) | `.stack-intel/config.toml` table by table, and partial configs |
| [Tools](https://github.com/ADolkun/stack-intel/blob/main/docs/tools.md) | The MCP tools and the response contract: error codes, diagnostic and limitation kinds |
| [Docs check](https://github.com/ADolkun/stack-intel/blob/main/docs/docs-check.md) | Reference kinds, statuses, rules, ignore entries and the agent instruction files |
| [Analysis notes](https://github.com/ADolkun/stack-intel/blob/main/docs/analysis.md) | How the analyses work, the library capture, freshness and the restart rule, performance |
| [Verification](https://github.com/ADolkun/stack-intel/blob/main/docs/verification.md) | `verify`, `summary`, `runtime-check`, and the tests and oracles |
| [Development](https://github.com/ADolkun/stack-intel/blob/main/docs/development.md) | Working on stack-intel, CI and releases |

Changes are listed in the [changelog](https://github.com/ADolkun/stack-intel/blob/main/CHANGELOG.md).

## License

MIT, see [LICENSE](https://github.com/ADolkun/stack-intel/blob/main/LICENSE).
