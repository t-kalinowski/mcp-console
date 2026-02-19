# Future Work: Optional REPL Description Extras and Skill Draft

## Current direction (kept)

The default MCP tool descriptions should stay concise and Codex-aligned:
- clear purpose
- strict argument schema
- short operational constraints

This repo now includes backend-specialized `repl` descriptions (`R` and `Python`) with only high-value affordances that Codex does not assume by default (pager, images, help, debugger).

## Problem

Even with backend specialization, there is additional knowledge that can improve agent performance but is too long for default tool descriptions:
- workflow philosophy (iterate quickly, inspect, fail fast)
- high-signal debugging recipes
- backend-specific gotchas and anti-patterns
- deep pager/search/navigation tactics

## Proposal

Add an opt-in "description extras" layer that can be surfaced separately from the default tool description.

Candidate delivery options:
- a dedicated skill document referenced by higher-level instructions
- a configurable "extended description" toggle
- backend-specific supplemental docs loaded only when needed

Design goals:
- keep default tool descriptions short
- keep extras explicit and structured
- make extras backend-aware and runtime-selected

## Draft Knowledge Set (candidate skill content)

### Shared REPL operating model

- Prefer short execution/inspection loops over speculative reasoning.
- Persist state intentionally; reset when large state is no longer useful.
- Use non-blocking calls and polling patterns when a request is expected to run long.
- Treat errors as signal; do not silently fallback to unrelated flows.

### Shared pager playbook

- When pager is active, backend input is blocked.
- Use empty input for next page.
- Use `:q` to exit pager.
- Use `:/pattern` and `:n` for forward-only search.
- Use `:a` for all remaining output when full context is needed.

### Shared image playbook

- Expect plot/image content as first-class outputs.
- Re-run plot commands after state changes to verify deltas.
- Keep image generation deterministic when possible (fixed seeds, fixed dimensions).

### R-specific draft

- Primary docs/help paths: `?topic`, `help()`, `help.search()`, `vignette()`, `RShowDoc()`.
- Debugging: `browser()`, `debug()`, `debugonce()`, `trace()`, and call-stack inspection.
- Data-workflow pattern: inspect with `str()`, verify assumptions, then transform.
- Plot controls: `options(console.plot.width, console.plot.height, console.plot.units, console.plot.dpi)`.
- Prefer explicit preconditions with `stopifnot()` in public APIs.

### Python-specific draft

- Primary docs/help paths: `help()`, `dir()`, `pydoc.help`.
- Debugging: `breakpoint()`, `pdb.set_trace()`, step/inspect/continue loops.
- Inspection-first workflow for dynamic objects before refactoring logic.
- Plot workflow for matplotlib and related libraries with explicit redraw checks.
- Prefer small explicit assertions over broad fallback trees.

### Reset/interrupt patterns

- Use interrupt (`\u0003`) for best-effort cancellation of active execution.
- Use reset (`repl_reset` or `\u0004` prefix behavior where applicable) when state is inconsistent or too large.
- After reset, rerun only the minimal setup needed for the next objective.

## Open implementation questions

- Where should extras be configured (CLI flag, env var, config file, or per-client profile)?
- Should extras be attached to `repl` tool description directly or injected as separate instructions?
- How should backend-specific extras evolve as new backends are added (for example Julia)?
