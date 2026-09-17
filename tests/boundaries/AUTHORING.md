# Authoring a boundary case

Start with `scripts/test --locate SELECTOR`, read the existing case and its contract, and add a public regression before changing implementation.
Use the [validation ladder](../../docs/DEVELOPMENT.md#validation-ladder) to keep iteration focused.

## Embedded programs

Use `support.normalization.code()` to dedent a readable program.
Keep `code(` and the opening string delimiter on the same line.
Place its formatting directive immediately above that line, including for nested calls.
Start the payload on the line after the opening quotes.
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

Run `scripts/format` first.
It attempts Ruff, Yamark, rustfmt, and Air before running the fixture checker, and reports each result.
Its default remains best effort; `scripts/format --strict` exits nonzero if any step fails or is missing, after attempting every step.
Review both its summary and the resulting diff.

Use `scripts/check-fixtures [PATH ...]` to check directives and direct `code()` layout without formatting; with no paths it scans Python files under `tests/`.
It reads string values and source positions from the containing Python file's AST and requires only Python's standard library.
Layout checking assumes the opening-line convention above; split `code(`/literal openings and directives inside the call are unsupported.
Embedded R and Python contents are opaque: invalid or incomplete programs need no special annotation.
Their public acceptance tests establish the intended behavior.

Each formatting directive selects the expression on the following line; put it immediately before the program argument when calling a helper.
Direct multiline literals assigned to `r`/`python`, including annotated assignments, or passed through those keyword arguments require the matching directive.
This includes direct `code()` calls containing those literals and values with escaped newlines.
Other embedded programs are identified by their directive; the checker does not infer a language from arbitrary string contents or follow variable assignments.

Add directives for transformed or assembled programs manually.
The checker does not inspect `code()` layout inside dynamic composition or infer unmarked dynamic expressions.
Review their layout directly, or assign the marked payload separately so the checker can inspect it before transformation.
Directive targets other than direct string literals or direct `code()` payloads require manual placement review; the checker does not determine whether arbitrary expressions or statements produce program text.

`scripts/check-core` also runs the checker and its command regressions.

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
