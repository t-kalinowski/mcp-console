# AGENTS.md

Keep this file synchronized with the code that exists in the repository.
The documents under `design-sketches/` describe intended behavior, not implemented behavior.

## Current state

MCP Console is an initial Rust binary package.
The only implemented command is:

```text
mcp-console --version
```

It prints the package name and version.
The MCP server, session model, language runtimes, sidecar API, viewer, sandbox, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL workbench exposed through MCP.
The planned public MCP surface has two tools:

- `console` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
- `console_session` manages session requirements and lifecycle operations.

The initial runtime design uses R as the host, embeds Python through reticulate, and runs SQL through the DuckDB R package and DBI.
The worker backend remains an open design decision.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the tentative architecture.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `src/main.rs` — current binary entry point.
- `tests/cli.rs` — public binary acceptance tests.
- `scripts/check` — local formatting, Clippy, and test checks.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `design-sketches/` — tentative product and architecture documents.
- `README.md` — current user-facing project status.

Add modules only when implemented public behavior needs them.
Begin as one Cargo package and split crates only when a real boundary emerges.

## Working rules

- Keep PRs narrow and easy to review.
  Most PRs should stay under 200 lines of diff, counting additions and deletions.
  A larger PR is acceptable when splitting it would prevent each part from compiling, running, or being reviewed on its own.
- Each PR should implement and test one observable behavior.
  Update design documents in the same PR only when they describe that behavior.
- Add a public acceptance or regression test first and confirm that it fails before implementing behavior.
- Test through public interfaces.
  Do not add tests for private helpers.
- Keep complete code cells separate from interactive `stdin`.
- Keep the MCP adapter independent of interpreter implementation details.
- Treat all runtime execution as shell-class capability and place safety at the worker-process boundary.
- Update this file when a PR changes the implemented surface or repository map.
- Run `scripts/check` before opening a PR.
