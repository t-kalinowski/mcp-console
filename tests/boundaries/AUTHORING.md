# Authoring a boundary case

Start with `scripts/test --locate SELECTOR`, read the existing case and its contract, and add a public regression before changing implementation.
Use the [validation ladder](../../docs/DEVELOPMENT.md#validation-ladder) to keep iteration focused.

## Embedded programs

Use `support.normalization.code()` to dedent a readable program.
Place its formatting directive immediately before the assignment or `code()` argument.
Indent the payload and closing delimiter four spaces beyond the line containing `code(`, preserving the program's own indentation:

```python
client.send(
    # fmt: python
    python=code(r"""
        for value in (1, 2):
            print(value)
        """),
)
```

Prefer a raw literal when backslashes belong to the submitted program.
A non-raw `"\n"` inside a Python string literal becomes an actual newline before the worker receives it.
Use `# fmt: r` for R programs.
Do not label a custom worker's command language as Python or R merely because it travels in that tool field.

Run `scripts/check-fixtures [PATH ...]` to check marked multiline programs; with no paths it scans Python files under `tests/`.
It compiles constant Python programs and parses R with `Rscript --vanilla`, without evaluating either, and checks direct `code()` payload indentation.
When `R_HOME` is set, the parser is `$R_HOME/bin/Rscript`; otherwise it uses `Rscript` on `PATH`.
Each formatting directive selects the expression on the following line; put it immediately before the program argument when calling a helper.
Multiline literals assigned to `r`/`python`, including annotated assignments, or passed through those keyword arguments require the matching directive.
Other embedded programs are identified by their directive; the checker does not infer a language from arbitrary string contents or follow variable assignments.
Direct `r`/`python` literals are checked as written; `code()` supplies dedenting when used.
Other marked helper payloads use the recipe's dedented syntax convention; their public case verifies the helper's actual normalization.
Multiline values are checked even when authored on one line with escaped newlines.
Single-line expressions split into adjacent literals remain single-line programs.

Interpolated strings, transformations such as `.replace()`, and assembled expressions are counted separately because their completed source depends on runtime values.
The checker does not compile strings nested inside interpolation expressions or transformation arguments as separate programs.
Fragments appended through `+=` are not inferred as complete programs; they may only become valid after assembly.
Their public acceptance case must exercise the assembled program.
For a test that deliberately submits invalid syntax, document the reason immediately above the formatting directive:

```python
# syntax: skip deliberately tests incomplete input
# fmt: r
r = code(r"""
    answer <- (
    """)
```

The skip exempts syntax parsing only; directive and indentation checks still apply.
Use the complete `# syntax: skip` marker followed by whitespace and a reason.
`scripts/check-core` runs the checker and its command regressions.
`scripts/format` attempts Ruff, Yamark, rustfmt, Air, then the fixture checker and reports each result.
Its default remains best effort; `scripts/format --strict` exits nonzero if any step fails or is missing, after attempting every step.
Review both its summary and the resulting diff: an embedded formatter can leave invalid input unchanged while returning success.

## Execution modes

Use `@executions(DIRECT, SANDBOXED)` and `execution.serve(...)` for ordinary cases that share a transcript across modes.
The fixture supplies `--no-sandbox` for direct execution; do not add it yourself.
A case that needs a writable sandbox root uses `SANDBOXED.serve("--writable-root", str(root))` and the appropriate sandbox capability.
Do not combine a direct fixture with sandbox-only arguments.
See [requirements and execution modes](README.md#requirements-and-execution-modes) for complete examples.

## Lifecycle receipts and checkpoints

Choose the observable receipt that establishes the state the assertion needs, then release the fixture's checkpoint.
`FifoCheckpoint` in `tests/support/checkpoints.py` supplies blocking arrival and release gates.
`read_lines` in `tests/support/capture.py` waits for a complete stream receipt without mixing descriptor readiness with buffered text reads.
`Events` in `tests/support/events.py` observes supported process transitions.
Reuse these helpers and keep cleanup in `finally` blocks.

For a worker program held at a FIFO, establish public evaluation admission before allowing it to proceed:

```python
client.send(python=program, timeout_ms=0)
assert last_tool_text(client) == "\n[running; poll with an empty send]"
started.wait("worker reached the release gate")
release.release()
```

A fixture marker alone does not prove the server admitted evaluation.
Likewise, process startup does not prove a cell log has closed.
When testing output written during shutdown, tie the fixture's release to completion of the relevant cell and verify its recorded `cell_output` event before asserting which file owns the later output.
Keep complete output assertions; sleeps, broader matching, or a longer timeout do not establish that ordering.
The Python [lifecycle cases](client_server/python/test_lifecycle.py) show admission before interrupt/restart, and the [retention cases](client_server/output/test_spools.py) show a completion checkpoint followed by retained-file and journal assertions.
