# AGENTS.md

Keep this file synchronized with the code that exists in the repository.
The documents under `design-sketches/` describe intended behavior, not implemented behavior.

## Current state

MCP Console is an initial Rust binary package.
The only implemented command is:

```text
mcp-console version
```

It prints the package name and version.
The MCP server, session model, language runtimes, sidecar API, viewer, sandbox, environment management, output retention, and transcript generation do not exist yet.

## Product direction

MCP Console is intended to become a persistent, sandboxed R, Python, and DuckDB SQL workbench exposed through MCP.
The planned public MCP surface has two tools:

- `console` evaluates complete R, Python, or SQL cells, supplies interactive input to an active evaluation, and polls for output.
- `console_session` manages session requirements and lifecycle operations.

R is the host runtime, Python is embedded through reticulate, and SQL initially uses DuckDB through R.
The worker backend remains an open design decision.
Do not couple the public MCP or sidecar interfaces to Ark, Jupyter, `harp`, or `libr` before the backend spike is complete.

See `design-sketches/README.md` for the product overview and `design-sketches/docs/ARCHITECTURE.md` for the proposed implementation sequence.

## Next implementation target

Milestone 0 is a backend-neutral MCP server with a deterministic fake runtime.
The first end-to-end slice should:

1. start an MCP server over stdio;
2. complete an MCP initialization handshake;
3. list the draft `console` and `console_session` tool schemas;
4. exercise that behavior through the compiled binary.

Keep the first slice independent of the unresolved R worker backend.

## Repository map

- `Cargo.toml` — Rust package metadata.
- `src/main.rs` — current binary entry point.
- `tests/cli.rs` — public binary acceptance tests.
- `.github/workflows/ci.yaml` — formatting, Clippy, and test checks.
- `design-sketches/` — tentative product and architecture documents.
- `README.md` — current user-facing project status.

Add modules only when implemented public behavior needs them.
Begin as one Cargo package and split crates only when a real boundary emerges.

## Working rules

- Keep PRs narrow and easy to review.
  Build the product in small, sure-footed layers, and do not bundle design changes with implementation unless the same behavior requires both.
- Add a public acceptance or regression test first and confirm that it fails before implementing behavior.
- Test through public interfaces.
  Do not add tests for private helpers.
- Keep complete code cells separate from interactive `stdin`.
- Keep the MCP adapter independent of interpreter implementation details.
- Treat all runtime execution as shell-class capability and place safety at the worker-process boundary.
- Update this file when a PR changes the implemented surface or repository map.
- Before opening a PR, run:

  ```bash
  cargo fmt --all --check
  cargo clippy --all-targets --all-features -- -D warnings
  cargo test --all-targets --all-features
  ```
