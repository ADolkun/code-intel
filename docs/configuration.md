# Configuration

The analyzed repository carries `.stack-intel/config.toml`. Every path in it is relative to the repository root, uses forward slashes and has no `..`. Unknown keys, unknown `kind`s and missing required keys are errors that name the key and the file. The file is read again on every call and is part of every cache's fingerprint, so a config edit needs no restart.

`[backend]`, `[frontend]`, `[docs]` and `[agents]` are each optional, but the config must have at least one of them (`[symbols]` alone does nothing): otherwise `config-check` fails with `the config configures nothing: add at least one of [backend], [frontend], [docs] or [agents]`. The full example:

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
skill_dirs = ["skills"]
strict = true
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
```

## Partial configs

A config may leave out `[backend]`, `[frontend]` or both. Each tool and command then answers exactly for the tables it has, and nothing that needs a missing table reports success: it answers [`not_configured`](tools.md#response-contract), is skipped, or carries a `not_configured` limitation.

```toml
# a backend-only API service
[backend]
root = "."
package = "app"
app = "app.main:app"

[docs]
scope = ["README.md", "docs/**/*.md"]
```

```toml
# docs only
[docs]
scope = ["README.md", "docs/**/*.md"]
```

```toml
# a frontend only
[frontend]
root = "web"
tsconfig = "web/tsconfig.json"

[docs]
scope = ["docs/**/*.md"]
```

| Tool | No `[backend]` | No `[frontend]` | No `[docs]` |
| --- | --- | --- | --- |
| `di_deps`, `di_reverse` | `not_configured` (`["backend"]`) | answers | answers |
| `route_match`, `route_unmatched` | `not_configured`, naming each missing side | same | answers |
| `jsx_usage` | answers | `not_configured` (`["frontend"]`), even with an explicit `root` | answers |
| `symbol_resolve` | `side="backend"`: `not_configured`; `side="both"`: frontend candidates only, with a `not_configured` limitation | `side="frontend"`: `not_configured`; `side="both"`: backend candidates only, with a `not_configured` limitation and no `frontend_unavailable` | answers |
| `doc_refs` | answers; route mentions are `not_checked` unless a frontend page matches; a `route` query has `definitions: null` and says `Routes not analyzed: no [backend] table in <config>.`; a `not_configured` limitation | answers; no frontend pages or TypeScript definitions; a `not_configured` limitation | `not_configured` (`["docs"]`) |
| `status` | answers: the analyzers that need the table are `not_configured` | answers; `toolchain.state` is `not_configured` and nothing is probed | answers |
| `refresh` | drops the caches; nothing to rebuild, so `complete` is `null` | rebuilds DI only; the routes counts are `null` | answers |

Neither side configured: `symbol_resolve` is `not_configured` with `["backend", "frontend"]`, and a `doc_refs` `symbol` query has `definitions: null` (`Symbol index not built: …`). A `not_configured` answer describes the config, not the machine: it never sets `complete: false` and is not worth retrying. Every tool checks its arguments first, then its tables, then the interpreter floor.

Without `[backend]` the interpreter floor is the tool's own (Python 3.14): docs that name Python files still have their code words parsed, and a file needing a newer Python is a `parse_failed` diagnostic.

## `[backend]`

Optional ([Partial configs](#partial-configs)); without it there is no DI, route matching, Python symbol index or runtime check.

| Key | Required | Meaning |
| --- | --- | --- |
| `root` | yes | The import root: the directory holding the package. `.` when the package sits at the repository root. |
| `package` | yes | The Python package to analyze, without importing it. |
| `app` | yes | The FastAPI app as `module:attribute`, or `module:factory()` for an app factory. A factory validates but is not analysed yet: it is reported as `unresolved_app`, and `runtime-check` can still import it. |
| `lock` | no | A `uv.lock` (or any file in that format) whose locked `fastapi` version picks the [include model](analysis.md#how-the-backend-analysis-works). Without it the model is `unknown`. |
| `python_version_file` | no | A `.python-version` file. Backend analysis is refused (`runtime_incompatible`) on an interpreter older than the version it names. |

## `[backend.capture]`

Optional. Its presence turns on the [library capture](analysis.md#library-routers-and-dependencies): the analysis applies the capture file, and `capture`, `verify`'s `library-capture` check and `runtime-check` can start the app.

| Key | Required | Meaning |
| --- | --- | --- |
| `python` | yes | An interpreter with the app's dependencies installed (a virtual environment's, for example). Repository-relative; it need not exist for `config-check`. |
| `file` | no | The capture file. Default `.stack-intel/library-capture.json`. |
| `env` | no | Environment variables the import sees besides `PATH`, `HOME`, `TMPDIR`, `LANG` and `LC_ALL`. Synthetic values only: the file is committed and shown in messages. |

## `[frontend]`

Optional ([Partial configs](#partial-configs)); without it there is no route matching, JSX usage, TypeScript symbol index or frontend pages, and Node and TypeScript are never needed.

| Key | Required | Meaning |
| --- | --- | --- |
| `root` | yes | The frontend directory. JSX and symbol scans default to it. |
| `tsconfig` | yes | The tsconfig whose options (paths, base URL, JSX) the routes analysis uses. Only the options matter; the file set is the walk. |
| `lockfile` | no | The frontend lockfile; part of the routes fingerprint. |
| `clients` | no | The calls that count as API calls; see below. Without any, no frontend call is found. |
| `pages` | no | The frontend pages the docs check knows; see below. |

## `[[frontend.clients]]`

A call is a frontend API call when its callee resolves, through the TypeScript checker, to a configured client. `kind` is required and decides the other keys; a key that does not belong to the kind is an error.

| `kind` | Keys | Recognizes |
| --- | --- | --- |
| `call` | `module`, `export`, `url_arg` (0), `init_arg` (1), `default_method` (`GET`) | A function exported from `module`: `export(url, init)`. The method is the init object's `method` property, else `default_method`. |
| `options` | `module`, `export`, `options_arg` (0), `url_property` (`url`), `default_method` | A function taking one options object that carries the URL: `export({ url: … })`. |
| `instance` | `module` + `export`, **or** `package` (+ optional `export`, default `"default"`); `base_path` | A client instance: `<export>.get/post/put/patch/delete/head/options(url, …)` with the method taken from the member name, and `<export>.request({ url, method })` and `<export>({ url, method })`. `base_path` (starting with `/`) is prepended to every URL. `module` and `package` are exclusive: `module` names a file in the repository that exports the instance, `package` an npm package whose export is the instance (a bare `import axios from "axios"`). |
| `global` | `name`, `url_arg` (0), `init_arg` (1), `same_origin_only` (false), `default_method` | A global function such as `fetch(url, init)`. With `same_origin_only`, an absolute URL is another service: it yields no call and no diagnostic. A local function or import that shadows the name is not the global. |

`default_method` is one of `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`, `OPTIONS`. Aliases are followed: renamed, default and namespace imports and re-exports for every kind, and for function clients also `await import()`, `require()`, destructuring and `const f = client`. A missing client module, or a module that no longer declares the export, is a `frontend_client_module_missing` diagnostic; the other clients are still analyzed. A reference to a client that is not a readable call is an `unparsed_frontend_callsite` diagnostic.

URL builders are found in the modules of the configured clients: exported functions whose every return is a path. A builder call outside a client call is a consumer of its path; inside a client URL it contributes to the URL.

## `[frontend.pages]`

| Key | Meaning |
| --- | --- |
| `kind` | `none` (default), `next-app` or `react-router`. |
| `modules` | Required for `react-router`, rejected otherwise: the files whose `<Route path="…">` elements define the pages. |
| `public_dir` | Every file under it is served at its path below it, and counts as a page. |

`next-app` reads pages and route handlers from the `app/` tree under `frontend.root` (`page.*` and `route.*` files with a `.ts`, `.tsx`, `.js` or `.jsx` suffix; `(group)` and `@slot` directories do not appear in the URL, a `_private` directory is not routable; `[id]` is a parameter, `[...rest]` and `[[...rest]]` catch the rest). `react-router` reads the string-literal `path` of every `<Route>` in `modules` through the TypeScript helper (so it needs Node and TypeScript), joins nested paths onto the nearest enclosing route's, and understands `:id`, optional `:id?` (the page with and without it) and a trailing `*`. See [React Router limits](analysis.md#react-router-limits).

## `[symbols]`

`primary_prefixes`: repository-relative path prefixes whose definitions rank first in `symbol_resolve` (see [Symbols](#symbols)).

## `[docs]`

| Key | Meaning |
| --- | --- |
| `scope` | Required, non-empty. Globs of the Markdown files to check, if git sees them. |
| `route_prefixes` | Prefixes a mention may add or omit: with `["/api"]`, a doc may write `GET /items` for the route `/api/items`, or `GET /api/items` for an unprefixed one. Each starts with `/` and does not end with one. |
| `reachability` | Optional table `root`, `include`, `exclude`. Docs matching `include` (and not `exclude`) must be reachable from `root` through links, else they are `orphaned doc` findings. Without the table there is no orphan check. |
| `rules` | `[[docs.rules]]`: `paths` (globs), `checks` (a non-empty subset of `links`, `paths`, `routes`, `symbols`, `scripts`) and a required `reason`. A doc gets every check unless the first rule whose `paths` match it names fewer. |
| `ignore` | `[[docs.ignore]]`: `file`, `kind` (`link`, `path`, `route`, `symbol` or `script`), `text` and a required `reason`. Suppresses the failing references of that kind in that file whose text, as written, equals `text`. An entry that suppresses nothing is itself a finding. |

## `[agents]`

Optional. The agent instruction files at their conventional locations are checked without it; the table only adds to that. See [Agent instruction files](docs-check.md#agent-instruction-files).

| Key | Meaning |
| --- | --- |
| `skill_dirs` | More Agent Skills roots, each holding `<name>/SKILL.md`, held to the Agent Skills rules. Repository-relative; a conventional root (`.claude/skills`, `.github/skills`, `.agents/skills`) or `.` is an error, and a directory listed twice counts once. |
| `strict` | `true` adds this repository's policy to the checks: every `.claude/rules` file is scoped (frontmatter with `paths`, no `applyTo`), the Agent Skills rules apply to every skill root, every directory under a skill root holds a `SKILL.md`, and frontmatter that is not clean YAML and repeated keys are reported. Default `false`. |
| `instruction_map` | `file` and `heading`: a section whose ``- `label`: `glob`, …`` lines must equal the `.claude/rules` files' stems and their `paths` exactly. |

`rules_dir` and `skills_dir` were removed in 0.3; `config-check` says what replaces them.
