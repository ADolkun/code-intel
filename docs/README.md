# stack-intel documentation

The reference for stack-intel. The [project README](../README.md) has the overview and a quickstart.

- [Install](install.md): requirements, installing and pinning the package, the launcher, the MCP client entry, the automatic TypeScript install, environment variables and worktrees.
- [Commands](commands.md): every command, its options and exit codes, and running the TypeScript helper by hand.
- [Configuration](configuration.md): `.stack-intel/config.toml`, table by table, and partial configs.
- [Tools](tools.md): the MCP tools, their arguments and output, and the response contract with every error code, diagnostic kind and limitation kind.
- [Docs check](docs-check.md): what `doc-check` checks, how to fix a finding, and the agent instruction files.
- [Analysis notes](analysis.md): how the backend and frontend analyses work, the library capture, matching, JSX usage, symbols, freshness and the restart rule, and performance.
- [Verification](verification.md): `verify`, `summary`, `runtime-check`, and the tests and oracles.
- [Development](development.md): working on stack-intel itself, CI and releases.

Changes are listed in the [changelog](../CHANGELOG.md).
