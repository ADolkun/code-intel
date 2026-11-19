# Development

```sh
uv sync
uv run zizmor --quiet .github
uv run ruff check && uv run ruff format --check
uv run pyrefly check
uv run stack-intel doc-check
git ls-files '*.cjs' | xargs -n1 node --check
uv run stack-intel setup
STACK_INTEL_REQUIRE_TOOLCHAIN=1 uv run pytest -n auto --dist worksteal
```

CI runs these on every pull request: `lint` (zizmor, ruff, pyrefly, the docs check and the JavaScript syntax check), `test` on Ubuntu and macOS, and `wheel`, which builds the sdist and the wheel from it (`uv build --no-sources`), on Ubuntu checks them with `twine check --strict`, and runs `tests/wheel_smoke.py` against the wheel installed alone in a clean venv. `tests/wheel_smoke.py` is not a pytest file: it checks the installed package's assets, `--version`, `setup`, `config-check`, `doc-check` (clean, then failing on a renamed route) and the MCP server's tool list and `status` on a fixture repository. A weekly workflow runs the suite against the `beta`, `rc` and `next` TypeScript builds (7.1 or newer) through `STACK_INTEL_TYPESCRIPT_DIR`.

The docs check runs on this repository's own docs with `.stack-intel/config.toml`: `README.md`, `CHANGELOG.md` and `docs/`, with `docs/README.md` as the reachability root. Its ignore entries cover what the reference describes of an analyzed repository, each with its reason. `README.md` is also the project description on PyPI, which renders a relative link as a 404, so its links are absolute URLs or same-page anchors (`tests/test_readme.py`).

Releases: `release.yml` builds once, smoke-tests the wheel on Ubuntu and macOS, and publishes with PyPI trusted publishing and attestations: to TestPyPI when dispatched by hand, to PyPI on a `v*` tag that equals the project version (`uv version --short`). Changes are listed in [CHANGELOG.md](../CHANGELOG.md).

The dependency groups `fastapi-live` (default) and `fastapi-copy` select the FastAPI the oracle tests run against; install one at a time: `uv sync --no-default-groups --group dev --group fastapi-copy`. The tests turn the automatic install off (`STACK_INTEL_NO_AUTO_SETUP=1`), so run `stack-intel setup` first. Tests that need Node, TypeScript, git or FastAPI skip when they are missing; `STACK_INTEL_REQUIRE_TOOLCHAIN=1` (set in CI) turns those skips into failures. The code has no type-check suppressions; keep it that way.

Layout of `src/stack_intel/`: `server.py` registers the tools, validates arguments and maps failures to error bodies; `analysis_runtime.py` owns every cache, its freshness and the analyzer-code staleness check; `queries.py` and `doc_queries.py` hold the pure filtering, matching and suggestion logic; `presenters.py` and `doc_presenters.py` render text and JSON through one envelope. Backend: `py_sources.py`, `py_escapes.py`, `py_values.py`, `router_bases.py`, `router_graph.py`, `include_model.py`, `dependency_resolver.py` and `di_map.py`; the library capture is `library_capture.py` (the file format), `library_view.py` (what the analysis applies and checks), `capture.py` and `runner.py` (the command and the clean-environment subprocess). Frontend: `ts_bridge.py`, `provision.py` (the install on first use), `ts_facts.py`, `ts/*.cjs`, `route_map.py`, `route_matching.py`, `jsx_usage.py`, `symbol_resolve.py`. Docs: `markdown_facts.py`, `repo_files.py`, `code_words.py`, `doc_rules.py`, `doc_refs.py`, `doc_report.py`, `doc_resolvers.py`, `doc_commands.py`, `make_targets.py`, `mise_tasks.py`, `package_manifest.py`, `definition_files.py`, `pages.py`, `doc_check.py`. Agent files: `agent_files.py` (discovery, rules, subagents, Copilot instructions and agents, the instruction map), `agent_frontmatter.py`, `agent_globs.py`, `agent_skills.py`, `agent_json.py` (MCP configs, hooks), `agent_imports.py`. Commands: `cli.py`, `setup.py`, `verify.py`, `summary.py`, `runtime_check.py`.
