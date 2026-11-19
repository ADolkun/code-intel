# Install

## Requirements

- **macOS or Linux.** Windows is not supported.
- **Python 3.14** and [uv](https://docs.astral.sh/uv/). The backend analysis parses the analyzed code with the running interpreter, so it must be at least the version the backend declares (`backend.python_version_file`); see [Old interpreter](analysis.md#old-interpreter). The package depends on `mcp`, `markdown-it-py` and `PyYAML`; `uv` installs them.
- **git** on `PATH`. Whether a path exists is decided by git (tracked, or untracked and not ignored), never by the filesystem, so a local checkout full of generated or ignored files answers like a clean CI checkout. Every walk (backend, frontend, symbols, docs) is limited to the same git-visible files. A git failure is reported by name; there is no filesystem fallback.
- **Node.js 18 or newer** on `PATH` (or named by `STACK_INTEL_NODE`, a path or a command name) for the frontend analysis. MCP clients started from a GUI often lack the version manager's `PATH`, so set `STACK_INTEL_NODE` in the server entry's `env`. A relative `STACK_INTEL_NODE` containing a directory separator resolves against the repository root.
- **TypeScript 7.1 or newer** for the frontend analysis, installed once per machine, on first use or by [`stack-intel setup`](#install-and-launch), into a user cache shared by every checkout and repository. The helper has its own pinned copy (`7.1.0-beta`, in the package's `ts/package.json` and lockfile) because it calls the compiler's JavaScript API, which TypeScript 7.0 does not export: 7.1 adds it at stable paths (`typescript/sync`, `typescript/ast`). When 7.1 is released, the pin becomes a version bump. `STACK_INTEL_TYPESCRIPT_DIR` names another TypeScript package directory instead (a relative value resolves against the repository root). A TypeScript older than 7.1 is refused with a `typescript_unsupported` problem that names the fix.

Without Node or TypeScript:

- these keep working: `di_deps` and `di_reverse` fully; `route_match` with the backend side only, marked `complete: false` with a `frontend_unavailable` diagnostic and without any orphan claim (a query lists the backend routes that fit under `Frontend not analyzed: <problem>. Fix: <fix>`, and every frontend-derived count is `null`); `symbol_resolve` with `side="backend"` (with `side="both"` the backend candidates are marked incomplete); `doc_refs` and `doc-check`, except that frontend definitions are not indexed and a bare path that only a React Router page could satisfy is `not_checked` (limitations `frontend_definitions_unavailable` and `frontend_pages_unavailable`);
- these fail with a `frontend_unavailable` error naming the missing path and the fix: `jsx_usage`, `route_unmatched` (on every side, because without the frontend every backend route would look unused) and `symbol_resolve` with `side="frontend"`.

A helper that crashes while Node and TypeScript are present is reported the same way with `problem: "helper_failed"`, and `status` shows the toolchain as `helper_failed` until a run in the same mode succeeds. A crash in `facts` mode (JSX usage, frontend symbols, pages) and one in `routes` mode (route matching) are tracked apart.

A config without `[frontend]` never needs Node or TypeScript: nothing probes or installs them, and `status` reports the toolchain as `not_configured`. Every tool that needs a table the config lacks answers with a [`not_configured`](tools.md#response-contract) error instead (see [Partial configs](configuration.md#partial-configs)).

## Install and launch

The package is `stack-intel` on PyPI. To try it, or to use it across repositories:

```sh
uv tool install stack-intel             # the stack-intel command, on PATH
uvx stack-intel@<version> <command>     # one run, nothing installed
```

A repository that uses it pins the version in one file, `.stack-intel/requirements.txt`, so every caller runs the same release:

```
stack-intel==0.3.0
```

A commit not yet released is pinned by its full SHA instead: `stack-intel @ git+https://github.com/ADolkun/stack-intel@<full commit sha>`. A full SHA is immutable, so `uv` serves later launches from its cache without fetching.

Hashes in this file would give no protection: `uv run --with-requirements` installs from it without verifying `--hash` lines (checked with uv 0.13.9).

Every caller (the MCP client, CI, a git hook) uses the same launcher from the repository root:

```sh
uv run --no-project --python 3.14 --with-requirements .stack-intel/requirements.txt stack-intel <command>
```

`--no-project` keeps the launch independent of whatever project the directory belongs to; `--python 3.14` is needed because `uv` only finds a repository's `.python-version` when it runs inside it.

**TypeScript install, automatic on first use.** A command or tool that needs the frontend and finds no TypeScript installs it itself, the way `stack-intel setup` does: it copies the packaged `ts/package.json` and `ts/package-lock.json` to `${STACK_INTEL_CACHE_DIR:-~/.cache/stack-intel}/ts/<lock digest>/` and runs `npm ci --ignore-scripts` there (npm is the only code it runs). It needs Node.js with npm on `PATH` and network access to the npm registry, once.

- A **command** installs first, printing one line to standard error (`stack-intel: installing TypeScript into … (first use, one time)`), and then runs.
- The **MCP server** installs in a background thread and does not block the reply. Until the install finishes, a frontend answer carries `problem: "frontend_provisioning"` (an error body, or the `frontend_unavailable` diagnostic of a partial answer, plus a `frontend_provisioning` [limitation](tools.md#limitation-kinds)) saying TypeScript will be ready shortly; backend tools are unaffected. The next call after the install finishes answers in full.
- The install is built in a staging directory and renamed into place under a lock file (`ts/.<digest>.lock`, `flock`), so a failed or killed run leaves nothing half-installed, and several servers (worktrees) and commands at once take turns: the one that waits finds the winner's install and does not call npm.
- A process makes one attempt per lock digest. When it fails (no `node` or `npm`, npm offline), the answer is the usual `frontend_unavailable`, and its fix names the cause and says to run `stack-intel setup` to retry.
- `STACK_INTEL_TYPESCRIPT_DIR` is an explicit TypeScript location and is never provisioned over: with it set, nothing is installed. `STACK_INTEL_NODE` only picks the `node` to run, so a missing TypeScript is still installed; the install uses the `npm` in the same directory as that node when there is one (with the directory first on `PATH`), else `npm` on `PATH`. `STACK_INTEL_NO_AUTO_SETUP=1` restores the earlier behaviour, a missing TypeScript being reported with the `setup` fix.

**Prewarming.** `stack-intel setup` does the same install on demand, for CI or a machine that is about to go offline. It needs no repository and no config, and a second run finds the install and does not call npm (`ok: typescript 7.1.0-beta already installed at …`). Every checkout, worktree and repository shares the install; the tool needs no `npm ci` per worktree.

**MCP client entry.** Register the server in the repository's `.mcp.json`:

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

The entry runs the server through a git shell alias because git runs a shell alias from the top level of the work tree on every platform, while a client started in a subdirectory (for example in `frontend/`) can load the root file and then starts the server from that subdirectory, where the relative requirements path does not exist. The server serves the repository that contains the current directory; `--repo-root PATH` names another. Clients show the tools as `mcp__stack-intel__<tool>` (Claude Code) or under the server name `stack-intel`.

MCP clients do not reliably pass your shell environment to the servers they start; the Python SDK's stdio client passes only a minimal default set. To point the server at a Node that is not on the client's `PATH`, or at a TypeScript outside the cache, add an `env` object to the entry:

```json
"env": {
  "STACK_INTEL_NODE": "/absolute/path/to/node",
  "STACK_INTEL_TYPESCRIPT_DIR": "/absolute/path/to/node_modules/typescript"
}
```

`status` shows the Node and TypeScript the server actually found.

**After changing the pin, restart the MCP server in your client**; see [Freshness and the restart rule](analysis.md#freshness-and-the-restart-rule).

Environment variables:

| Variable | Meaning |
| --- | --- |
| `STACK_INTEL_NODE` | The `node` binary (a path or a command name). Default: the first `node` on `PATH`. The [automatic TypeScript install](#install-and-launch) uses the `npm` beside it. |
| `STACK_INTEL_TYPESCRIPT_DIR` | A TypeScript package directory used instead of the cache install. Setting it turns off the automatic install. |
| `STACK_INTEL_CACHE_DIR` | Where the automatic install and `setup` put TypeScript. Default: `~/.cache/stack-intel`. |
| `STACK_INTEL_NO_AUTO_SETUP` | Set to `1` to turn off the automatic TypeScript install: a missing TypeScript is reported as `frontend_unavailable`, as before 0.2.1. |
| `STACK_INTEL_REQUIRE_TOOLCHAIN` | Set to `1` in the test suite: a test that needs Node, TypeScript, git or FastAPI fails instead of skipping when it is missing. |

## Worktrees

The MCP server answers for the checkout it was started in; `status` shows it as `repo_root`. An agent working in another worktree of the same repository still gets that checkout's answers from it, so:

- run the commands from the worktree instead (`dump di`, `dump routes`, `dump jsx`, `dump symbols`, `doc-check`), with the same launcher. Each run starts cold, a few seconds on a large repository (see [Performance](analysis.md#performance));
- or start the client session in the worktree: the `.mcp.json` entry then starts a server of its own there.

`doc_refs` has no command equivalent yet, so the docs lookup is available only from a server started in the worktree.
