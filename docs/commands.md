# Commands

Every command takes `--repo-root PATH` (default: the git work tree containing the current directory) and `--config PATH` (default: `<repo-root>/.stack-intel/config.toml`), except `setup`, which needs neither.

| Command | Does | Exit codes |
| --- | --- | --- |
| `config-check` | Validate the config file and print `ok: <path>`, then `tables: <the configured tables>` (for example `tables: backend, docs`). Unknown keys, unknown client kinds and missing required keys are errors that name the key and the file, and so is a config that configures none of `[backend]`, `[frontend]`, `[docs]` and `[agents]`. | 0 valid, 1 invalid |
| `mcp` | Serve the [tools](tools.md) over stdio. | |
| `setup` | Install the [TypeScript](install.md#install-and-launch) the frontend analysis needs: an explicit prewarm, for CI. Other commands and the MCP server install it on first use. | 0, 1 failed |
| `doc-check [--json]` | Check the [docs](docs-check.md) and the agent instruction files against the code. Without `[docs]` it checks the agent files only. | 0 no findings, 1 findings, 2 could not run, incomplete, or nothing to check |
| `verify [CHECK …]` | Check the analyzers against each other and against the source; see [Verification](verification.md). Its `library-capture` check starts the app. A check whose tables the config lacks is skipped. | 0 passed, 1 a check failed, 2 a check could not run, or every check was skipped |
| `summary` | Markdown counts and coverage of the analysis, one table per analyzer. A section whose tables the config lacks prints `Skipped`. | 0, 1 a section failed |
| `runtime-check [--python PYTHON] [--set K=V …] [--env-file FILE]` | Compare the static route analysis with the running app. Needs `[backend]`. | 0, 1 unexplained difference, 2 could not run or no `[backend]` |
| `capture [--check \| --if-stale] [--python PYTHON]` | Record what installed code contributes to the app (routers library factories build, library dependency trees) in `.stack-intel/library-capture.json`; `--check` compares without writing; `--if-stale` starts the app only when a static check finds the file stale; see [Library routers and dependencies](analysis.md#library-routers-and-dependencies). Needs `[backend.capture]`; `--if-stale` without it (or without `[backend]`) prints a note and exits 0. | 0, 1 `--check` found a difference, 2 could not run, no `[backend]`, or the environment differs from the lock |
| `dump di [--json]` | Dependency-injection map: every route with its dependencies in FastAPI order. Needs `[backend]`. | 0, 1 could not run or not configured, 2 incomplete |
| `dump routes [--json] [--unmatched]` | Backend routes matched with frontend calls. Needs `[backend]` and `[frontend]`. | 0, 1 could not run or not configured, 2 incomplete |
| `dump jsx (COMPONENT \| --all) [--root DIR] [--include-tests] [--no-self-close] [--json]` | JSX usage of a component, or every element. Needs `[frontend]`. | 0, 2 frontend unavailable or not configured |
| `dump symbols (NAME \| --dump-ts \| --dump-py) [--side backend\|frontend\|both] [--kind KIND] [--file-hint TEXT] [--json]` | Where a symbol is defined, or every indexed declaration. `--side both` searches the side the config has. | 0, 2 frontend unavailable or not configured |

The `dump` commands are for checking an analyzer by hand; their JSON is the same schema the tools return. A command that needs a table the config lacks prints the [`not_configured`](tools.md#response-contract) message (`dump di needs the [backend] table, and .stack-intel/config.toml has none. Fix: add [backend] with root, package and app.`) and exits with its "could not run" code.

**Docs check.** `doc-check` needs only Python, git and the packaged Markdown parser. Standard output holds only the findings, one `path:line: message` line each, or with `--json` one JSON document (the [response contract](tools.md#response-contract) envelope with `findings`, `finding_count`, `doc_count` and `reference_counts`). Everything else goes to standard error: the `⚠ INCOMPLETE` banner with every diagnostic, the `ⓘ` limitations, and a one-line summary (`doc-check: N findings in D docs (R references, C checked); A agent files`, ending in `; INCOMPLETE` when the report is incomplete and in `; docs not configured` when the config has no `[docs]`; A counts the agent instruction files read; R counts every reference the docs name, C the ones whose status decides the verdict). No GitHub workflow commands (`::error`) are printed, because messages quote doc text that a pull request controls.

| Code | Meaning |
| --- | --- |
| 0 | No findings. Limitations (an uninitialized submodule, no Node or TypeScript, an unresolved include, a side the config does not configure) are printed and do not count. |
| 1 | Findings. |
| 2 | The check could not run (an interpreter older than the backend's, a git failure, an invalid config, an analyzer error, with the traceback for an internal error), there is nothing to check (no `[docs]` and no agent files: `doc-check: .stack-intel/config.toml has no [docs] table and there are no agent files to check`), or the report is incomplete (a doc or code file could not be read, decoded or parsed, or the app did not resolve), with or without findings, because an incomplete report can hide findings. A diagnostic of an untracked or ignored file does not count, because CI's checkout has no such file. |

What it checks and how to fix a finding: [Docs check](docs-check.md).

**Running the TypeScript helper by hand.** When a tool reports `helper_failed`, run the helper to see the full error. It reads one JSON request on standard input and writes one JSON document on standard output (the protocol is in `ts_bridge.py` and `result_types.py`). The path is `stack_intel.ts_bridge.HELPER`; the TypeScript directory is the one `status` reports:

```sh
echo '{"protocol": 1, "mode": "facts", "typescript_dir": "'"$TS_DIR"'",
       "files": ["'"$PWD"'/frontend/components/ItemList.tsx"], "snippet_max": 500, "signature_max": 200}' \
  | node "$HELPER"
```
