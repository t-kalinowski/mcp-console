# Working on MCP Console

Read the [development guide](docs/DEVELOPMENT.md) for setup and validation, and the [architecture](docs/ARCHITECTURE.md) for ownership boundaries.
Use the [documentation index](docs/README.md) to find a specific contract; do not load every document for every task.
Source and public acceptance tests settle disagreements with prose.
`design-sketches/` is exploratory, not a specification of current behavior.

## Change discipline

- Keep a behavior-changing PR to one observable change.
  Aim for fewer than 500 added/deleted implementation lines, but prefer a coherent change to an artificial split.
  Mechanical moves, tests, snapshots, and docs are exempt.
- Start a behavior change with a failing public regression test.
  Verify an internal refactor against existing public tests; do not test private helpers.
- Preserve generation ownership and confirmed-retirement barriers.
  Submitted code is shell-class capability; dependency preparation is a separate trusted host operation, not protected by the worker sandbox.
- Console-owned internal protocols evolve in lockstep and are not independently versioned.
  Update both endpoints and fixtures together; preserve external protocol versions, the separately pinned companion contract, and component/artifact identity checks.
- Prefer event-driven waits with explicit cancellation over polling.
  Add abstractions for implemented responsibilities, not planned features.
  Reassess large files rather than enforcing a line limit; retain one Cargo package until a concrete crate boundary emerges.
- Keep production R/Python programs under `src/` compile-time embedded.
  Do not load them from the checkout or installation layout at runtime.
- Update the owning document when a contract changes.
  Explain decisions and invariants, not line-by-line mechanics or exhaustive file inventories.

## Validation

Run from the repository root:

```sh
scripts/preflight
scripts/test BOUNDARY/SUITE::CASE
scripts/format
scripts/check
```

Run `scripts/format` unchanged before committing and review its output: by default it reports failures without failing the whole command.
Use `scripts/check` as the ordinary final gate; `--full` is for explicitly requested or change-appropriate exhaustive validation.
Report commands actually run and any unavailable coverage.
CI is the comprehensive merge gate.

Windows x64 supports experimental local sandboxed and `serve --no-sandbox` sessions with R/Python and host dependency resolution through `ir`/`uv`.
Windows SQL is deferred.
Follow [Windows validation](docs/WINDOWS.md#validation); shared workflow commands select native Windows checks.
Run native build and validation commands exclusively in the checkout; Windows packaging holds a blocking native checkout lock outside `target`.

Use temporary workspaces and `MCP_CONSOLE_HOME` for tests, preserving `HOME` and the caller's tool environment.
Declare platform/capability requirements in test support rather than reconstructing provider defaults.
See the [boundary test guide](tests/boundaries/README.md) for fixtures and snapshots.

Never hand-edit `tests/snapshots/`.
Update them through `scripts/test --update` or formatting.
Use one shared transcript across platforms by default.
Make fixtures deterministic, normalize incidental paths and presentation only after assertions, and split capability-specific coverage into separate cases with declared requirements.
Reserve platform snapshots for the exact platform-specific behavior under test; document that behavior in the decorator's `reason`.
A different handshake, dependency default, newline, or executable suffix alone does not justify a variant of an otherwise portable case.
Preserve complete errors and tracebacks.
Client/server transcripts should generally include initialization and tool discovery before ordinary calls.
Generally keep one `mcp-console` invocation per YAML transcript.
Include initialization once near the top, normally via a matching canonical `!same-as` reference; use separate transcript files for additional invocations.
Use the existing exact canonical-handshake comparison for `!same-as` references; preserve different or incomplete exchanges in full.
Preserve errors and tracebacks; normalize incidental values, not behavior.
Fix the producer or serializer when regeneration is wrong.

Prefer readable multiline programs.
In Python tests, put `# fmt: r` or `# fmt: python` immediately above `code(` and its opening delimiter; indent the payload and closing delimiter one level deeper.
Review formatting and affected platform snapshots.
Group related command arguments and diagnostic fields.

## Local work and publishing

For resumed work, verify `.dev-workflow/task.md`, when present, against Git status; it is a checkpoint, not authority.
Use `scripts/with-checkout` for direct Cargo/Maturin commands (`scripts/with-checkout.cmd` on Windows).
Do not bypass checkout ownership locks or reuse a busy worktree.
See [development](docs/DEVELOPMENT.md) for the validation ladder and [release](RELEASE.md) for companion staging and packaging.

Before merging, require passing CI and a verified thumbs-up reaction from the GPT connector reviewer for the **current PR head**.
A completed review without findings is not approval.
Follow `RELEASE.md` before pushing a release tag; never yank or remove published PyPI files.
