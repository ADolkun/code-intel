# Verification

Three commands check the analyses on the repository they run in. They assert relations between analyzers and between analyzers and the source, never counts or names copied from the code.

## `verify`

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
| `docs-reference-lines` | Every reference's text starts on the line it reports (a wrapped code span starts on its first line; symbols and paths outside a fence sit between backticks; a Markdown link is reported on the line its `[` opens, where its destination may follow on a later line or sit in a reference definition). |
| `docs-failure-messages` | Every failing reference has a specific message, not just `<kind> <status>`. |
| `docs-routes-are-live` | Every `ok` route reference's handler location is a live route's, and its target is one of the routes defined there. |
| `docs-code-words` | The symbol check's two sources agree: a Python definition the index holds is also an identifier word of its file. |
| `docs-lookups` | `doc_refs` finds a documented route, a documented symbol and a doc's inbound links the way the report holds them. |

The two oracle checks compare only git-visible files. A check whose config tables are missing is `SKIPPED` with the reason (`the config has no [backend] table`), never run against an empty side: `backend-content`, `handler-locations` and `python-index` need `[backend]`; `routes-content`, `di-matches-routes`, `call-sites` and `matched-backends-are-live` need `[backend]` and `[frontend]`; `library-capture` needs `[backend.capture]`; the two oracle checks need `[frontend]`; the `docs-*` checks need `[docs]`, and `docs-routes-are-live` and `docs-code-words` also `[backend]`, because they compare the report with the backend analysis. Checks that need the frontend toolchain report `ERROR` with the `setup` fix when it is missing. Exit code: 0 when every check passed or was skipped, 1 when a check failed, 2 when a check could not run and none failed, or when every selected check was skipped (`verify: no check applies to this config`).

## `summary`

Prints one Markdown table per analyzer, for a job summary (`stack-intel summary >> "$GITHUB_STEP_SUMMARY"`): the app, FastAPI version and include model, routes, dependency targets, files scanned, diagnostics and limitations by kind and the cold analysis time; the route figures with their bounds and coverage; JSX elements and TypeScript declarations by kind; Python symbols by kind; and the docs (references, checked, findings). A section that crashes prints a `Failed` row naming the analyzer and the error instead of costing the other sections; one that cannot run prints `Unavailable` with the fix, and one that does not apply prints `Skipped`: its tables are missing (`Skipped | the config has no [backend] table`, checked first), or the interpreter is older than the backend's. The exit code is 1 only when a section failed.

## `runtime-check`

Imports the configured app with a Python that has the app's dependencies installed and compares what FastAPI built with the dependency map:

```sh
stack-intel runtime-check --python backend/.venv/bin/python \
  --set FEATURE_FLAG=true --env-file .env.test
```

`--python` defaults to `python` in `[backend.capture]`; without either the check cannot run. A relative `--python` is taken from the current directory, then the repository root; it is not resolved through links, so a virtual environment's interpreter is found. `--set KEY=VALUE` (repeatable) and `--env-file FILE` (`KEY=VALUE` lines; an existing variable wins) add to the `env` of `[backend.capture]`; together they set what the app needs to import and mount its routers; give the flags and synthetic secrets the routes depend on, never real secrets. The app is imported from an empty temporary working directory with a clean environment: only `PATH`, `HOME`, `TMPDIR`, `LANG` and `LC_ALL` (when set) are inherited, so a local `.env`, a stray file or an ambient variable cannot change the routes. Version 0.1 inherited the whole environment; since 0.2 it does not.

It compares routes, handlers and their `def` locations, dispatch order, route-level and parameter dependencies, dependency closures and `redirect_slashes`, and prints the differences by class: `route_missing`, `route_extra`, `order_mismatch`, `handler_mismatch`, `location_mismatch`, `route_deps_mismatch`, `via_mismatch`, `param_deps_mismatch`, `closure_mismatch` and `redirect_slashes_mismatch`. A note appears when the app ran on a different FastAPI version than the one the analysis follows (the locked one), because the include model may differ.

Some differences are known blind spots of a static analysis; they are listed as **explained**, with their reason, and do not fail the check:

- a route the runtime serves from installed code (the oracle attributes the handler to an installed distribution), when the analysis reported a limitation that can hide routes (`unresolved_include`, `opaque_router_call`, `library_capture_stale`, …), is explained by those limitations, listed with their locations;
- a dependency difference located at a dependency the analysis cannot see into (an `external` or `unresolved` dependency the [library capture](analysis.md#library-routers-and-dependencies) has no entry for) is explained when the runtime dependency standing in its place, and everything beneath it, accounts for the whole difference. Every other dependency of the route, at every depth, must still agree.

What the capture fills is compared like everything else: an installed handler's location compares as (distribution, file inside it, line) against the route's `library`, and an external binding the capture filled stands for the id FastAPI reports for it (`runtime_id`; several bindings can share one, as `current_user(active=True)` and `current_user(superuser=True)` do).

Everything else is **unexplained**: an extra route, a changed handler or location, a different order, a dependency that differs where the analysis can see. Exit code: 0 when nothing is unexplained, 1 otherwise, 2 when the check could not run (no usable interpreter, the app did not import — the error is shown —, or the analysis is incomplete).

## Tests and oracles

The tests compare the analyzers with the tools they model instead of with hand-written expectations:

- **FastAPI oracle** (`oracles/fastapi_oracle.py`): imports a FastAPI app and walks `app.routes` (through `fastapi.routing.iter_route_contexts` on FastAPI 0.137.2 and later, so a live include's routes carry its prefix and dependencies), recording each route's method, path, endpoint, `def` line, route-level dependencies, parameter dependencies and closure, in Starlette order. The oracle tests write every case to a temporary directory, run the oracle once, and require the analyzers to produce the same routes and dependencies. To add a case, add an entry to `CASES` in `oracles/fastapi_cases.py` (routes) or `DEPENDENCY_CASES` in `oracles/fastapi_dependency_cases.py` (dependencies). A case FastAPI builds in a way the analyzer cannot read from source goes in the `INEXACT` tables with the limitation kinds that explain the difference: it must report them, and the test fails once the case matches the runtime, so the entry gets removed. The route cases run under both include models. The library cases (`tests/library_cases.py`) put an installed library in a temporary site directory (a router factory that is a method of a generic-subscripted class, a module-level factory, a router that includes a nested one, a signature set at runtime, a closure, a class used as `Depends()`, a callable passed in from the package), run `capture`, the analysis and the oracle, and require zero differences under both include models.
- **`fastapi_oracle.py --compare RUNTIME.json DI.json`** reports the differences by the classes above and exits 1 when any class is non-empty; `runtime-check` is this comparison plus the explanations.
- **TypeScript oracles** (`oracles/jsx_oracle.cjs`, `oracles/decl_oracle.cjs`, compared by `oracles/compare_facts.py`): independent walks of the TypeScript AST that the helper's JSX elements and declarations must equal exactly, on fixtures and, through `verify`, on the repository.
- **Docs check** tests: Markdown lines and GitHub slugs, git-visible files against real git repositories in temporary directories, what counts as a real use of a name, every reference kind, rule, status, config error and ignore entry over fixture repositories, and the CLI's output and exit codes. Planted drift (a stale path, a broken anchor, a removed route, an orphaned doc) must print its exact `path:line: message` line and exit 1, end to end over a real git checkout.
