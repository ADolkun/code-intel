# code-intel

Static analysis of a FastAPI backend and its TypeScript frontend, served to coding agents over MCP.

It answers the questions general code search cannot:

- which FastAPI dependencies guard a route, and which routes a dependency reaches;
- which backend handler a frontend API call hits, and which calls a handler has;
- where a React component is rendered;
- which definition a same-name symbol means;
- which docs describe a file, route or symbol, and whether they still match the code;
- which routes and dependencies installed libraries add (a fastapi-users router, `current_user(...)`), through [a committed capture](#library-routers-and-dependencies) of the running app.

The same analyses check the docs against the code in CI (`code-intel doc-check`), check themselves against the source and against the running app (`verify`, `runtime-check`), and report their own counts (`summary`).

Every repository-specific fact (the backend package and app, the frontend root, how the frontend calls the API, which docs to check) comes from one config file in the analyzed repository: [`.code-intel/config.toml`](#configuration). Nothing else is assumed.

Contents: [Requirements](#requirements) · [Install and launch](#install-and-launch) · [Commands](#commands) · [Configuration](#configuration) · [Tools](#tools) · [Response contract](#response-contract) · [Analysis notes](#analysis-notes) · [Docs check](#docs-check) · [Freshness and the restart rule](#freshness-and-the-restart-rule) · [Performance](#performance) · [Verification](#verification) · [Development](#development) · [License](#license)

## Requirements

- **Python 3.14** and [uv](https://docs.astral.sh/uv/). The backend analysis parses the analyzed code with the running interpreter, so it must be at least the version the backend declares (`backend.python_version_file`); see [Old interpreter](#old-interpreter). The package depends on `mcp` (>=2.3, <3), `markdown-it-py` (==4.2.0) and `PyYAML`; `uv` installs them.
- **git** on `PATH`. Whether a path exists is decided by git (tracked, or untracked and not ignored), never by the filesystem, so a local checkout full of generated or ignored files answers like a clean CI checkout. Every walk (backend, frontend, symbols, docs) is limited to the same git-visible files. A git failure is reported by name; there is no filesystem fallback.
- **Node.js 18 or newer** on `PATH` (or named by `CODE_INTEL_NODE`, a path or a command name) for the frontend analysis. MCP clients started from a GUI often lack the version manager's `PATH`, so set `CODE_INTEL_NODE` in the server entry's `env`. A relative `CODE_INTEL_NODE` containing a directory separator resolves against the repository root.
- **TypeScript 7.1 or newer** for the frontend analysis, installed once per machine by [`code-intel setup`](#install-and-launch) into a user cache and shared by every checkout and repository. The helper has its own pinned copy (`7.1.0-beta`, in the package's `ts/package.json` and lockfile) because it calls the compiler's JavaScript API, which TypeScript 7.0 does not export: 7.1 adds it at stable paths (`typescript/sync`, `typescript/ast`). When 7.1 is released, the pin becomes a version bump. `CODE_INTEL_TYPESCRIPT_DIR` names another TypeScript package directory instead (a relative value resolves against the repository root). A TypeScript older than 7.1 is refused with a `typescript_unsupported` problem that names the fix.

Without Node or TypeScript:

- these keep working: `di_deps` and `di_reverse` fully; `route_match` with the backend side only, marked `complete: false` with a `frontend_unavailable` diagnostic and without any orphan claim (a query lists the backend routes that fit under `Frontend not analyzed: <problem>. Fix: <fix>`, and every frontend-derived count is `null`); `symbol_resolve` with `side="backend"` (with `side="both"` the backend candidates are marked incomplete); `doc_refs` and `doc-check`, except that frontend definitions are not indexed and a bare path that only a React Router page could satisfy is `not_checked` (limitations `frontend_definitions_unavailable` and `frontend_pages_unavailable`);
- these fail with a `frontend_unavailable` error naming the missing path and the fix: `jsx_usage`, `route_unmatched` (on every side, because without the frontend every backend route would look unused) and `symbol_resolve` with `side="frontend"`.

A helper that crashes while Node and TypeScript are present is reported the same way with `problem: "helper_failed"`, and `status` shows the toolchain as `helper_failed` until a run in the same mode succeeds. A crash in `facts` mode (JSX usage, frontend symbols, pages) and one in `routes` mode (route matching) are tracked apart.

## Install and launch

A repository pins the tool by commit in one file, `.code-intel/requirements.txt`:

```
code-intel @ git+https://github.com/ADolkun/code-intel@<full commit sha>  # v0.2.0
```

A full SHA is immutable, so `uv` serves later launches from its cache without fetching. Every caller (the MCP client, CI, a git hook) uses the same launcher from the repository root:

```sh
uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel <command>
```

`--no-project` keeps the launch independent of whatever project the directory belongs to; `--python 3.14` is needed because `uv` only finds a repository's `.python-version` when it runs inside it.

**One-time TypeScript install.** `code-intel setup` copies the packaged `ts/package.json` and `ts/package-lock.json` to `${CODE_INTEL_CACHE_DIR:-~/.cache/code-intel}/ts/<lock digest>/` and runs `npm ci --ignore-scripts` there. It needs no repository and no config. The install is built in a staging directory and renamed into place, so a failed or killed run leaves nothing half-installed and two runs at once cannot corrupt each other. A second run finds the install and does not call npm (`ok: typescript 7.1.0-beta already installed at …`). Every checkout, worktree and repository shares the install; the tool needs no `npm ci` per worktree.

**MCP client entry.** Register the server in the repository's `.mcp.json`:

```json
{
  "mcpServers": {
    "code-intel": {
      "command": "git",
      "args": [
        "-c",
        "alias.code-intel-mcp=!exec uv run --no-project --python 3.14 --with-requirements .code-intel/requirements.txt code-intel mcp",
        "code-intel-mcp"
      ]
    }
  }
}
```

The entry runs the server through a git shell alias because git runs a shell alias from the top level of the work tree on every platform, while a client started in a subdirectory (for example in `frontend/`) can load the root file and then starts the server from that subdirectory, where the relative requirements path does not exist. The server serves the repository that contains the current directory; `--repo-root PATH` names another. Clients show the tools as `mcp__code-intel__<tool>` (Claude Code) or under the server name `code-intel`.

MCP clients do not reliably pass your shell environment to the servers they start; the Python SDK's stdio client passes only a minimal default set. To point the server at a Node that is not on the client's `PATH`, or at a TypeScript outside the cache, add an `env` object to the entry:

```json
"env": {
  "CODE_INTEL_NODE": "/absolute/path/to/node",
  "CODE_INTEL_TYPESCRIPT_DIR": "/absolute/path/to/node_modules/typescript"
}
```

`status` shows the Node and TypeScript the server actually found.

**After changing the pin, restart the MCP server in your client**; see [Freshness and the restart rule](#freshness-and-the-restart-rule).

Environment variables:

| Variable | Meaning |
| --- | --- |
| `CODE_INTEL_NODE` | The `node` binary (a path or a command name). Default: the first `node` on `PATH`. |
| `CODE_INTEL_TYPESCRIPT_DIR` | A TypeScript package directory used instead of the cache install. |
| `CODE_INTEL_CACHE_DIR` | Where `setup` installs TypeScript. Default: `~/.cache/code-intel`. |
| `CODE_INTEL_REQUIRE_TOOLCHAIN` | Set to `1` in the test suite: a test that needs Node, TypeScript, git or FastAPI fails instead of skipping when it is missing. |

## Commands

Every command takes `--repo-root PATH` (default: the git work tree containing the current directory) and `--config PATH` (default: `<repo-root>/.code-intel/config.toml`), except `setup`, which needs neither.

| Command | Does | Exit codes |
| --- | --- | --- |
| `config-check` | Validate the config file. Unknown keys, unknown client kinds and missing required keys are errors that name the key and the file. | 0 valid, 1 invalid |
| `mcp` | Serve the [tools](#tools) over stdio. | |
| `setup` | Install the [TypeScript](#install-and-launch) the frontend analysis needs. | 0, 1 failed |
| `doc-check [--json]` | Check the [docs](#docs-check) and the agent instruction files against the code. | 0 no findings, 1 findings, 2 could not run or incomplete |
| `verify [CHECK …]` | Check the analyzers against each other and against the source; see [Verification](#verification). Its `library-capture` check starts the app. | 0 passed, 1 a check failed, 2 a check could not run |
| `summary` | Markdown counts and coverage of the analysis, one table per analyzer. | 0, 1 a section failed |
| `runtime-check [--python PYTHON] [--set K=V …] [--env-file FILE]` | Compare the static route analysis with the running app. | 0, 1 unexplained difference, 2 could not run |
| `capture [--check] [--python PYTHON]` | Record what installed code contributes to the app (routers library factories build, library dependency trees) in `.code-intel/library-capture.json`; `--check` compares without writing; see [Library routers and dependencies](#library-routers-and-dependencies). Needs `[backend.capture]`. | 0, 1 `--check` found a difference, 2 could not run or the environment differs from the lock |
| `dump di [--json]` | Dependency-injection map: every route with its dependencies in FastAPI order. | 0, 1 could not run, 2 incomplete |
| `dump routes [--json] [--unmatched]` | Backend routes matched with frontend calls. | 0, 1 could not run, 2 incomplete |
| `dump jsx (COMPONENT \| --all) [--root DIR] [--include-tests] [--no-self-close] [--json]` | JSX usage of a component, or every element. | 0, 2 frontend unavailable |
| `dump symbols (NAME \| --dump-ts \| --dump-py) [--side backend\|frontend\|both] [--kind KIND] [--file-hint TEXT] [--json]` | Where a symbol is defined, or every indexed declaration. | 0, 2 frontend unavailable |

The `dump` commands are for checking an analyzer by hand; their JSON is the same schema the tools return.

**Docs check.** `doc-check` needs only Python, git and the packaged Markdown parser. Standard output holds only the findings, one `path:line: message` line each, or with `--json` one JSON document (the [response contract](#response-contract) envelope with `findings`, `finding_count`, `doc_count` and `reference_counts`). Everything else goes to standard error: the `⚠ INCOMPLETE` banner with every diagnostic, the `ⓘ` limitations, and a one-line summary (`doc-check: N findings in D docs (R references, C checked)`, ending in `; INCOMPLETE` when the report is incomplete; R counts every reference the docs name, C the ones whose status decides the verdict). No GitHub workflow commands (`::error`) are printed, because messages quote doc text that a pull request controls.

| Code | Meaning |
| --- | --- |
| 0 | No findings. Limitations (an uninitialized submodule, no Node or TypeScript, an unresolved include) are printed and do not count. |
| 1 | Findings. |
| 2 | The check could not run (an interpreter older than the backend's, a git failure, an invalid config, an analyzer error, with the traceback for an internal error), or the report is incomplete (a doc or code file could not be read, decoded or parsed, or the app did not resolve), with or without findings, because an incomplete report can hide findings. A diagnostic of an untracked or ignored file does not count, because CI's checkout has no such file. |

What it checks and how to fix a finding: [Docs check](#docs-check).

**Running the TypeScript helper by hand.** When a tool reports `helper_failed`, run the helper to see the full error. It reads one JSON request on standard input and writes one JSON document on standard output (the protocol is in `ts_bridge.py` and `result_types.py`). The path is `code_intel.ts_bridge.HELPER`; the TypeScript directory is the one `status` reports:

```sh
echo '{"protocol": 1, "mode": "facts", "typescript_dir": "'"$TS_DIR"'",
       "files": ["'"$PWD"'/frontend/components/ItemList.tsx"], "snippet_max": 500, "signature_max": 200}' \
  | node "$HELPER"
```

## Configuration

The analyzed repository carries `.code-intel/config.toml`. Every path in it is relative to the repository root, uses forward slashes and has no `..`. Unknown keys, unknown `kind`s and missing required keys are errors that name the key and the file. The file is read again on every call and is part of every cache's fingerprint, so a config edit needs no restart.

```toml
[backend]
root = "backend"                       # import root: the directory holding the package
package = "app"
app = "app.main:app"                   # module:attribute, or module:factory() for an app factory
lock = "backend/uv.lock"               # the locked FastAPI decides the include model
python_version_file = "backend/.python-version"

[backend.capture]                      # optional: the library capture and runtime-check's defaults
python = "backend/.venv/bin/python"    # an interpreter with the app's dependencies installed
env = { FEATURE_FLAG = "true" }        # synthetic values only: the app is imported with nothing else

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.app.json"   # options only; the file set is the walk
lockfile = "frontend/package-lock.json"

[[frontend.clients]]
kind = "instance"                      # <export>.get/post/put/patch/delete/head/options(url, ...)
module = "frontend/src/lib/api.ts"     # and <export>.request({url, method})
export = "api"
base_path = "/api"                     # the axios baseURL, prepended to every URL

[[frontend.clients]]
kind = "instance"                      # a package's export: import axios from "axios"; axios.get("/api/…")
package = "axios"

[[frontend.clients]]
kind = "global"                        # a global function, here fetch(url, init)
name = "fetch"
url_arg = 0
init_arg = 1
same_origin_only = true                # an absolute URL is another service: no call, no diagnostic

[frontend.pages]
kind = "react-router"                  # next-app | react-router | none
modules = ["frontend/src/App.tsx"]     # <Route path> literals, nested paths joined
public_dir = "frontend/public"

[symbols]
primary_prefixes = ["frontend/src/components/", "frontend/src/lib/", "backend/app/"]

[docs]
scope = ["docs/**/*.md", "AGENTS.md"]
route_prefixes = ["/api"]              # a mention may add or omit any of these

[docs.reachability]                    # optional table; without it there is no orphan check
root = "docs/README.md"
include = ["docs/**/*.md"]
exclude = []

[[docs.rules]]
paths = ["docs/imported.md"]
checks = ["links"]
reason = "Imported material: only its links must resolve."

[[docs.ignore]]
file = "docs/guide.md"
kind = "route"
text = "/api/external"
reason = "Served by another service."

[agents]
rules_dir = ".claude/rules"
skills_dir = ".claude/skills"
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
```

### `[backend]`

| Key | Required | Meaning |
| --- | --- | --- |
| `root` | yes | The import root: the directory holding the package. `.` when the package sits at the repository root. |
| `package` | yes | The Python package to analyze, without importing it. |
| `app` | yes | The FastAPI app as `module:attribute`, or `module:factory()` for an app factory. A factory validates but is not analysed yet: it is reported as `unresolved_app`, and `runtime-check` can still import it. |
| `lock` | no | A `uv.lock` (or any file in that format) whose locked `fastapi` version picks the [include model](#how-the-backend-analysis-works). Without it the model is `unknown`. |
| `python_version_file` | no | A `.python-version` file. Backend analysis is refused (`runtime_incompatible`) on an interpreter older than the version it names. |

### `[backend.capture]`

Optional. Its presence turns on the [library capture](#library-routers-and-dependencies): the analysis applies the capture file, and `capture`, `verify`'s `library-capture` check and `runtime-check` can start the app.

| Key | Required | Meaning |
| --- | --- | --- |
| `python` | yes | An interpreter with the app's dependencies installed (a virtual environment's, for example). Repository-relative; it need not exist for `config-check`. |
| `file` | no | The capture file. Default `.code-intel/library-capture.json`. |
| `env` | no | Environment variables the import sees besides `PATH`, `HOME`, `TMPDIR`, `LANG` and `LC_ALL`. Synthetic values only: the file is committed and shown in messages. |

### `[frontend]`

| Key | Required | Meaning |
| --- | --- | --- |
| `root` | yes | The frontend directory. JSX and symbol scans default to it. |
| `tsconfig` | yes | The tsconfig whose options (paths, base URL, JSX) the routes analysis uses. Only the options matter; the file set is the walk. |
| `lockfile` | no | The frontend lockfile; part of the routes fingerprint. |
| `clients` | no | The calls that count as API calls; see below. Without any, no frontend call is found. |
| `pages` | no | The frontend pages the docs check knows; see below. |

### `[[frontend.clients]]`

A call is a frontend API call when its callee resolves, through the TypeScript checker, to a configured client. `kind` is required and decides the other keys; a key that does not belong to the kind is an error.

| `kind` | Keys | Recognizes |
| --- | --- | --- |
| `call` | `module`, `export`, `url_arg` (0), `init_arg` (1), `default_method` (`GET`) | A function exported from `module`: `export(url, init)`. The method is the init object's `method` property, else `default_method`. |
| `options` | `module`, `export`, `options_arg` (0), `url_property` (`url`), `default_method` | A function taking one options object that carries the URL: `export({ url: … })`. |
| `instance` | `module` + `export`, **or** `package` (+ optional `export`, default `"default"`); `base_path` | A client instance: `<export>.get/post/put/patch/delete/head/options(url, …)` with the method taken from the member name, and `<export>.request({ url, method })` and `<export>({ url, method })`. `base_path` (starting with `/`) is prepended to every URL. `module` and `package` are exclusive: `module` names a file in the repository that exports the instance, `package` an npm package whose export is the instance (a bare `import axios from "axios"`). |
| `global` | `name`, `url_arg` (0), `init_arg` (1), `same_origin_only` (false), `default_method` | A global function such as `fetch(url, init)`. With `same_origin_only`, an absolute URL is another service: it yields no call and no diagnostic. A local function or import that shadows the name is not the global. |

`default_method` is one of `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`. Aliases are followed: renamed, default and namespace imports and re-exports for every kind, and for function clients also `await import()`, `require()`, destructuring and `const f = client`. A missing client module, or a module that no longer declares the export, is a `frontend_client_module_missing` diagnostic; the other clients are still analyzed. A reference to a client that is not a readable call is an `unparsed_frontend_callsite` diagnostic.

URL builders are found in the modules of the configured clients: exported functions whose every return is a path. A builder call outside a client call is a consumer of its path; inside a client URL it contributes to the URL.

### `[frontend.pages]`

| Key | Meaning |
| --- | --- |
| `kind` | `none` (default), `next-app` or `react-router`. |
| `modules` | Required for `react-router`, rejected otherwise: the files whose `<Route path="…">` elements define the pages. |
| `public_dir` | Every file under it is served at its path below it, and counts as a page. |

`next-app` reads pages and route handlers from the `app/` tree under `frontend.root` (`page.*` and `route.*` files with a `.ts`, `.tsx`, `.js` or `.jsx` suffix; `(group)` and `@slot` directories do not appear in the URL, a `_private` directory is not routable; `[id]` is a parameter, `[...rest]` and `[[...rest]]` catch the rest). `react-router` reads the string-literal `path` of every `<Route>` in `modules` through the TypeScript helper (so it needs Node and TypeScript), joins nested paths onto the nearest enclosing route's, and understands `:id`, optional `:id?` (the page with and without it) and a trailing `*`. See [React Router limits](#react-router-limits).

### `[symbols]`

`primary_prefixes`: repository-relative path prefixes whose definitions rank first in `symbol_resolve` (see [Symbols](#symbols)).

### `[docs]`

| Key | Meaning |
| --- | --- |
| `scope` | Required, non-empty. Globs of the Markdown files to check, if git sees them. |
| `route_prefixes` | Prefixes a mention may add or omit: with `["/api"]`, a doc may write `GET /items` for the route `/api/items`, or `GET /api/items` for an unprefixed one. Each starts with `/` and does not end with one. |
| `reachability` | Optional table `root`, `include`, `exclude`. Docs matching `include` (and not `exclude`) must be reachable from `root` through links, else they are `orphaned doc` findings. Without the table there is no orphan check. |
| `rules` | `[[docs.rules]]`: `paths` (globs), `checks` (a non-empty subset of `links`, `paths`, `routes`, `symbols`) and a required `reason`. A doc gets every check unless the first rule whose `paths` match it names fewer. |
| `ignore` | `[[docs.ignore]]`: `file`, `kind` (`link`, `path`, `route` or `symbol`), `text` and a required `reason`. Suppresses the failing references of that kind in that file whose text, as written, equals `text`. An entry that suppresses nothing is itself a finding. |

### `[agents]`

`rules_dir` and `skills_dir` are required; `instruction_map` (`file` and `heading`) is optional. See [Agent instruction files](#agent-instruction-files).

## Tools

| Tool | Use it for | Default `limit` |
| --- | --- | --- |
| `di_deps` | The dependency chain of a route: app, router and include guards, route-decorator guards, handler parameters and the transitive closure. With no `handler` or `path`, every dependency target. | none |
| `di_reverse` | Blast radius of a dependency: every route using it, labelled `direct`, `route decorator`, `router`, `app` or `transitive`. | 50 (across groups) |
| `route_match` | Frontend call to backend handler, or the reverse. With no arguments, a coverage summary. Falls back to the orphan lists when the handler, function or path exists but is unmatched. | 50 |
| `route_unmatched` | Backend routes with no frontend caller, and frontend calls with no backend route. | 50 per side |
| `jsx_usage` | Every JSX render site of a component. | 50 |
| `symbol_resolve` | Ranked definitions of a same-name symbol across backend Python and frontend TypeScript/JavaScript. | 50 |
| `doc_refs` | Docs that mention a file, route or symbol, each mention with its line and docs-check status; or, for a doc, its links, code references, the docs linking to it and whether the reachability root reaches it. | 50 per list |
| `status` | Health: completeness, cache freshness, stale analyzer code, git SHA, Python, the config, the Node/TypeScript toolchain, diagnostics, limitations; `load_counts=true` adds DI and routes counts. | |
| `refresh` | Drop every cache and rebuild DI and routes. Troubleshooting only: caches refresh themselves, and refresh does not reload analyzer code. | |

Every tool returns text by default and accepts `output_format: "json"`. Tools are registered with `structured_output=False`, so clients get one text result, not a second `structuredContent` copy. An error body (see [Response contract](#response-contract)) is sent with the MCP result's `isError: true`; the text is the full error body in the requested format.

**Arguments** are typed: the input schema lists every accepted value of `match` (`exact`, `suffix`, `contains`, `auto`), `side` (`backend`, `frontend`, `both`), `method` (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`, `TRACE`, `WS`), `kind` and `output_format`, and the bounds of `limit` (an integer `>= 0`, or `null` for every row) and of the required names (non-empty). The tools repeat these checks before any analysis, together with three a schema cannot express, in this order: a whitespace-only name, `method` without `handler` or `path` (`method needs handler or path`), and a `root` outside the repository.

**Argument names are checked too.** A name a tool does not declare (`hanlder=`) is an `invalid_argument` (`Unknown argument 'hanlder' for di_deps. Allowed: …`) listing the tool's arguments in `allowed`, instead of being dropped and answered as if it were absent. A value the schema refuses (`limit=-1`, an enum value it does not list, a missing required argument) gets the same `invalid_argument` body, in the requested `output_format` and with `isError`.

**Line numbers.** A backend `file:line` is the handler's `def` line, never a decorator. A frontend call shows `[file def D · call C]`: the start of the enclosing function and the call inside it, collapsed to `[file:C]` when they coincide or when the call is not inside a named function. All paths are repository-relative POSIX paths. Lines break at `\r\n`, `\r` and `\n` only, as editors and `rg` count them, and columns are 1-based code points.

**Files.** Walks use git-visible files only and skip `node_modules`, `__pycache__`, `.next`, `dist`, `.venv`, `venv`, every dot directory and symlinks at any depth. Build output (`coverage`, `out`, `build`, `playwright-report`, `test-results`, `storybook-static`) is skipped only directly under the frontend root, so a route segment like `app/build/` or a backend `coverage` package is still analyzed.

**Truncation** is always stated: `... showing 50 of 186 routes; pass limit=186 (or null) to see all`. Every JSON reply that lists rows carries `omitted` (0 when nothing was cut), and the full count of each list `X` sits beside it as `X_count` in the singular (`match_count`, `usage_count`, `candidate_count`, `dependency_function_count`, `backend_only_count`); `di_reverse` counts its routes across groups as `route_count`.

**JSON keys are stable per tool.** Every reply of a tool has the same keys whichever way it was answered; a key that is not part of the answer is `null` (or `[]` for a list such as `matches`, `hints` or `suggestions`). The exceptions are the modes a tool switches into when called without a query: `di_deps` with no `handler` or `path` lists dependency targets (`dependency_functions`, `dependency_function_count`), and `route_match` with no `path`, `handler` or `frontend_func` returns the coverage `summary`.

**Name matching is exact.** A name that matches nothing gets up to three close-name suggestions; nothing is matched fuzzily.

### `di_deps` / `di_reverse`

`handler` matches a handler's short name or its exact `module:qualname` id. `di_deps` lists each route's dependencies per layer (`App deps`, `Router deps`, `Route deps`, `Direct deps`) and the closure (`All deps`). When a handler or path exists but not for the requested `method`, the reply says which methods exist (`list_items exists for GET; none for PUT.`). With no arguments it lists every dependency target, sorted by name and module, with the full id where two targets share a name.

```
DELETE /api/items/{item_id}  →  delete_item()  [backend/shop/main.py:20]
  App deps:     (none)
  Router deps:  current_user
  Route deps:   require_role(admin)
  Direct deps:  get_session
  All deps:     current_user, get_session, require_role
```

`di_reverse` matches `dep_function` against, in order, an exact `module:qualname` id, an exact short name (for a factory call written `name(arguments)`, the name: `require_role("admin")` matches `require_role`, and arguments are not filtered), and an exact guard alias. Text with a `(` that is not a whole `name(arguments)` call (`get_db(x`) is an `invalid_argument`. When `limit` cuts a group, the note names the limit that shows every group, because the limit is shared. When several definitions share the name, the reply has one group per definition, each with its own file, line and routes; nothing is merged. A route that gets the dependency in several ways is labelled with the first that applies, in the order `direct`, `route decorator`, `router`, `app`, and otherwise `transitive`.

With a [library capture](#library-routers-and-dependencies), a dependency of installed code is a target of kind `library`, shown with its distribution and the definition (`OAuth2PasswordBearer  [fastapi 0.162.0 fastapi/security/oauth2.py:536]`), and an external binding the capture filled shows the expression it is bound to and its captured sub-dependencies (`current_active_user (external) … = fastapi_users.current_user(active=True)`). The route a library defines shows its handler as `me()  [backend/app/main.py:170 · fastapi-users 15.0.5 fastapi_users/router/users.py:46]`: the repository `include_router` that mounts it, then where the library defines the handler.

```
get_session  [backend/shop/deps.py:4]  shop.deps:get_session
  Kind: function
  Sub-deps: (none)
  Transitive: (none)
  Routes using it (4: 3 direct, 1 transitive):
    GET /api/items  →  list_items()  [backend/shop/main.py:10]  (direct)
    GET /api/items/{item_id}  →  get_item()  [backend/shop/main.py:15]  (direct)
    DELETE /api/items/{item_id}  →  delete_item()  [backend/shop/main.py:20]  (direct)
    ... showing 3 of 4 routes; pass limit=4 (or null) to see all
```

### `route_match` / `route_unmatched`

The summary reports `Backend routes`, `Frontend calls` (non-test, with the number of test-file calls skipped), `Matched routes` (backend routes with at least one caller), `Matched calls` (frontend calls with a backend route), `Match rows` (route/call pairs) and `Coverage` (matched routes over backend routes; `n/a` while the analysis is incomplete). A `possible` match is marked `(possible: <reason>)` in text; see [Matching](#matching).

**Library routes** (routes installed code adds, known through a [library capture](#library-routers-and-dependencies)) match calls like any route, but are kept out of `backend_routes`, `backend_only` and coverage: the repository neither owns them nor decides whether they are called. The summary counts them apart (`Library routes: 8 (3 matched)`; `library_routes`, `matched_library_routes`) and the JSON lists them under `library_routes`; every route row has `library`, `null` or `{dist, version, file, line}`. For such a row `file` and `line` are the repository's `include_router` call.

`path` matching for these lookups:

| `match` | Rule |
| --- | --- |
| `exact` (default) | Equal after dropping the query string and replacing every parameter with `{}`, so `/items/{id}` finds `/items/{item_id}`. A trailing slash is significant when the app sets `redirect_slashes=False`; the reply says when the other spelling exists. |
| `suffix` | Exact, or the candidate's last path segments equal the query's segments: `/roles` finds `/api/auth/roles` but not `/userroles`. |
| `contains` | Substring of the backend path, the frontend path or the frontend source text, with every parameter in both written as `{}`. The query string is kept. |
| `auto` | Exact or suffix. |

```
GET /api/items
  Backend:  list_items()  [backend/shop/main.py:10]
  Frontend: GET "/items"  loadItems()  [frontend/components/ItemList.tsx:3] via api
```

When nothing matches but the handler, function or path exists, the reply falls back to the unmatched entries that fit the query, under the same JSON keys as `route_unmatched` (`backend_only`, `backend_only_count`, `backend_omitted`, `backend_only_confirmed`, `backend_only_gaps` and their `frontend_` counterparts). In JSON, `frontend_calls` has one row per call and URL variant (a call whose URL has two evaluated variants gives two rows), while `summary.frontend_calls` counts distinct calls. Calls in test files are left out of `frontend_calls` and matching, and counted in `summary.test_calls_skipped`.

**Every route figure says how far it can be trusted.** Some findings mean the analysis may differ from the app, and each kind of finding has a known effect on the figures:

| Effect | Findings | Can move |
| --- | --- | --- |
| Calls missed | frontend `list_failed`, `stat_failed`, `read_failed`, `decode_failed`, `parse_failed`, `analysis_failed`, `frontend_client_module_missing`, `unparsed_frontend_callsite`, `unresolved_frontend_path`, `typescript_syntax_error` (the recovered tree can swallow the rest of a file) | calls and matches up, backend-only down, frontend-only up |
| URL variants dropped | `url_variants_truncated` | matches up, backend-only down, frontend-only up |
| Calls misread | frontend `decode_failed`, `typescript_syntax_error` | every match and orphan figure, either way |
| Routes missed | backend `list_failed`, `stat_failed`, `read_failed`, `decode_failed`, `parse_failed`, `analysis_failed`, `unresolved_app`, `unresolved_include`, `unresolved_route_router`, `opaque_router_call`, `asgi_mount`, `unsupported_route_registration`, `unsupported_router_argument`, `router_include_cycle` | routes, matched calls and backend-only up, frontend-only down; matched routes, match rows and coverage either way |
| Routes misplaced | `unresolved_route_path`, `unresolved_route_prefix`, `unresolved_include_prefix`, `unresolved_mount_path` (the route is kept under an `<unresolved: …>` path that matches no call) | matched calls up, frontend-only down; the rest either way |
| Routes changed | `opaque_router_change`, `custom_router_class`, `recursive_router_call`, `router_call_limit`, `unimported_router_change`, `library_capture_stale` | every route-based figure, either way |
| Calls caught by a mount | every routes-missed finding but `unresolved_route_router` and `router_include_cycle`, `unresolved_mount_path`, and `mount_shadows_route` | matches and coverage down, backend-only and frontend-only up: a mount takes every request under its path |

Not counted: `unresolved_http_method` (an unknown method matches any route), `unresolved_prop_value` (kept as a possible variant), routes judged not live (`unmounted_router`, `deferred_router_mount`, `deferred_route_registration`), routes kept although they may not run (`conditional_router_mount`, `conditional_import`), `unresolved_route_methods` (ANY), `unresolved_redirect_slashes` (True) and DI-only findings.

A figure is then `exact`, `at_least`, `at_most` or `unbounded`, and never past its ends: a count cannot fall below 0 and coverage cannot pass 100%. The summary's JSON carries `summary_bounds` for `backend_routes`, `frontend_calls`, `matched_routes`, `matched_calls`, `match_rows`, `backend_only`, `frontend_only` and `coverage_pct`; text marks each figure that is not exact `(at least; see below)`, `(at most; see below)` or `(not bounded; see below)` and names the findings below, one line per effect.

**Orphans are confirmed or not, complete or not.** An orphan list is *confirmed* when no finding can take an entry off it (a missed or misread call, or a route at another path, can give a backend-only route a caller), and *complete* when no finding can add one. Every `route_unmatched` reply, every `route_match` orphan fallback and the summary carry `backend_only_confirmed`, `backend_only_count_bound` and `backend_only_gaps` (the findings that can move the list, as `{"kind", "file", "line", "message"}`) and their `frontend_only_` counterparts. Text heads such a list `Backend-only routes, not confirmed (N):`, `…, may be incomplete (N):` or `…, not confirmed and may be incomplete (N):`. Matched pairs are always kept.

Without the frontend (see [Requirements](#requirements)) the reply makes no orphan claim:

```
⚠ INCOMPLETE — the frontend was not analyzed, so frontend-derived results are n/a.

Frontend not analyzed: node not found on PATH. Fix: install Node.js 18 or newer and put node on PATH, or set CODE_INTEL_NODE to the node binary
No frontend consumer can be confirmed or ruled out until the frontend is analyzed.

Backend routes that fit the query (1):
  GET /api/items  →  list_items()  [backend/shop/main.py:10]
```

### `jsx_usage`

Returns `file`, `line`, `column`, `snippet`, `parent_function`, `tag_kind` (`open` or `self_close`) and `is_test` for every `<Component …>` and `<Component … />` whose tag equals `component_name` exactly (member tags such as `Ctx.Provider` included). `root` (default: the frontend root) must be a directory inside the repository, or the tool returns `invalid_root`; a root inside a frontend root the checkout does not have is a `frontend_unavailable` error with `problem: "frontend_not_found"`. `include_self_close` (default true) and `include_tests` (default false: test, spec and mock files are not product usage) filter the rows, and the text header states both filters. See [JSX usage](#jsx-usage) for the rules.

```
Found 2 JSX usages of <Row> across 1 file(s) (test files excluded):

  frontend/components/ItemList.tsx:9:7  self_close in ItemList
    <Row onLoad={loadItems} />
  frontend/components/ItemList.tsx:10:7  self_close in ItemList
    <Row onLoad={() => removeItem(1)} />
```

### `symbol_resolve`

Returns ranked candidates with `name`, `file`, `line`, `end_line`, `kind`, `scope`, `container`, `exported`, `is_test`, `signature` (the declaration header), `language`, `score` and `file_hint_match`, `protocol_method` and `protocols` (Python methods; see [Calls through a Protocol](#calls-through-a-protocol)). `kind` filters by kind (`none` means any); `file_hint` is a substring or glob of the path, and a matching candidate scores 1.0. `side` selects `backend`, `frontend` or `both`. See [Symbols](#symbols).

```json
{
  "candidates": [
    {
      "container": null,
      "end_line": 17,
      "exported": false,
      "file": "frontend/components/ItemList.tsx",
      "file_hint_match": false,
      "is_test": false,
      "kind": "component",
      "language": "typescript",
      "line": 15,
      "name": "Row",
      "protocol_method": false,
      "protocols": [],
      "scope": "module",
      "score": 0.4,
      "signature": "function Row(props: { onLoad: () => unknown })"
    }
  ],
  "candidate_count": 1,
  "complete": true,
  "omitted": 0,
  "schema_version": 2,
  "side": "both"
}
```

(trimmed: the envelope fields of the [response contract](#response-contract) are left out.)

### `doc_refs`

Takes exactly one of `file`, `route`, `symbol` or `doc`, plus `limit` and `output_format`; none or two of them is an `invalid_argument`. `file` and `doc` are repository-relative paths (an absolute path inside the repository is converted, one outside it is refused, and `doc` must end in `.md`), `route` is `METHOD /path` or `/path`, and `symbol` is a name or a dotted name. The answer comes from the [docs check](#docs-check)'s own extraction and resolution, so a mention's `status` is the one `doc-check` gives it.

| Query | Returns |
| --- | --- |
| `file` | Every mention of the file or directory, which need not exist (so the docs still naming a deleted file are found): a link or path whose target is it or lies under it; a route mention resolved to a route defined there, with status `ok`, `method_differs`, `unmounted` or `frontend`; and a symbol mention resolved to a definition there outside a function body. `ambiguous` is true when the name is also defined outside the file (text: `[also defined outside it]`). |
| `route` | The live routes the query fits (`definitions`, text `Live route:`), then every route mention whose path fits the query segment by segment, whatever the parameter syntax (`{id}`, `:id`, `[id]`, `<id>`) or names and with or without a configured route prefix. A `.../tail` mention matches a query whose path ends with its segments. A method on both sides must agree; a query or a mention without one matches any method. |
| `symbol` | The symbol-index definitions (`definitions`; for `Owner.attr`, an `attr` whose container is or ends with `Owner`; more qualifiers must match the whole container chain, and those left over the module, as the end of its path; in Python they may instead name a package whose `__init__.py` binds the next name at top level), then the symbol mentions. A name matches a mention that is the name, or starts or ends with it as a dotted part. `ambiguous` is true when the definitions span more than one owner or file, so the mentions may be of different symbols; the text then adds one line naming those places. |
| `doc` | The doc's `links` and code `references` (paths, routes, symbols), each with `status` and `checked`; `inbound`, the docs linking to it; `checks`, the checks its rule runs; `in_scope`, false for a doc outside the check's scope; and `reachable`, `null` for a doc outside the orphan check. |

Every mention row carries `file`, `line`, `kind`, `text` (as written), `target`, `status`, `detail`, `message` (the docs check's finding text for a failing status, else `null`), `checked`, `defined_in` (`file:line` of the resolved definitions) and `ambiguous`. `limit` applies to each list separately; each has its `_count` and `_omitted`, and `omitted` sums them.

```
Live route: DELETE /api/items/{item_id}  delete_item()  [backend/shop/main.py:20]

1 mention(s) of DELETE /api/items/{item_id} in 1 doc(s):
  docs/items.md:5  route  DELETE /items/{id}  ok
```

Without the frontend toolchain no frontend definitions are indexed, so a `symbol` query, and a `file` query on a file under the frontend root, may miss frontend definitions: the reply is `complete: false` with a `frontend_unavailable` diagnostic. Route, doc and backend-file queries do not depend on it.

**Blind spots.** Only a code span that is a whole name (an identifier, a dotted name, an empty call or a JSX tag) is a symbol mention; a signature or an expression inside a code span is not checked or found. A path, route or name in plain prose is not a mention, except a `METHOD /path` or `METHOD .../tail` route. Docs outside the check's scope are not indexed, and a `doc` query on one returns `in_scope: false`. A star import in a package's `__init__.py` is not followed, so a name only it re-exports is not reached through the package.

### `status`

Reports the FastAPI version `backend.lock` locks and the include model it gives (`fastapi`: `fastapi_version`, `include_model`, `include_model_reason`, `source`, `summary`), `complete`, per-analyzer state (`not_run`, `complete`, `incomplete` or `failed`, with the failed step and error), cache freshness (`backend_model`, `di`, `routes`), the toolchain probe, the deduplicated diagnostics and limitations of the loaded analyses and, with `[backend.capture]`, the `library_capture` block (the file, its app and distributions, how many includes, routes and dependencies it holds and, once DI is built, how many were applied, and the stale findings; the status never builds anything or starts the app). A cache whose sources were modified within the last two seconds stays loaded but is not `fresh` until the window has passed (see [Freshness](#freshness-and-the-restart-rule)).

The toolchain state is `ok`, `unavailable` (the probe found a problem) or `helper_failed` (the probe is fine, but the last TypeScript helper run in some mode crashed). `helper_failures` maps each helper mode whose last run crashed (`facts`, `routes`) to its problem; an entry clears when a run in that mode succeeds. The probe itself never runs the helper:

```
Frontend toolchain: node v26.13.0, typescript 7.1.0-beta (~/.cache/code-intel/ts/<digest>/node_modules/typescript): ok
Frontend toolchain: unavailable: …/node_modules/typescript/package.json not found. Fix: run "code-intel setup" (or set CODE_INTEL_TYPESCRIPT_DIR to a typescript package directory). DI tools are unaffected; JSX usage, frontend symbols and route matching need it.
```

`load_counts=true` runs DI and routes and adds `counts: {"di": {...}, "routes": {...}}`. Each analyzer runs on its own: one that fails leaves its counts `null` with the error under `counts_errors`, and routes counts are `null` with a `frontend_unavailable` error when the frontend could not be analyzed. On an old interpreter nothing runs: `counts` is `null` and `counts_error` names the refusal.

## Response contract

Every JSON reply carries `schema_version: 2`, `analyzer_code_stale` (`true` when a loaded module file or the pin changed on disk, `null` when one cannot be compared, else `false`) and `analyzer_code_unconfirmed` (the files that cannot be compared, `[]` when none). A field added to a row is additive and leaves `schema_version` at 2; the version changes only when a field is renamed, removed or changes its type or meaning.

**Success bodies** also carry:

- `complete`: `true` when nothing was lost; `false` when any diagnostic was reported (or, in `status`, an analyzer failed); `null` in `status` while an analyzer has not run on the current sources.
- `diagnostics`: lost content or a resolver failure. Each sets `complete: false`, and text output leads with one banner grouped by step, such as `⚠ INCOMPLETE — 2 files could not be read, 1 directory could not be listed; counts may be undercounts.` A `frontend_unavailable` diagnostic gets its own sentence, because its counts are not undercounts but n/a. Shape: `{"kind", "category": "content_lost" | "resolver", "stage", "step", "file", "line", "message", "exception"?, "likely_cause"?, "missing"?, "fix"?, "problem"?}`.
- `analysis_limitations`: known blind spots that lose no data. They never change `complete`; text output shows one leading line (`ⓘ N analysis limitation(s) (kinds); details: status or output_format='json'`) and `status` lists them in full. Shape: `{"kind", "stage", "file", "line", "message"}`.

Rows use `file` and `line` (frontend rows add `def_line`); route matches nest `backend` and `frontend` objects.

**Error bodies** are `{"schema_version": 2, "error": <code>, "message": …, …fields}` in JSON and the message in text, and carry the stale-code warning like any reply:

| Code | Fields | When |
| --- | --- | --- |
| `invalid_argument` | `argument`, `value`, `allowed`? | an argument fails validation, or its name is not one the tool declares; no analysis ran. `allowed` lists the accepted values (or, for an unknown name, the tool's argument names) |
| `invalid_root` | `root` | `jsx_usage` `root` is outside the repository or not a directory |
| `runtime_incompatible` | `running`, `required` | backend analysis on an interpreter older than the backend's |
| `analysis_failed` | `analyzer`, `step`, `missing` | an analyzer raised; the message names the step and the repository root |
| `frontend_unavailable` | `missing`, `fix`, `problem` | the tool needs the frontend and it could not be analyzed; `problem` names the cause (below) |
| `internal_error` | `tool` | an unexpected exception in the tool itself; the message names the file, line and function that raised, and the server log has the traceback |

`frontend_unavailable` `problem` codes: `node_not_found`, `node_failed`, `node_too_old`, `typescript_not_found`, `typescript_unreadable`, `typescript_unsupported` (TypeScript older than 7.1) and `toolchain_incomplete` (the probe returned no problem, yet node or TypeScript is missing) from the toolchain probe; `frontend_not_found` when the checkout has no frontend root; and `helper_failed` when Node and TypeScript are present but the helper crashed (its message names the helper mode and the failed step; the fix is to [run the helper by hand](#commands)). `symbol_resolve` with `side="both"` keeps its backend candidates and carries the code in its diagnostic's `problem`.

### Diagnostic kinds

| Kind | Category | Meaning |
| --- | --- | --- |
| `list_failed` | content_lost | a directory could not be listed, so its files were not analyzed |
| `stat_failed` | content_lost | a file's metadata could not be read, so the file was not analyzed |
| `read_failed` | content_lost | a file could not be read |
| `decode_failed` | content_lost | a file's bytes could not be decoded as text |
| `parse_failed` | content_lost | a file could not be parsed (`likely_cause` names an interpreter that is too old); for TypeScript, the parser itself failed (for example a stack overflow on very deep nesting), which is not the same as a syntax error |
| `analysis_failed` | content_lost | an unexpected error stopped the analysis of one file; the `step` says which (`analyze module`, `extract facts`, `extract calls`, `index symbols`) |
| `unresolved_app` | resolver | the configured app (`module:attribute`) did not resolve to exactly one FastAPI app, so no route is live; also the result of an app written as `module:factory()`, which is not analysed yet |
| `typescript_syntax_error` | content_lost | TypeScript reported syntax errors; results come from TypeScript's recovered tree |
| `frontend_unavailable` | content_lost | the frontend could not be analyzed (missing frontend root, Node or TypeScript, or a crashed helper); `problem` names the cause with the codes above |
| `frontend_client_module_missing` | content_lost | a configured API client module does not exist, or no longer declares the export, so its calls were not found |
| `unparsed_frontend_callsite` | resolver | a reference to an API client is not a call the analyzer can read (`const f = client` used indirectly, a spread argument), or a callee named like a client whose declaration cannot be found (`client_not_resolved`) |
| `unresolved_frontend_path` | resolver | a frontend API call's URL does not evaluate to a path starting with `/` |

### Limitation kinds

| Kind | Meaning |
| --- | --- |
| `unmounted_router` | a router with registrations is not reachable from the app; its routes are listed under `unmounted_routes`, not as live routes |
| `unimported_router_change` | a module that nothing imports when the app is imported changes another module's router; the change is not counted |
| `unresolved_include` | an `include_router` child could not be resolved to a router (for example a router built by an installed library's factory function that the [library capture](#library-routers-and-dependencies) has no entry for; the message then says so) |
| `unresolved_include_prefix` | an `include_router` `prefix=` could not be evaluated |
| `unresolved_dependencies` | a `dependencies=` value or element could not be evaluated to `Depends`/`Security` calls |
| `unresolved_route_path` | a route path could not be evaluated; the route is reported as `<unresolved: …>` and never matches |
| `unresolved_dependency` | a `Depends`/`Security` target could not be resolved to a callable, or FastAPI would reject the declaration; also the label of an `external` dependency the library capture has no entry for, whose own sub-dependencies are unknown |
| `opaque_router_call` | a router or app is passed to code that may add routes or dependencies the analyzer cannot see |
| `opaque_router_change` | a router's routes, prefix or dependencies are changed in place, or its route list is handed on, so routes the analysis counts may be moved or removed |
| `asgi_mount` | `mount()` of a non-FastAPI ASGI app; its routes are not analyzed |
| `router_include_cycle` | routers include each other in a cycle; the edge closing the cycle is cut |
| `unsupported_route_registration` | a Starlette-level registration (`route`, `add_route`, `websocket_route`, `add_websocket_route`) or `host` |
| `unsupported_router_argument` | a router or app is built with `routes=` (directly or through a `**mapping`), whose routes the analyzer does not read |
| `custom_router_class` | a router subclass overrides its constructor or a registration method, so its prefix, dependencies or routes may differ |
| `conditional_router_mount` | an include runs under a branch, so it may not happen at runtime |
| `conditional_import` | a module's first import is under a branch, loop or handler (or in a conditional module), so the routes, includes and mounts it makes are marked `conditional` |
| `recursive_router_call` | a function called at import calls itself, directly or through other functions; the recursive call is not followed and the routes it would add are missing |
| `router_call_limit` | a function called at import is more than 16 calls deep (one per calling function), or past the 4,096 call chains the analysis follows (one per run, with the count); it is not followed |
| `deferred_router_mount` | an include registered inside a function nothing calls at import; on a router another include already copied at import, it is left out of that include, and the message names the include |
| `deferred_route_registration` | a route registered inside a function nothing calls at import, or in code that runs lazily (a lambda, a generator expression, an annotation) or behind a call the analysis does not follow; it is listed with `mount: "deferred"`, except on a router an include copied at import before the function could run |
| `unresolved_route_router` | a route decorator's base may be a router but could not be resolved |
| `unresolved_route_prefix` | a router's own `prefix=` could not be evaluated |
| `unresolved_mount_path` | a `mount()` path could not be evaluated; the mounted app's routes show it as `<unresolved: …>` |
| `mount_shadows_route` | a route registered after a mount that the mount's path overlaps is not reached for those requests (Starlette dispatches to the mount first); matching still pairs calls with it |
| `unresolved_route_methods` | a `methods=` value the analysis cannot evaluate; the route is reported as `ANY`. `methods=None` is FastAPI's default `GET` and is not reported |
| `unresolved_redirect_slashes` | an app's `redirect_slashes=` could not be evaluated; it is reported as `True` |
| `library_capture_stale` | the library capture does not match the code: a captured distribution's version differs from the lock (the capture is still applied), or an entry matches nothing the analysis finds, names a callable the package lacks, is a binding captured for another expression, or is for another app (not applied); the message names it and says to run `code-intel capture` |
| `opaque_decorator` | a package decorator returns a wrapper that does not copy the function's metadata (`functools.wraps` or `functools.update_wrapper`), so FastAPI reads the wrapper's signature, not the function's |
| `shared_dependency_id` | callables FastAPI reads with different parameters share one dependency id (a `functools.partial` and its function, a factory and the closure it returns, two definitions of one name). The id's `dependency_functions` entry lists one form's sub-dependencies; the message names the id and both lists |
| `unresolved_http_method` | a frontend call's HTTP method could not be evaluated; it is reported as `UNKNOWN` |
| `url_variants_truncated` | a frontend URL has more variants than the evaluator keeps |
| `unresolved_prop_value` | a component prop that feeds a URL receives a non-literal value at some call site |
| `submodule_missing` | a git submodule is not checked out (`git submodule update --init --recursive`), so documentation links and paths into it get status `not_checked`; the docs check still passes |
| `frontend_definitions_unavailable` | Node or TypeScript is unavailable, so the docs check indexes no frontend definitions: frontend symbols named in docs carry no definition location, but their existence still resolves through tracked code, so no status and no exit code changes |
| `frontend_pages_unavailable` | the frontend pages could not be read (Node or TypeScript is unavailable), so a bare path that only a page could satisfy is `not_checked` |

A test keeps these tables in sync with the registries in `problems.py`.

## Analysis notes

### How the backend analysis works

Backend analysis runs in-process on `backend.root/backend.package`, without importing it.

**One router graph** serves both DI and routes. Every `APIRouter(...)` and `FastAPI(...)` call is a node with its own `prefix=` and `dependencies=`; every `include_router(...)` in any module is an edge with its own prefix and dependencies, resolved through imports, aliases, `module.router` attributes and re-export barrels (`from .a import router as a_router`, `from .a import *`). Routes are the paths through the graph from the app. A router built inside a factory function belongs to that function, and each include of the factory's result adds an edge. `app.mount(path, sub_app)` of a FastAPI sub-app continues the walk; any other mount is an `asgi_mount` limitation. Registrations come from the route decorators (`get`, `post`, `put`, `patch`, `delete`, `options`, `head`, `trace`, `api_route`, `websocket`) and from `add_api_route` and `add_api_websocket_route`. Router factories and package functions called at import are followed, including nested ones. Mounts on a router reached through `include_router` are dropped, as FastAPI does. Star-import barrels are resolved; a module's `__all__` limits them only when it is one unconditional literal list or tuple of strings that no module changes in place. Chains of plain re-exports are followed in a loop; other nested lookups stop at 100 levels with a limitation.

**Include models.** What an include serves depends on the FastAPI version `backend.lock` locks. The analysis reads it on every build, the DI and routes JSON name it in their `app` block (`fastapi_version`, `include_model`, and `include_model_reason`, `null` unless the model is `unknown`), and `status` shows it. Order is the app's import order: a module's imports run first, depth first (a package's `__init__` before its submodules), and a function called at import runs at its call.

- `copy`, FastAPI before 0.137.0: `include_router` copies the child's routes when it runs, so an include sees exactly the registrations on the child that ran before it. A registration made after the include, in the same module or in a module imported later, is not part of that include, and neither is a mount on an included router (FastAPI drops it).
- `live`, FastAPI 0.137.0 and later: the parent keeps the child router, so every registration on it is served through the include, with the include's prefix and dependencies, whenever it runs. Code that runs after the import (an `on_event` handler, a `lifespan`) or may never run is listed as `deferred` and reported. A mount on an included router is served under the include's prefix.
- `unknown`, when the lock is missing or unreadable, locks no `fastapi` or several versions, or the version is not a plain release (`0.137.0rc1`): no registration is dropped on order grounds. One only `live` serves is listed as `deferred` and reported, with the reason the version is unknown in the message.

Under `copy`, a registration in a function that can only run after the import, on a router an include copies at import, is not part of that include either, but only when no way of reaching the function object is left open (it is a module-level function defined once, decorated by nothing but route decorators or `on_event`; every read of its name is the callee of a call the analysis follows; no string names it; its module is never used as a value; and the package has no dynamic lookup that may reach any name: `getattr` with a computed name, `vars`, `globals`, `sys.modules`, `__dict__`, `eval`, `exec`, `__import__`, `importlib.import_module`, …). Such a registration is left out of that include and reported (`deferred_route_registration` or `deferred_router_mount`, naming the include). Any other function may run at import through an alias, an argument, a container, a decorator, a string or a dynamic lookup, so its routes are listed as `deferred` and reported. A router included twice yields two route sets. A change made in a module the app never imports is reported as `unimported_router_change`.

**Values** are evaluated statically: string constants, module and imported constants, `+`, f-strings whose parts are known, list and tuple concatenation, starred sequences and `**kwargs` dictionaries. A value that cannot be evaluated becomes a limitation (`unresolved_route_path`, `unresolved_include_prefix`, `unresolved_route_prefix`, `unresolved_dependencies`), never a silent empty string. A list, dict or set can be changed through any reference to it, so rather than follow each reference the analysis treats every use of a container name as a possible change except a use known to only read it (the `dependencies=`, `prefix=`, `tags=` and `responses=` values of route, include, `APIRouter` and `FastAPI` calls confirmed by import from `fastapi`; a loop or comprehension over it; the reading builtins; comparisons; indexing; and a few read-only methods). A container name with another use is unknown ("may be changed in place") and reported. A value changed through `setattr`, an instance attribute assigned by a method, or a local bound more than once is also treated as unknown and reported, never guessed.

**When code runs.** Module-level code, `if`/`try`/`with`/`for`/`while`/`match` bodies, app or router factories and package functions called at import (`setup()` or `setup(app)` at module level) run at import. An include under a branch, a loop, an `except` handler or a conditional expression is marked `conditional` with a `conditional_router_mount` limitation. Functions nothing calls at import are still read: an include or a route registration inside one is marked `deferred`. A function called at import runs once per call chain, up to 16 calls deep and 4,096 call chains in all; a recursive call is not followed and is reported as `recursive_router_call`, a call past either bound as `router_call_limit`. A module whose first import may be skipped is conditional even if it is imported again later, with a `conditional_import` limitation at the import statement.

**Known gaps**, each reported rather than guessed:

- A router or app passed to code outside the package (`add_pagination(app)`) is an `opaque_router_call`, because that code may add routes or dependencies. A router changed in place (`app.router.routes.clear()`, `app.dependencies = []`) or whose route list is bound to a name or passed on is an `opaque_router_change`.
- **App factories.** `app = "module:factory()"` validates but is not analysed yet: it is an `unresolved_app` diagnostic. [`runtime-check`](#verification) can still import such an app.
- **Library routers and dependencies.** Installed code is not read. Without a [library capture](#library-routers-and-dependencies), a router built by an installed library's factory (`include_router(auth_lib.get_users_router())`) is an `unresolved_include` limitation and its routes are missing from the route tools and the docs check, and a `Depends(name)` that resolves to a module-level name bound to a call result (`current_user = auth_lib.current_user(active=True)`) is an `external` dependency, identified by the defining binding (`module:current_user`) with the bound expression shown, and an `unresolved_dependency` limitation because its sub-dependencies are unknown. The capture fills both.
- A name bound by a `match` capture pattern is not evaluated, so an include of it is `unresolved_include`; so is an include of a loop variable.
- A `global` rebinding of a router name inside any function makes uses of that module-level name ambiguous, so the registrations or includes that read it are reported.
- A route registered under a module-level branch is listed with `mount: "conditional"` and no limitation of its own.
- A container changed through reflection (`globals()`, `vars(module)`, `sys.modules[...]`, `exec`/`eval`, `module.__dict__`) is not seen: its value is listed as if unchanged, with no limitation.

**Routes are listed in Starlette order**, the order the app dispatches them (the first full match wins), with the `def` line of the handler and its `module:qualname` id.

**Dependency order** is FastAPI's: the app's own dependencies, then each include's dependencies and each router's own dependencies from the app down to the registering router, then the route decorator's `dependencies=`, then the handler's parameters. Duplicates are kept, as FastAPI keeps them.

**Parameters** follow FastAPI's `analyze_param`: a dependency is a `Depends(...)` or `Security(...)` from `fastapi` (a local function named `Depends` is not one), in the parameter default or inside `Annotated[...]`, with aliases such as `DbSession = Annotated[Session, Depends(get_db)]` resolved across modules and re-exports. Inside one `Annotated`, only the last FastAPI marker counts: `Annotated[str, Query(), Depends(x)]` depends on `x`, and `Annotated[str, Depends(x), Query()]` declares no dependency. A marker in both the default and `Annotated` is an `unresolved_dependency` limitation, because FastAPI rejects it. `Depends()` with no argument uses the annotation's type.

A target the analysis cannot see into is an `unresolved_dependency` limitation instead of a guess: a name bound to a wrapper call (`get_settings = lru_cache(_settings)` makes the wrapper's result the dependency, not `_settings`), a callable defined outside the analyzed package, or a factory whose returned callable is unknown.

**Identity and display.** A dependency is identified by `module:qualname` (a lambda as `module:<lambda>@L<line>`) and displayed by its short name. Classes contribute their `__init__` parameters and instances (`Depends(RoleChecker("admin"))`, or a module-level `checker = RoleChecker(...)`) their `__call__` parameters, so an instance is its own target, `module:RoleChecker()`, next to the class `module:RoleChecker`. A plain method through an instance (`Depends(service.get_items)`) is bound, so FastAPI skips its first parameter, and is `module:Service().get_items`; through the class it is `module:Service.get_items`. A factory call (`Depends(require_role("admin"))`) is identified by the factory and displayed with its arguments; its sub-dependencies come only from the callable it returns. A module-level guard alias (`admin_only = require_role("admin")`) resolves to the factory and is shown as `require_role(…) [as admin_only]`.

Security scopes, `use_cache`, the `scope=` of `Depends` and `app.dependency_overrides` are ignored: none of them changes which dependencies a route declares. A class whose `__init__` comes from a base outside the package (a pydantic model, for one) contributes no sub-dependencies, and no limitation says so. Dataclass dependencies are supported (their fields are the parameters); any other class decorator is reported as `unresolved_dependency`. A `functools.partial` is identified as its function, with the parameters FastAPI reads from it. A package decorator is transparent when it returns the function itself or a wrapper that copies the function's metadata with `functools.wraps` or `functools.update_wrapper`; any other package decorator is reported as `opaque_decorator`.

### Library routers and dependencies

Static analysis reads the repository, never installed code. Two things in installed code decide what a route is and cannot be read from its source: a router a library builds when it runs (`app.include_router(fastapi_users.get_users_router(...))`), and the dependencies of a callable whose signature the library sets at runtime (`current_user = fastapi_users.current_user(active=True)`, then `Depends(current_user)`). `code-intel capture` imports the app once, under the interpreter of `[backend.capture]`, and records only those facts in `.code-intel/library-capture.json`. The analysis applies that file without running anything, so the MCP server, `doc-check` and every other command give the same answer on every machine.

**Workflow.**

1. Add `[backend.capture]` to the config. Install the backend's locked dependencies (`uv sync --locked` in the backend) so the interpreter matches the lock.
2. Run `code-intel capture`. It finds the gaps statically (each `unresolved_include`, each `external` dependency binding, each installed callable the package uses as a dependency), imports the app, records what the running app shows at exactly those places, and writes the file. A second run writes the same bytes. A gap it cannot fill stays a gap, and its reason is printed (`capture: not captured, include_router at backend/app/main.py:28 (…): pkg.main:own_handler is defined in the backend package`).
3. Commit the file. `capture --check` (and the `library-capture` check of `verify`) fails when the file differs from what the app builds now; run it in CI with the backend's locked environment. After a lock bump of FastAPI or of a captured library, a change to an `include_router` of installed code or to a library dependency, or when `library_capture_stale` appears, run `capture` again; two branches that both recapture conflict, and running it again resolves that.

`capture` refuses (exit 2, nothing written) when an installed version of `fastapi` or of any captured distribution differs from the lock, naming both versions and `uv sync --locked`, because the file would describe code the lock does not install.

**When code runs.** Repository code runs in exactly three commands: `capture`, the `library-capture` check of `verify`, and `runtime-check`. Each starts the configured interpreter on the packaged oracle in a subprocess, from an empty temporary directory, with a 300 s timeout (on expiry the subprocess's whole process group is killed) and a clean environment: `PATH`, `HOME`, `TMPDIR`, `LANG` and `LC_ALL` when set, plus `[backend.capture] env`. `status`, `summary`, `dump`, `doc-check`, `config-check` and every MCP tool never start it.

**What the file holds** (`format: 1`):

- `includes`: for each `include_router` call whose child comes wholly from installed code, the child's routes (method, path relative to the child, handler id and definition, route-level dependencies and their `via`, parameter dependencies), keyed by the call's file, the text of its router argument (`ast.unparse`, so reformatting does not matter) and its occurrence among the calls of that file with the same text;
- `dependencies`: the dependency tree of each external binding (`binding`: `module:name`, with `bound_to`, the text of the expression the name is bound to) and of each installed callable named in the package (`external`: a dotted name). A node with `sub` is a callable of installed code and the capture owns its subtree; a node without it is a callable of the backend package, which the analysis reads from source on every call;
- `callables`: where each installed callable is defined (distribution, file inside it, line), and `distributions` the versions installed.

Everything the repository owns is still read from source on every call: an include's prefix and dependencies, the app's dependencies, dispatch order and the sub-dependencies of the repository's own callables. A repository edit therefore rarely stales the capture.

**What it lifts.** A captured router is a router node mounted exactly like a repository router: it takes the include's own prefix and dependencies and its place in dispatch order, and its routes are live routes (`library` set, `file:line` the include) in DI, routes, docs and `runtime-check`. No `unresolved_include` is reported for it. A captured binding keeps its id and the expression it is bound to, gains the captured sub-dependencies and the `runtime_id` FastAPI reports for it, and is no longer an `unresolved_dependency`; an installed callable named in the package becomes a `library` target. `di_reverse` therefore reaches every route a library dependency guards, where it used to stop short by the routes behind `current_user`.

**Freshness.** The capture file is part of the backend fingerprint, so editing or regenerating it rebuilds DI, routes and docs. On every build the analysis checks, without importing anything:

- each captured distribution's version against the lock: on a difference the capture is still applied and a `library_capture_stale` limitation names the distribution, both versions and `code-intel capture`;
- an `includes` entry that matches no unresolved `include_router` call, an entry for another `app`, and an entry that names a callable of the package the analysis cannot find: not applied, and reported as `library_capture_stale`;
- a `dependencies` entry that no dependency of the package matches, or a `binding` entry whose `bound_to` differs from the expression the package binds the name to now (a rebound name): reported the same way;
- a gap with no entry while `[backend.capture]` is configured keeps its own limitation, which adds `not in .code-intel/library-capture.json; run code-intel capture`.

A malformed file is a `parse_failed` diagnostic on it. What static checks cannot see is a value passed to a library factory changing in another module; the `library-capture` check catches it.

**Limits.** A router that is not wholly from installed code (a repository handler passed into the factory), or whose include did not run once while the app was imported, is not captured. A package callable that returns a closure it defines is identified by FastAPI as its factory, so its parameters cannot be told from the closure's and the entry is not applied. A library that adds routes to a router it is handed (an app-level `get_app()`) is not captured. Library routes are never reported as backend-only or counted in coverage.

### How the frontend analysis works

Frontend analysis runs the TypeScript 7.1 compiler (a native executable that the helper drives through its JavaScript API) through a Node helper (`ts/helper.cjs`), one process per batch, with the request on standard input and the result on standard output. It has two modes:

- **`facts`**: each file is parsed on its own, without a program, and yields its JSX elements and declarations (and, for React Router pages, the string attributes of the configured tags). Results are cached per file, so after an edit only the changed files are parsed again. JSX usage, TypeScript symbols and pages use this mode. A file that cannot be read (`read_failed`), that the parser itself fails on (`parse_failed`) or whose facts cannot be extracted (`analysis_failed`, step `extract facts`) is reported by name, and the rest of the batch is unaffected.
- **`routes`**: one program and type checker over the frontend, built from `frontend.tsconfig` (paths, base URL, JSX) but hermetic: no default library, no type packages, and no file under any `node_modules`. Results do not depend on installed packages. The hermetic module resolution is a static table: before each program is built, every import of every file in it is resolved with TypeScript's own resolution, only the ones that land in the repository outside `node_modules` go into the table, and the program is rebuilt until no new file appears. The checker resolves imports, aliases, namespace imports, re-export barrels and constants across files. It runs in a worker thread while the backend graph is built. Per-file failures name a step: `read file`, `read tsconfig`, `parse file` (TypeScript syntax errors; the file is still read from the recovered tree) and `extract calls`. A file the parser or binder cannot handle (the native compiler crashing, which the helper survives by restarting it for the next file) is a `parse_failed` diagnostic and is left out of the program. Bytes that are not UTF-8 are a `decode_failed` diagnostic, and the file is read with U+FFFD in their place.

**Clients.** See [`[[frontend.clients]]`](#frontendclients). Client and builder rows carry `callee` (the callee as written) and the line and column it starts at, `callee_line` and `callee_column`, which can be a later line than the call's own `line` (`api\n  .get(…)`). WebSocket rows have no callee, so those three are `null`, and their `column` is the start of the URL expression. Calls inside a client's own definition are not consumers. Calls in test files are found but not matched (`test_calls_skipped`).

**The evaluator** handles string literals, templates, `+`, conditionals (`?:` and `??`, one variant per branch), `as const`, `satisfies`, non-null assertions, `const` declarations including imported ones, members of `const` objects and calls to functions in the program, followed through builders that call builders. A value it cannot evaluate becomes a hole in the URL. A URL that does not start with `/` is an `unresolved_frontend_path` diagnostic; more than 16 variants is a `url_variants_truncated` limitation. A client's `base_path` is prepended before matching.

**Holes** are `param` (inside a path segment), `query` (after a literal `?`, or a trailing value that is always a query string or empty) or `unknown` (a trailing value the evaluator cannot see).

**Method.** The `method` property at the top level of the init object (quoted, shorthand or a computed key), after resolving an identifier to its object literal; the member name for an `instance` verb call; `GET` (or the client's `default_method`) when there is no init or no `method`. A computed key is evaluated; one the evaluator cannot read makes the method `UNKNOWN` with an `unresolved_http_method` limitation. A `method` written after a spread wins over the spread. The method never comes from a type annotation.

**Prop flows.** When a URL hole is a parameter or destructured prop of the enclosing component or hook, the string literals passed for it at call sites and in JSX attributes become variants with `confidence: "possible"` and reason `prop_flow`. Sites that omit the prop use the parameter's default. Sites are followed through `const X = Y`, `memo`, `forwardRef`, default exports, and `dynamic`/`lazy(() => import(…))`. A non-literal value at any call site, or a site that cannot be traced to the function, adds an `unresolved_prop_value` limitation and keeps the value unknown.

**WebSockets.** Every string expression in code (never in comments) whose evaluated text contains `/ws/` is a WebSocket call with method `WS`; the path is the part from `/ws/` on. Text containing whitespace is never a URL.

**Known gaps.** A URL string that is never passed to a client call (a `const pdfUrl = "/pdf"` handed to an `<a href>` or an `<img src>`) is not a call: the route it names stays backend-only. A call through a client the config does not name is not found.

### Matching

Backend paths become Starlette's path regexes (`{name}`, `{name:path}`, `{name:int}`, `{name:float}`, `{name:uuid}`, `{name:str}`), tried in Starlette order. Each frontend URL variant is matched separately, with query holes and anything after a literal `?` dropped.

- A literal URL matches the first route whose regex and method match: `exact`.
- A URL with holes is matched segment by segment. A literal segment matches a backend literal or parameter (`exact`). A `param` hole matches a backend parameter (`exact`) or a backend literal (`possible`, `hole_on_literal`). A mixed segment whose holes each start it or follow `/` (`${id}.csv`) matches a parameter (`exact`) or a literal that fits (`possible`).
- A hole glued to text or another hole inside a segment, or one that ends the URL, may be empty or hold `/`. Such a URL is matched character by character against the route's pattern, so convertors still apply. Every route that some value of its holes reaches is listed as `possible`, with reason `mixed_segment`, or `trailing_hole` when the URL ends in a hole, and none shadows another.
- An exact match that fits every value the holes can take shadows every later route, because Starlette always dispatches there; a possible match never shadows.
- `{name:path}` absorbs one or more segments, and the backend segments after it must match the end of the URL.
- An `UNKNOWN` method matches any method (`possible`).
- `WS` calls match only WebSocket routes, and HTTP calls never do.
- Trailing slashes are significant when the app sets `redirect_slashes=False`. With `redirect_slashes=True` a slash-variant match is `possible` (`redirect`).

A match row's `reason` is one of `literal`, `param`, `literal_on_param`, `hole_on_literal`, `mixed_segment`, `trailing_hole`, `method_unknown`, `redirect` or `prop_flow` (an otherwise exact match of a prop-flow variant). Duplicate calls are merged only when file, line, column, method and path are all equal.

### JSX usage

Elements come from the TypeScript parser, so comments, strings, type arguments (`useState<Row>`, `<DataTable<Row> … />`) and generics are never mistaken for tags, and `tag_kind` is exact however long the tag.

#### Parent rule

`parent_function` is the innermost *named function* whose span, from its parameter list to its end, contains the element; elements in a parameter default belong to that function. A named function is a function declaration or a function expression with a name, or an arrow or anonymous function expression that initializes a variable, seen through `as`/`satisfies`/parentheses and through any calls it is an argument of (`const X = () => …`, `memo(forwardRef(…))`, `withA(B)(…)`). So:

- `const X = function Y() {…}` is `Y`;
- `const rows = items.map((r) => <Row />)` is `rows`;
- `dynamic(() => import("…"), { loading: () => <Spinner /> })` does not name the `loading` arrow, which is not a direct argument;
- methods, class-field arrows and object-property arrows (`cell: () => <Badge />`) are not named functions, so their elements take the next named function outward, or `null`.

Fragments (`<>…</>`) have no tag and cannot be queried. JSX usage scans `.tsx`, `.jsx`, `.js`, `.mjs` and `.cjs` files; `.ts`, `.mts` and `.cts` cannot contain JSX (`<Foo>x` is a type assertion there). A test file is one under a `__tests__`, `__mocks__`, `test`, `tests` or `e2e` directory, or named `*.test.*` or `*.spec.*` (the rule is shared with the route analysis). A file with syntax errors is reported as a `typescript_syntax_error` diagnostic and analyzed from TypeScript's recovered tree; a file that cannot be read is a `read_failed` diagnostic. Both name the file.

### Symbols

**Kinds:** `function`, `class`, `component`, `method`, `const`, `variable`, `type_alias`, `interface`, `enum` and `namespace`. **Scopes:** `module`, `namespace`, `class`, `object` (object-literal members) and `local`.

**TypeScript and JavaScript** (every `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs` and `.cjs` file under the frontend root, from the TypeScript parser):

- function declarations, and `const`/`let`/`var`/`using` bindings, including `let x!: T`, destructuring patterns and `for … of/in` headers;
- `component` for a PascalCase name (with a lowercase letter) whose own body contains JSX outside nested functions, or that is initialized by `memo`, `forwardRef`, `lazy` or `dynamic` (also as `React.memo`); SCREAMING_CASE names are never components;
- classes, class methods, accessors and arrow-valued class fields (`method`), object-literal properties whose value is an arrow or function and object shorthand methods; the name of a named class expression (`x = class Name {}`) is `local`, since it is bound only inside the class;
- interfaces, type aliases, enums and `namespace`/`module` blocks with an identifier name; members of nested namespaces have the dotted container (`A.B`);
- `export default x;` and `export = x;` mark `x` exported;
- JSDoc tags in JavaScript files declare nothing: TypeScript 7 parses `@typedef`, `@private` and `@type` into nodes of their own, and the helper skips them.

Not indexed: parameters, catch bindings, plain class fields, constructors, interface and type members, `declare module "x"`, anonymous default exports, `export default <expr>`, export specifiers, `export * as` and the own name of a named function expression.

**Python** (every `.py` file under `backend.root`, from `ast`): module-level functions, classes and assignments, including those inside module-level `if`, `try`, `with`, `for`, `while` and `match` blocks, `for` targets and walrus targets; class bodies (methods, nested classes, class-level assignments); and functions and classes nested inside functions (`local`). `Enum` subclasses are `enum`, `Protocol` subclasses `interface`, and type-alias assignments (`TypeAlias`, `type X = …`, `TypeVar`, `ParamSpec`, `NewType`, `TypeAliasType`, generic subscripts and `|` unions of types) `type_alias`. Function-local variables are not indexed: Python has no declaration keyword, so every assignment in a function body would become a "definition"; use text search for those.

**Ranking.** Base score 0.5 for a path under one of `[symbols] primary_prefixes`, 0.4 elsewhere under the frontend or backend root, 0.1 otherwise; minus 0.3 for a test path (including mocks and `tests` directories), 0.2 for `object` scope and 0.1 for `local` or `class` scope; never below 0. A `file_hint` match scores 1.0. Ties are broken by scope (module first), path depth, file and line, so neither language wins ties by default.

#### Calls through a Protocol

Python language servers resolve a call on a variable typed as a `typing.Protocol` (`gateway: JobGateway`) to the Protocol's method, not to the concrete class's method, so references and callers of `JobGatewayAdapter.active_jobs` list only the calls typed as the adapter. For a Python `method` candidate the reply therefore also says which Protocol methods to query:

- `protocol_method` is `true` when the method is declared in a class that subclasses `Protocol` (text: `in JobGateway (Protocol)`), `false` for every other candidate.
- `protocols` lists, for a method of any other class, each Protocol that declares a method of the same name and that the class matches: `{"name", "id" (module:qualname of the Protocol), "file", "line" (of the Protocol's same-name method)}`, sorted by file and line; `[]` when none, and for every candidate that is not a Python method. Text output adds one line under the candidate: `calls typed as a Protocol it matches (by member names) resolve to: JobGateway.active_jobs (backend/…/processor.py:168), …`.

A class matches a Protocol when it names it as a base, or when every member the Protocol declares is a member of the class. A base counts as naming the Protocol only when it resolves to the Protocol's `module:qualname`, directly or through the bases of an in-repo base class; an unrelated imported class that shares the Protocol's short name does not. Members are methods, annotated or assigned class attributes and nested classes, plus attributes assigned on `self` in `__init__` and `__post_init__`, of the class and of its in-repo base classes. The match is by names only, as runtime Protocol checks are: no signature, annotation or return type is compared. Attributes set elsewhere than `__init__` and `__post_init__`, `__getattr__` and `__slots__` are not seen.

### React Router limits

The `react-router` page provider reads only what the TypeScript parser sees in the configured `modules`:

- a `path={expr}` that is not a string literal is invisible: the route yields no page;
- an `index` route has no `path`, so it yields no page of its own (its parent's path is the page);
- object routers (`createBrowserRouter([{ path: "/x", … }])`) and routes declared in modules that are not listed are unsupported;
- a route whose path is only `*` is the not-found handler and is skipped;
- a listed module that is not a tracked file is a `read_failed` diagnostic.

## Docs check

`doc-check` checks the docs against the code: every link and anchor resolves, every repository path a doc names exists, every backend route and code symbol it names still exists, every doc under the reachability `include` is reachable from its `root`, and the [agent instruction files](#agent-instruction-files) are well-formed. Run it in CI on every pull request and make it unconditional, because a code change can stale a doc that did not change. `doc_refs` answers the reverse question with the same extraction: which docs name a file, route or symbol.

**Scope.** The `[docs] scope` globs: every Markdown file they match, if git sees it (tracked, or untracked and not ignored).

**Per-directory rules.** A doc gets every check unless the first `[[docs.rules]]` entry whose `paths` match it names fewer, with a required `reason`: for historical or imported material, only `links` may be worth checking.

**Reference kinds.** Only what a doc writes as Markdown structure or code counts; a path or a name in plain prose is not checked.

| Kind | What counts | Fails when |
| --- | --- | --- |
| link | Relative Markdown links and images, and HTML `href`/`src`; external links and empty targets are skipped. | The target is not a file or directory git sees (`broken link`), or a `#fragment` on a Markdown target, or on a directory, matches no heading slug and no HTML `id`/`name` in it (`broken anchor`; the match is exact, case included). A directory link lands on its README.md, else its index.md. `/` is the repository root. A link with no path (`#heading`, `?plain=1`) is the doc itself. |
| path | A code span, or a fence line, naming a repository path: from the repository root, from `backend.root`, `frontend.root` or the backend package directory, or from the doc's own directory. Line suffixes (`:12`, `#L12`), globs and `{a,b}` braces are understood. In a fence only paths whose first directory is a top-level repository directory count, so a shell transcript or a sample stack trace is not checked line by line. | No such file or directory (`stale path`). Placeholders (`<name>`, `{x}`, `...`) and example names (`my_…`, `example…`) are skipped. |
| route | `METHOD /path` in a code span, a fence line or prose, and a code span that is only `/path` when its first segment (after an optional configured prefix) is a route root: the root of a live or unmounted backend route, of a frontend page, of FastAPI's own pages (`/docs`, `/redoc`, `/openapi.json`), or of a `METHOD /path` with an upper-case method (or WebSocket) in any doc whose rule checks routes. In prose the method must be upper case or WebSocket, so English such as "get" before a path is not read as a route. Every parameter syntax matches (`{id}`, `:id`, `[id]`, `<id>`), and a mention with or without a configured prefix matches. A path that opens with an ellipsis (`POST .../checkout`) names the end of a route: it passes as `suffix` when exactly one live route of a compatible method ends with those segments. Any other `...`, `${` or `{{` in a path is a placeholder and is skipped. | No live route fits (`route not found`), including one only a plain word filling a parameter would fit, unless a frontend page has exactly that path; only another method serves it (`route method differs`); or only a router the app does not mount does (`route not mounted`). A route tail fails the same ways, and when live routes at two or more paths end with it (`route not specific`). Blind spot: a root that the docs only ever name as a bare `/path` is skipped once its last route is gone, because nothing declares it a route root any more. |
| symbol | A code span that is a whole identifier, a dotted name, a call with empty parentheses or a JSX tag. | It is a CamelCase name, a name starting with an underscore, a call, a JSX component or `Class.attr` where `Class` is a Python class the symbol index holds, and the symbol index defines it nowhere and no tracked code file uses it as an identifier (`symbol not found`). Python is read with `ast`, so a name left only in a comment or docstring does not count. The config file is not code here (an ignore entry's text would otherwise resolve itself). Other names (`snake_case`, `UPPER_SNAKE`, a dotted permission string) that resolve nowhere are `unresolved`, which never fails. |

Two file-level findings complete the list: `orphaned doc` for a doc matching the reachability `include` that no chain of links from `root` reaches, and `unused ignore entry`.

**Statuses.** A reference that fails is `missing`, `anchor_missing`, `method_differs`, `weak_param`, `unmounted` or `ambiguous` (a `.../tail` that several live routes or pages end with). Every other status passes: `ok`; `prefix` and `suffix` (a bare path that is the start or the end of a live route, or a `.../tail` that ends one); `frontend` (a frontend page, route handler or public file); `framework` (`/docs`, `/redoc`, `/openapi.json`); `local_only`; `ignored`; `skipped` (external, placeholder or example); `not_checked`; `unresolved`.

**What git sees.** A file exists for the check when git tracks it or lists it as untracked and not ignored. A tracked file deleted from the work tree but not staged does not exist. The listing also runs inside every initialized submodule, each with its own ignore rules, and with `core.ignorecase=false`, so it matches case-sensitively as on a Linux CI checkout. Symbols, code words, backend routes, frontend pages, diagnostics and route limitations that come from an untracked or ignored file are ignored, so a local run gives CI's verdict.

**Local-only paths.** A path with a directory that never reaches git (`.venv`, `node_modules`) or one git ignores (`.env`, generated code) is `local_only` and passes: it exists on a developer's machine and nowhere else, so neither side can check it. Blind spot: a stale path that a broad `.gitignore` pattern happens to match (`*.log`, `build/`) is `local_only` too.

**Submodules.** Links and paths into a submodule that is not checked out are `not_checked`, with a `submodule_missing` limitation. Symbols are never `not_checked` for a missing submodule.

**Pages.** Without Node or TypeScript, `react-router` pages are unavailable and a bare path that only a page could satisfy is `not_checked` (`frontend_pages_unavailable`); install the toolchain in the job (`code-intel setup`) to check them.

**Library routes.** A route installed code adds is live once the [library capture](#library-routers-and-dependencies) has it, so a doc naming it resolves like any route, and a lookup of the route names the repository `include_router` that mounts it and where the library defines the handler. Without a capture entry it is missing from the analyzed list, so a doc naming it fails with `route not found`, with the limitation printed below. Giving every such doc an ignore entry is the intended fix, since treating the limitation as "not confirmed" would leave the route check unable to fail.

**Ignore entries.** An ignore entry suppresses the failing references of one kind in one file whose text, as written, equals `text`, and needs a `reason`. A link's text is its target percent-decoded, and the entry may give it either way. An entry that suppresses nothing is itself a finding (`unused ignore entry`), so entries go away with the drift they covered. An entry for a reference that cannot be checked locally still counts as used.

**Fixing a finding.** The line names the doc, the line and the reason:

- `broken link` / `broken anchor`: point the link at the file or heading as it is now.
- `stale path`: write the current path. An illustration that is not a real file gets a placeholder (`<path>`) or an example name.
- `route not found`, `route method differs`, `route not mounted`: take the route's URL and method from `route_match`, never from the decorator, which omits mount prefixes.
- `symbol not found`: use the current name. A name that was deliberately removed and that the doc still mentions goes in plain text, not a code span.
- `orphaned doc`: link it from the reachability root or from a page reachable from it.
- `unused ignore entry`: delete the entry.

Add an ignore entry only when the doc is right and the check cannot see it, and say why in `reason`.

```
$ code-intel doc-check
docs/items.md:7: route not found: POST /items/import (only /api/items/{item_id} fits, by a word in a parameter)
doc-check: 1 finding in 2 docs (7 references, 6 checked)
```

### Agent instruction files

With an `[agents]` table the docs check also validates the instruction files coding agents load, and prints their findings as `path:line: message` beside the docs findings:

- every rule file under `rules_dir` opens with YAML frontmatter holding a non-empty `paths` list of non-blank strings, and has no `applyTo` key;
- every `paths` glob matches at least one git-visible file, so a renamed directory cannot silently stop a rule from loading;
- every skill's `SKILL.md` under `skills_dir` has a `name` equal to its directory, unique among skills, and a non-empty `description`;
- no frontmatter key repeats;
- the `instruction_map` section (the `heading` in `file`) lists ``- `label`: `glob`, …`` lines that equal the rule files' stems and their `paths` exactly.

## Freshness and the restart rule

Every cache is keyed by a fingerprint of exactly the files it reads (and of the config file), so any edit, addition or deletion is picked up by the next call; `refresh` is only for troubleshooting.

- **TypeScript facts** are cached per file by stamp. The cache as a whole is keyed by the Node version, path and binary stamp, the TypeScript directory and version, the stamp of the compiler file the helper loads, and a digest of the helper scripts, so upgrading TypeScript or Node, or reinstalling or patching the same version, drops it automatically.
- **The backend model** is keyed by the stamps of every `.py` file under the backend package, the Python version file, the lock and the library capture file; a rebuild re-parses and re-analyzes only the changed modules, then reassembles the graph. DI is built from the model.
- **The frontend routes scan** is keyed by the stamps of every frontend source file plus the tsconfig and the lockfile, and the toolchain stamp. A missing frontend or an unusable toolchain is cached like a scan until that key changes; a crashed helper is not cached as an answer, so the next routes call runs it again.
- **Routes** are keyed by the backend fingerprint and the scan's key. After a backend-only edit the cached scan is matched against the rebuilt backend model, so the helper does not run again.
- **Stamps.** A file's stamp is its modification time, size, inode and status-change time (ctime). As with git's `core.trustctime`, ctime is what catches a same-size rewrite whose modification time was set back: the kernel updates ctime on every content or metadata change, and nothing can set it back. On Windows ctime is the creation time, so there only the other three fields count.
- **Racy-clean rule.** As in git, a file changed (by modification time or ctime) within two seconds of being stamped may change again without changing its stamp. The content of those files is hashed when the fingerprint is taken, and a cache built from them is kept only while their content still matches: while the window lasts it stays loaded but not `fresh`, a real edit inside the window drops it and the next call rebuilds, and after the window equal content confirms it.
- **The toolchain** is probed again on every call while it is unavailable, and whenever the TypeScript package's `package.json` or the compiler file it names changes, or the node a probe would choose is a different path or file.
- **Docs** (`doc-check` and `doc_refs`): each doc's Markdown facts and each tracked code file's identifier words are cached per file by stamp, under the racy-clean rule, so after an edit only that file is read again. The file list comes from git on every call, DI and the symbol index come from their own caches, and resolution, reachability and the ignore entries run on every call.

**The restart rule.** Python modules are imported once, so a running server keeps the code it started with. The server records a digest of the exact bytes each of its modules was compiled from, and of the repository's `.code-intel/requirements.txt`. **After a pin bump (or a `git pull` of an editable install), restart the code-intel MCP server in your client.** Until then every reply leads with `⚠ code-intel changed since this server started (its code, or the version pinned in .code-intel/requirements.txt); restart the code-intel MCP server in your client to load it. These results come from the old code.` and JSON carries `"analyzer_code_stale": true`. A module that was modified so close to its load that its bytes cannot be compared makes replies lead with `⚠ Cannot confirm that the analyzer code this server loaded matches the files on disk; restart the code-intel MCP server in your client to be sure.`, with `"analyzer_code_stale": null` and `analyzer_code_unconfirmed` listing each `{"file", "reason"}`. The TypeScript helper (`ts/*.cjs`) and the TypeScript package need no restart. Config edits need no restart: the file is read on every call.

The runtime serializes access to its caches with a lock, so concurrent tool calls cannot corrupt them.

### Old interpreter

When the interpreter is older than the backend's (`backend.python_version_file`), backend source cannot be parsed reliably, so the server refuses instead of returning results that silently miss files:

- `di_deps`, `di_reverse`, `route_match`, `route_unmatched`, `doc_refs`, `refresh`, and `symbol_resolve` with `side="backend"` or `"both"` return `runtime_incompatible` with the running and required versions, before any analysis;
- `status` keeps working and names the interpreter; with `load_counts=true` it runs nothing and returns `counts: null` with `counts_error`;
- `jsx_usage` and `symbol_resolve` with `side="frontend"` keep working.

The tool itself requires Python 3.14, so this refusal applies when the backend's version file names a newer interpreter. `doc-check`, `verify`, `summary`, `runtime-check` and the `dump` commands refuse the same way.

## Performance

Measured with the launcher's Python 3.14.7, Node 26.13.0 and TypeScript 7.1.0-beta on an Apple M4 laptop, on a real-world repository of the shape this tool targets: 301 backend files (369 routes, 14 dependency targets), 468 frontend source files (about 11,000 JSX elements and 12,000 declarations), 30 docs with 543 references, 1,407 tracked files in all. Each figure is the median of three runs, in-process, with a warm OS file cache. "Cold" is a fresh runtime; "after an edit" appends a line to one file and calls again 2.2 s later, past the racy-clean window:

| Analysis | Cold | Warm | After editing one file |
| --- | --- | --- | --- |
| DI | 2.21 s | 0.04 s | 0.25 s (a backend `.py`) |
| Routes (alone) | 2.64 s | 0.04 s | 1.05 s (a frontend `.ts`) |
| JSX usage | 0.57 s | 0.04 s | 0.14 s (a `.tsx`) |
| Symbol resolve (`side="both"`) | 0.97 s | 0.06 s | 0.15 s (a `.tsx`) |
| Docs report (DI, symbols and every scope doc) | 5.87 s | 0.19 s | 0.22 s (a scope `.md`) |

Routes cost about the same with or without a prior DI call: the TypeScript helper runs beside the backend build, so the slower of the two sets the time. The `routes` helper builds one program over the whole frontend, so a frontend edit runs it again; a backend edit reuses its cached scan and pays only for the incremental model rebuild and matching. DI, JSX and symbols re-read only the changed file.

End to end through the launcher, every command starts cold (wall time, and the peak memory of the Python process; the TypeScript helper runs as a child process with its own memory):

| Command | Wall time | Peak memory |
| --- | --- | --- |
| `doc-check` | 6.3 s | 361 MB |
| `dump routes --json` | 3.2 s | 298 MB |
| `summary` | 8.0 s | 361 MB |
| `verify` | 12.6 s | 372 MB |

`status` shows each analyzer's last run time for the running server and the facts cache's hit and helper-run counters.

## Verification

Three commands check the analyses on the repository they run in. They assert relations between analyzers and between analyzers and the source, never counts or names copied from the code.

### `verify`

Runs sixteen checks, one line each (`PASS`, `FAIL`, `ERROR` for a check that could not run, `SKIPPED`), and lists up to 20 offending rows under a failing one. Name checks on the command line to run only those.

| Check | The relation |
| --- | --- |
| `backend-content` | The backend model lost no content, and every limitation kind it reported is registered. |
| `handler-locations` | Every live route's `file:line` holds its handler's `def` (a lambda endpoint's line holds the `lambda` its id names), and its `module:qualname` id resolves to that definition. For a route installed code defines, the line holds the `include_router` call that mounts it. |
| `python-index` | Every module-level Python function and class is in the symbol index. |
| `routes-content` | The routes scan lost no content. |
| `di-matches-routes` | The dependency map and the routes scan (repository and library routes) list the same routes. |
| `call-sites` | Every frontend call row points at a call of its kind: the callee is where `callee_line:callee_column` says (judged by position, so an aliased import, a callee on the next line and a concatenated URL pass), and a WebSocket row starts a URL expression. |
| `matched-backends-are-live` | Every matched backend row is a live route. |
| `library-capture` | The capture file equals what `capture` records now. It starts the app, like `capture` and `runtime-check`, and is `SKIPPED` without `[backend.capture]`; it is `ERROR` (with the fix) when the interpreter is missing or the app does not import. |
| `jsx-oracle` | The helper's JSX elements equal an independent walk of the TypeScript AST (`oracles/jsx_oracle.cjs`) in file, tag, line, column, kind, parent and test flag. |
| `declaration-oracle` | The helper's declarations equal an independent walk (`oracles/decl_oracle.cjs`) in name, line, column, kind, end line, scope, container, exported and test flag. |
| `docs-content` | The docs report lost no content. |
| `docs-reference-lines` | Every reference's text starts on the line it reports (a wrapped code span starts on its first line; symbols and paths outside a fence sit between backticks). |
| `docs-failure-messages` | Every failing reference has a specific message, not just `<kind> <status>`. |
| `docs-routes-are-live` | Every `ok` route reference's handler location is a live route's, and its target is one of the routes defined there. |
| `docs-code-words` | The symbol check's two sources agree: a Python definition the index holds is also an identifier word of its file. |
| `docs-lookups` | `doc_refs` finds a documented route, a documented symbol and a doc's inbound links the way the report holds them. |

The two oracle checks compare only git-visible files. The `docs-*` checks are skipped when the config has no `[docs]` table and `library-capture` when it has no `[backend.capture]` table. Checks that need the frontend toolchain report `ERROR` with the `setup` fix when it is missing. Exit code: 0 when every check passed or was skipped, 1 when a check failed, 2 when a check could not run and none failed.

### `summary`

Prints one Markdown table per analyzer, for a job summary (`code-intel summary >> "$GITHUB_STEP_SUMMARY"`): the app, FastAPI version and include model, routes, dependency targets, files scanned, diagnostics and limitations by kind and the cold analysis time; the route figures with their bounds and coverage; JSX elements and TypeScript declarations by kind; Python symbols by kind; and the docs (references, checked, findings). A section that crashes prints a `Failed` row naming the analyzer and the error instead of costing the other sections; one that cannot run prints `Unavailable` with the fix, and one that does not apply prints `Skipped`. The exit code is 1 only when a section failed.

### `runtime-check`

Imports the configured app with a Python that has the app's dependencies installed and compares what FastAPI built with the dependency map:

```sh
code-intel runtime-check --python backend/.venv/bin/python \
  --set FEATURE_FLAG=true --env-file .env.test
```

`--python` defaults to `python` in `[backend.capture]`; without either the check cannot run. A relative `--python` is taken from the current directory, then the repository root; it is not resolved through links, so a virtual environment's interpreter is found. `--set KEY=VALUE` (repeatable) and `--env-file FILE` (`KEY=VALUE` lines; an existing variable wins) add to the `env` of `[backend.capture]`; together they set what the app needs to import and mount its routers; give the flags and synthetic secrets the routes depend on, never real secrets. The app is imported from an empty temporary working directory with a clean environment: only `PATH`, `HOME`, `TMPDIR`, `LANG` and `LC_ALL` (when set) are inherited, so a local `.env`, a stray file or an ambient variable cannot change the routes. Version 0.1 inherited the whole environment; since 0.2 it does not.

It compares routes, handlers and their `def` locations, dispatch order, route-level and parameter dependencies, dependency closures and `redirect_slashes`, and prints the differences by class: `route_missing`, `route_extra`, `order_mismatch`, `handler_mismatch`, `location_mismatch`, `route_deps_mismatch`, `via_mismatch`, `param_deps_mismatch`, `closure_mismatch` and `redirect_slashes_mismatch`. A note appears when the app ran on a different FastAPI version than the one the analysis follows (the locked one), because the include model may differ.

Some differences are known blind spots of a static analysis; they are listed as **explained**, with their reason, and do not fail the check:

- a route the runtime serves from installed code (the oracle attributes the handler to an installed distribution), when the analysis reported a limitation that can hide routes (`unresolved_include`, `opaque_router_call`, `library_capture_stale`, …), is explained by those limitations, listed with their locations;
- a dependency difference located at a dependency the analysis cannot see into (an `external` or `unresolved` dependency the [library capture](#library-routers-and-dependencies) has no entry for) is explained when the runtime dependency standing in its place, and everything beneath it, accounts for the whole difference. Every other dependency of the route, at every depth, must still agree.

What the capture fills is compared like everything else: an installed handler's location compares as (distribution, file inside it, line) against the route's `library`, and an external binding the capture filled stands for the id FastAPI reports for it (`runtime_id`; several bindings can share one, as `current_user(active=True)` and `current_user(superuser=True)` do).

Everything else is **unexplained**: an extra route, a changed handler or location, a different order, a dependency that differs where the analysis can see. Exit code: 0 when nothing is unexplained, 1 otherwise, 2 when the check could not run (no usable interpreter, the app did not import — the error is shown —, or the analysis is incomplete).

### Tests and oracles

The tests compare the analyzers with the tools they model instead of with hand-written expectations:

- **FastAPI oracle** (`oracles/fastapi_oracle.py`): imports a FastAPI app and walks `app.routes` (through `fastapi.routing.iter_route_contexts` on FastAPI 0.137.2 and later, so a live include's routes carry its prefix and dependencies), recording each route's method, path, endpoint, `def` line, route-level dependencies, parameter dependencies and closure, in Starlette order. The oracle tests write every case to a temporary directory, run the oracle once, and require the analyzers to produce the same routes and dependencies. To add a case, add an entry to `CASES` in `oracles/fastapi_cases.py` (routes) or `DEPENDENCY_CASES` in `oracles/fastapi_dependency_cases.py` (dependencies). A case FastAPI builds in a way the analyzer cannot read from source goes in the `INEXACT` tables with the limitation kinds that explain the difference: it must report them, and the test fails once the case matches the runtime, so the entry gets removed. The route cases run under both include models. The library cases (`tests/library_cases.py`) put an installed library in a temporary site directory (a router factory that is a method of a generic-subscripted class, a module-level factory, a router that includes a nested one, a signature set at runtime, a closure, a class used as `Depends()`, a callable passed in from the package), run `capture`, the analysis and the oracle, and require zero differences under both include models.
- **`fastapi_oracle.py --compare RUNTIME.json DI.json`** reports the differences by the classes above and exits 1 when any class is non-empty; `runtime-check` is this comparison plus the explanations.
- **TypeScript oracles** (`oracles/jsx_oracle.cjs`, `oracles/decl_oracle.cjs`, compared by `oracles/compare_facts.py`): independent walks of the TypeScript AST that the helper's JSX elements and declarations must equal exactly, on fixtures and, through `verify`, on the repository.
- **Docs check** tests: Markdown lines and GitHub slugs, git-visible files against real git repositories in temporary directories, what counts as a real use of a name, every reference kind, rule, status, config error and ignore entry over fixture repositories, and the CLI's output and exit codes. Planted drift (a stale path, a broken anchor, a removed route, an orphaned doc) must print its exact `path:line: message` line and exit 1, end to end over a real git checkout.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run pyrefly check
git ls-files '*.cjs' | xargs -n1 node --check
uv run code-intel setup
CODE_INTEL_REQUIRE_TOOLCHAIN=1 uv run pytest -n auto
```

The dependency groups `fastapi-live` (default) and `fastapi-copy` select the FastAPI the oracle tests run against; install one at a time: `uv sync --no-default-groups --group dev --group fastapi-copy`. Tests that need Node, TypeScript, git or FastAPI skip when they are missing; `CODE_INTEL_REQUIRE_TOOLCHAIN=1` (set in CI) turns those skips into failures. The code has no type-check suppressions; keep it that way.

Layout of `src/code_intel/`: `server.py` registers the tools, validates arguments and maps failures to error bodies; `analysis_runtime.py` owns every cache, its freshness and the analyzer-code staleness check; `queries.py` and `doc_queries.py` hold the pure filtering, matching and suggestion logic; `presenters.py` and `doc_presenters.py` render text and JSON through one envelope. Backend: `py_sources.py`, `py_escapes.py`, `py_values.py`, `router_bases.py`, `router_graph.py`, `include_model.py`, `dependency_resolver.py` and `di_map.py`; the library capture is `library_capture.py` (the file format), `library_view.py` (what the analysis applies and checks), `capture.py` and `runner.py` (the command and the clean-environment subprocess). Frontend: `ts_bridge.py`, `ts_facts.py`, `ts/*.cjs`, `route_map.py`, `route_matching.py`, `jsx_usage.py`, `symbol_resolve.py`. Docs: `markdown_facts.py`, `repo_files.py`, `code_words.py`, `doc_rules.py`, `doc_refs.py`, `doc_report.py`, `doc_resolvers.py`, `pages.py`, `agent_files.py`, `doc_check.py`. Commands: `cli.py`, `setup.py`, `verify.py`, `summary.py`, `runtime_check.py`.

## License

MIT, see [LICENSE](LICENSE).
