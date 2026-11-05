# code-intel

Static analysis of a FastAPI backend and its TypeScript frontend, served to coding agents over MCP.
Every repository-specific fact comes from one config file in the analyzed repository.

Status: scaffold. The package, the config reader and `code-intel config-check` work; the analyzers arrive unit by unit.

## Requirements

- Python 3.14 and [uv](https://docs.astral.sh/uv/)
- Node.js, for the frontend analysis (later)

## Configuration

The analyzed repository carries `.code-intel/config.toml`. Every path in it is relative to the repository root.
Unknown keys, unknown client kinds and missing required keys are errors that name the key and the file.

```toml
[backend]
root = "backend"                     # import root: the directory holding the package
package = "app"
app = "app.main:app"                 # module:attribute, or module:factory() for an app factory
lock = "backend/uv.lock"             # optional: the locked FastAPI decides the include model
python_version_file = "backend/.python-version"   # optional

[frontend]
root = "frontend"
tsconfig = "frontend/tsconfig.json"  # options only; the file set is the walk
lockfile = "frontend/package-lock.json"           # optional

[[frontend.clients]]                 # kind: call | options | instance | global
kind = "instance"                    # <export>.get/post/put/patch/delete/head/options(url, ...)
module = "frontend/src/lib/api.ts"
export = "api"
base_path = "/api"

[[frontend.clients]]
kind = "global"                      # a global function, here fetch(url, init)
name = "fetch"
same_origin_only = true              # an absolute URL is another service

[frontend.pages]                     # optional; kind: next-app | react-router | none
kind = "react-router"
modules = ["frontend/src/App.tsx"]
public_dir = "frontend/public"

[symbols]                            # optional
primary_prefixes = ["frontend/src/", "backend/app/"]

[docs]                               # optional
scope = ["docs/**/*.md", "README.md"]
route_prefixes = ["/api"]            # a mention may add or omit any of these

[docs.reachability]                  # optional; without it there is no orphan check
root = "docs/README.md"
include = ["docs/**/*.md"]
exclude = []

[[docs.rules]]                       # checks: links | paths | routes | symbols
paths = ["docs/imported.md"]
checks = ["links"]
reason = "Imported material: only its links must resolve."

[[docs.ignore]]                      # kind: link | path | route | symbol
file = "docs/guide.md"
kind = "route"
text = "/api/external"
reason = "Served by another service."

[agents]                             # optional
rules_dir = ".claude/rules"
skills_dir = ".claude/skills"
instruction_map = { file = "AGENTS.md", heading = "Scoped instruction map" }
```

Check a config:

```sh
uv run code-intel config-check --repo-root /path/to/repo [--config /path/to/config.toml]
```

`--repo-root` defaults to the enclosing git work tree and `--config` to `<repo-root>/.code-intel/config.toml`.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run pyrefly check
uv run pytest -n auto
```

The dependency groups `fastapi-live` (default) and `fastapi-copy` select the FastAPI the oracle tests run against;
install one at a time: `uv sync --no-default-groups --group dev --group fastapi-copy`.

## License

MIT, see [LICENSE](LICENSE).
