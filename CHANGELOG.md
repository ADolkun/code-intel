# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases up to 0.2.3 were published as `code-intel`, installed from git; the names below are the ones each release used.

## Unreleased

### Added

- `doc-check` checks documented commands beyond npm scripts: pnpm, yarn and bun scripts, make targets and mise tasks, in code spans and shell fences. A command whose outcome the tracked files cannot settle is `not_checked` with the reason, never `missing`.

### Changed

- The docs check `scripts` is renamed `commands`, and the reference kind `script` is renamed `command`. A config that still uses the old names fails with an error naming the new one.
- A `package.json` that cannot be read or parsed no longer adds a `parse_failed` diagnostic (which made the run incomplete): the script references that depend on it are `not_checked`, naming the file and the error. Makefiles, mise files and `.npmrc` files are treated the same way.

## 0.2.3 - 2026-11-05

### Added

- `doc-check` checks documented npm scripts (`npm run <script>`, `npm test`, `npm start`, in code spans and shell fences) against the `scripts` of the `package.json` they run in.
- `doc-check` reports skill directories without a `SKILL.md` and an `.mcp.json` that does not parse.

## 0.2.2 - 2026-11-05

### Added

- `capture --if-stale` starts the app only when the committed capture file is stale, as the analysis would report it. Gaps a capture could not fill are recorded in an optional `unfilled` key, so an attempted gap does not start the app on every run.

### Fixed

- `capture --if-stale` converges: a missing capture file is stale, entries the analysis would not apply move to `unfilled`, and the number of runs of the app is no longer capped.
- The capture file no longer holds machine-specific paths.

## 0.2.1 - 2026-11-05

### Added

- A command or MCP tool that needs the frontend and finds no TypeScript installs it the way `code-intel setup` does. Commands install before running; the MCP server installs in the background and answers with a `frontend_provisioning` problem until it is ready. `CODE_INTEL_NO_AUTO_SETUP=1` turns this off.

### Fixed

- TypeScript is provisioned when `CODE_INTEL_NODE` is set, with the `npm` beside that `node`.

## 0.2.0 - 2026-11-05

### Added

- The `capture` command and the library-capture file: what installed code contributes to the app (library routers, dependencies), recorded by running the app in a clean environment, and a `[backend.capture]` config table.
- The analysis applies the capture to routes, dependencies, route matching, `verify` (a `library-capture` check), `runtime-check`, `status` and `summary`. A capture whose distribution versions differ from the lock is applied with a `library_capture_stale` limitation.

### Changed

- `runtime-check` defaults `--python` to the config.
- Dependency floors raised to the newest stable releases.

### Fixed

- Malformed capture entries are rejected with a `parse_failed` diagnostic, and the oracle's app process is killed as a group on timeout.
- `FastAPIInstrumentor.instrument_app` is recognized under its real module, `opentelemetry.instrumentation.fastapi`, again, and the `route not found` hint no longer names one framework: it points at the limitation when the route may be mounted by code the analysis cannot see.

## 0.1.0 - 2026-11-05

### Added

- Static analysis of a FastAPI backend (routers, dependency injection) and a TypeScript frontend (API calls, JSX usage, symbols, React Router pages), served to coding agents over MCP.
- One config file in the analyzed repository and `config-check` to validate it.
- `setup`, which installs the TypeScript compiler the frontend analysis needs.
- `doc-check`, which checks the docs' paths, anchors, routes and symbols, doc reachability and agent files against the code.
- `verify`, `summary` and `runtime-check`, which check the analyzers against each other and against the running app.
- Every walk is limited to git-visible files.
