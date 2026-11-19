# Tools

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

## `di_deps` / `di_reverse`

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

With a [library capture](analysis.md#library-routers-and-dependencies), a dependency of installed code is a target of kind `library`, shown with its distribution and the definition (`OAuth2PasswordBearer  [fastapi 0.162.0 fastapi/security/oauth2.py:536]`), and an external binding the capture filled shows the expression it is bound to and its captured sub-dependencies (`current_active_user (external) … = fastapi_users.current_user(active=True)`). The route a library defines shows its handler as `me()  [backend/app/main.py:170 · fastapi-users 15.0.5 fastapi_users/router/users.py:46]`: the repository `include_router` that mounts it, then where the library defines the handler.

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

## `route_match` / `route_unmatched`

The summary reports `Backend routes`, `Frontend calls` (non-test, with the number of test-file calls skipped), `Matched routes` (backend routes with at least one caller), `Matched calls` (frontend calls with a backend route), `Match rows` (route/call pairs) and `Coverage` (matched routes over backend routes; `n/a` while the analysis is incomplete). A `possible` match is marked `(possible: <reason>)` in text; see [Matching](analysis.md#matching).

**Library routes** (routes installed code adds, known through a [library capture](analysis.md#library-routers-and-dependencies)) match calls like any route, but are kept out of `backend_routes`, `backend_only` and coverage: the repository neither owns them nor decides whether they are called. The summary counts them apart (`Library routes: 8 (3 matched)`; `library_routes`, `matched_library_routes`) and the JSON lists them under `library_routes`; every route row has `library`, `null` or `{dist, version, file, line}`. For such a row `file` and `line` are the repository's `include_router` call.

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

Without the frontend (see [Requirements](install.md#requirements)) the reply makes no orphan claim:

```
⚠ INCOMPLETE — the frontend was not analyzed, so frontend-derived results are n/a.

Frontend not analyzed: node not found on PATH. Fix: install Node.js 18 or newer and put node on PATH, or set STACK_INTEL_NODE to the node binary
No frontend consumer can be confirmed or ruled out until the frontend is analyzed.

Backend routes that fit the query (1):
  GET /api/items  →  list_items()  [backend/shop/main.py:10]
```

## `jsx_usage`

Returns `file`, `line`, `column`, `snippet`, `parent_function`, `tag_kind` (`open` or `self_close`) and `is_test` for every `<Component …>` and `<Component … />` whose tag equals `component_name` exactly (member tags such as `Ctx.Provider` included). `root` (default: the frontend root) must be a directory inside the repository, or the tool returns `invalid_root`; a root inside a frontend root the checkout does not have is a `frontend_unavailable` error with `problem: "frontend_not_found"`. `include_self_close` (default true) and `include_tests` (default false: test, spec and mock files are not product usage) filter the rows, and the text header states both filters. See [JSX usage](analysis.md#jsx-usage) for the rules.

```
Found 2 JSX usages of <Row> across 1 file(s) (test files excluded):

  frontend/components/ItemList.tsx:9:7  self_close in ItemList
    <Row onLoad={loadItems} />
  frontend/components/ItemList.tsx:10:7  self_close in ItemList
    <Row onLoad={() => removeItem(1)} />
```

## `symbol_resolve`

Returns ranked candidates with `name`, `file`, `line`, `end_line`, `kind`, `scope`, `container`, `exported`, `is_test`, `signature` (the declaration header), `language`, `score` and `file_hint_match`, `protocol_method` and `protocols` (Python methods; see [Calls through a Protocol](analysis.md#calls-through-a-protocol)). `kind` filters by kind (`none` means any); `file_hint` is a substring or glob of the path, and a matching candidate scores 1.0. `side` selects `backend`, `frontend` or `both`. See [Symbols](configuration.md#symbols).

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

## `doc_refs`

Takes exactly one of `file`, `route`, `symbol` or `doc`, plus `limit` and `output_format`; none or two of them is an `invalid_argument`. `file` and `doc` are repository-relative paths (an absolute path inside the repository is converted, one outside it is refused, and `doc` must end in `.md`), `route` is `METHOD /path` or `/path`, and `symbol` is a name or a dotted name. The answer comes from the [docs check](docs-check.md)'s own extraction and resolution, so a mention's `status` is the one `doc-check` gives it.

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

## `status`

Reports the FastAPI version `backend.lock` locks and the include model it gives (`fastapi`: `fastapi_version`, `include_model`, `include_model_reason`, `source`, `summary`), `complete`, per-analyzer state (`not_run`, `complete`, `incomplete` or `failed`, with the failed step and error, or `not_configured` when the config lacks a table the analyzer reads: `[backend]` for `di_map`, both sides for `route_map`), the configured tables (`config.tables`), cache freshness (`backend_model`, `di`, `routes`), the toolchain probe, the deduplicated diagnostics and limitations of the loaded analyses and, with `[backend.capture]`, the `library_capture` block (the file, its app and distributions, how many includes, routes and dependencies it holds and, once DI is built, how many were applied, and the stale findings; the status never builds anything or starts the app). A cache whose sources were modified within the last two seconds stays loaded but is not `fresh` until the window has passed (see [Freshness](analysis.md#freshness-and-the-restart-rule)).

`complete` counts only the configured analyzers, and is `null` when none is configured. The toolchain state is `ok`, `unavailable` (the probe found a problem), `helper_failed` (the probe is fine, but the last TypeScript helper run in some mode crashed) or `not_configured` (no `[frontend]`: nothing needs the toolchain, so it is never probed or installed). `helper_failures` maps each helper mode whose last run crashed (`facts`, `routes`) to its problem; an entry clears when a run in that mode succeeds. The probe itself never runs the helper:

```
Frontend toolchain: node v26.13.0, typescript 7.1.0-beta (~/.cache/stack-intel/ts/<digest>/node_modules/typescript): ok
Frontend toolchain: unavailable: …/node_modules/typescript/package.json not found. Fix: run "stack-intel setup" (or set STACK_INTEL_TYPESCRIPT_DIR to a typescript package directory). DI tools are unaffected; JSX usage, frontend symbols and route matching need it.
```

`load_counts=true` runs DI and routes and adds `counts: {"di": {...}, "routes": {...}}`. Each analyzer runs on its own: one that fails leaves its counts `null` with the error under `counts_errors`, and routes counts are `null` with a `frontend_unavailable` error when the frontend could not be analyzed, or with the `not_configured` error body (text: `routes: not configured: …`) when the config lacks a table the analyzer reads. With no analyzer configured, `Complete` reads `n/a (no analyzer configured)`. On an old interpreter nothing runs: `counts` is `null` and `counts_error` names the refusal.

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
| `not_configured` | `tables`, `config`, `fix` | the tool needs config tables the config lacks: `tables` lists them in the order the tool names them (`["backend"]`, `["backend", "frontend"]`), `config` is the config file, `fix` says what to add. It describes the config, not the machine, so it is not worth retrying. Text: `di_deps needs the [backend] table, and .stack-intel/config.toml has none. Fix: add [backend] with root, package and app.` |
| `internal_error` | `tool` | an unexpected exception in the tool itself; the message names the file, line and function that raised, and the server log has the traceback |

`frontend_unavailable` `problem` codes: `node_not_found`, `node_failed`, `node_too_old`, `typescript_not_found`, `typescript_unreadable`, `typescript_unsupported` (TypeScript older than 7.1) and `toolchain_incomplete` (the probe returned no problem, yet node or TypeScript is missing) from the toolchain probe; `frontend_provisioning` when the MCP server is installing TypeScript in the background and the answer will be possible shortly; `frontend_not_found` when the checkout has no frontend root; and `helper_failed` when Node and TypeScript are present but the helper crashed (its message names the helper mode and the failed step; the fix is to [run the helper by hand](commands.md)). `symbol_resolve` with `side="both"` keeps its backend candidates and carries the code in its diagnostic's `problem`.

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
| `unresolved_include` | an `include_router` child could not be resolved to a router (for example a router built by an installed library's factory function that the [library capture](analysis.md#library-routers-and-dependencies) has no entry for; the message then says so) |
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
| `library_capture_stale` | the library capture does not match the code: a captured distribution's version differs from the lock (the capture is still applied), or an entry matches nothing the analysis finds, names a callable the package lacks, is a binding captured for another expression, or is for another app (not applied); the message names it and says to run `stack-intel capture` |
| `opaque_decorator` | a package decorator returns a wrapper that does not copy the function's metadata (`functools.wraps` or `functools.update_wrapper`), so FastAPI reads the wrapper's signature, not the function's |
| `shared_dependency_id` | callables FastAPI reads with different parameters share one dependency id (a `functools.partial` and its function, a factory and the closure it returns, two definitions of one name). The id's `dependency_functions` entry lists one form's sub-dependencies; the message names the id and both lists |
| `unresolved_http_method` | a frontend call's HTTP method could not be evaluated; it is reported as `UNKNOWN` |
| `url_variants_truncated` | a frontend URL has more variants than the evaluator keeps |
| `unresolved_prop_value` | a component prop that feeds a URL receives a non-literal value at some call site |
| `submodule_missing` | a git submodule is not checked out (`git submodule update --init --recursive`), so documentation links and paths into it get status `not_checked`; the docs check still passes |
| `frontend_definitions_unavailable` | Node or TypeScript is unavailable, so the docs check indexes no frontend definitions: frontend symbols named in docs carry no definition location, but their existence still resolves through tracked code, so no status and no exit code changes |
| `frontend_pages_unavailable` | the frontend pages could not be read (Node or TypeScript is unavailable), so a bare path that only a page could satisfy is `not_checked` |
| `not_configured` | the config has no `[backend]` or no `[frontend]` table, so that side is not analyzed; `file` is the config file and the message names what the answer lacks (in `doc_refs` and `doc-check`: route references not checked against backend routes, or no frontend pages and TypeScript definitions; in `symbol_resolve` with `side="both"`: the side that was not searched). It never changes `complete` |
| `frontend_provisioning` | the MCP server is installing TypeScript in the background (first use), so the frontend-derived parts of this answer are n/a; it is added to a reply whose `frontend_unavailable` diagnostic has `problem: "frontend_provisioning"`, and the next call after the install finishes answers in full |

A test keeps these tables in sync with the registries in `problems.py`.
