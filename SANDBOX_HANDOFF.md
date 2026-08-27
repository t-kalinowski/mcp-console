# Bundled Codex sandbox runner handoff

This branch and draft pull request preserve the first-slice sandbox-boundary work for another agent. No validation result is claimed, and the pull request must not be merged in its current handoff-only state.

## Starting revisions

- MCP Console main: `50d2f5c613f382b1381447b64505e269262ff2a9`
- Codex sandbox patch branch: `mcp-console/sandbox-api/2161ec27`
- Codex sandbox API v2 commit: `855e8f9ef5b4211408791252e02e38b4757218cc`
- Intended Codex runner branch: `mcp-console/sandbox-runner-v1-validated`

The actual heads were verified and still matched those revisions when the work began.

## Intended design already selected

- Add a focused private `mcp-console-sandbox` executable to the Codex workspace. Do not add another mode to the full Codex CLI.
- Use a strict, typed JSON launch specification with `protocol_version: 1` over a private inherited descriptor separate from file descriptors 0, 1, and 2.
- Keep the target program and arguments as native trailing OS arguments and use the runner's inherited environment as the target environment.
- Remove private runner variables and close the specification descriptor before target launch.
- Use a direct-exec topology for this first slice so the sandboxed relay remains MCP Console's direct child, process-group leader, and session leader.
- Keep relay stdin and stdout as the existing native pipes and relay stderr as inherited diagnostics. Do not proxy or change the relay or worker protocols.
- MCP Console owns normalized application policy and helper discovery. Codex owns native sandbox enforcement and the filesystem-policy-to-Seatbelt translation.
- Preserve host-read-only filesystem access, one MCP Console private writable temporary directory, denied direct networking, inherited standard streams, PTYs created inside the sandbox, terminal isolation, and supervised process-tree lifetime.
- Build the Codex helper separately using the Codex toolchain and lockfile. Bundle it privately in the MCP Console wheel without exposing another uv tool command. MCP Console must not link Codex Rust crates.
- Keep R, Python, and DuckDB dependency resolution outside the sandbox.

## Recovered work

The prior implementation attempt was serialized into raw recovery fragments on the `codex-sandbox-runner` branch under `.validation/`:

- `codex.patch.gz.b64.00` through `.02`
- `mcp.patch.gz.b64.00` through `.04`

Those fragments are retained on GitHub as recovery evidence. The transport is malformed and cannot be assumed to reconstruct by simple concatenation and base64 decoding. A later agent should inspect the fragments and branch history, but should be prepared to reapply the implementation from the design and repository context instead of repairing the transport.

The partial recovered MCP Console patch showed changes in these areas before corruption:

- `.github/workflows/ci.yaml`
- `.github/workflows/release.yml`
- `AGENTS.md`
- `README.md`
- `RELEASE.md`
- `codex-sandbox-revision.txt`
- `docs/ARCHITECTURE.md`
- `scripts/check`
- `scripts/prepare-sandbox-runner`
- `scripts/release.py`
- `scripts/test`
- `src/sandbox.rs`
- removal of `src/sandbox/macos.rs`

The intended implementation also included Codex-side runner source, Cargo and Bazel metadata, sandbox API documentation, and rolling-patch maintenance notes.

## Publication blocker

The connected GitHub App installation currently includes `t-kalinowski/mcp-console` but not `t-kalinowski/codex`. Direct branch, blob, and file writes to the Codex fork return `403 Resource not accessible by integration`. A one-shot cross-repository Actions publisher was also attempted, but no repository secret with write access to the Codex fork was available.

As a result, the intended Codex runner branch has not been created. Add `t-kalinowski/codex` to the same GitHub App installation, or provide a fine-grained token with Contents write access to that repository, before continuing.

## Remaining work

1. Create `t-kalinowski/codex:mcp-console/sandbox-runner-v1-validated` from `855e8f9ef5b4211408791252e02e38b4757218cc`.
2. Recover or reapply the focused runner implementation and commit it on that branch.
3. Record the exact resulting Codex commit in MCP Console.
4. Recover or reapply the MCP Console provider, packaging, tests, and documentation changes on this branch.
5. Replace this handoff-only diff with the real implementation diff and update the draft PR body.
6. Run the focused Codex checks and the requested MCP Console formatting, complete checks, golden-diff check, dependency-tree check, and installed-wheel smoke tests.
7. Inspect attributable CI failures, leave the PR open for review, and do not merge it.
