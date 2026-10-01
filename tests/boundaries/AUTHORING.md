# Authoring a boundary case

Locate a related case with `scripts/test --locate SELECTOR`, read its contract,
and add a public regression before changing behavior. Follow the
[validation ladder](../../docs/DEVELOPMENT.md#validation-ladder).

## Embedded programs

Use short strings for short fixtures and dedented multiline literals for longer
programs. Put `# fmt: python` or `# fmt: r` immediately above `code(` and its
opening delimiter, including in nested calls. Indent the payload and closing
delimiter four spaces beyond that line:

```python
client.send(
    # fmt: python
    python=code(r"""
        for value in (1, 2):
            print(value)
        """),
)
```

Use raw literals when backslashes belong to the submitted program. Preserve its
indentation and trailing newlines. Do not label a custom worker's command language
as R/Python merely because it travels in that field. Run `scripts/format` and
inspect every formatter's result and the diff; `--strict` fails after attempting
all formatters when any is missing or fails.

## Execution modes

For portable cases, use `@executions(DIRECT, SANDBOXED)` and `execution.serve()`;
the fixture chooses sandboxing. Put sandbox-only arguments in explicit sandbox
fixtures, for example `SANDBOXED.serve("--writable-root", str(root))`, with the
appropriate capability. See [modes and requirements](README.md#requirements-and-execution-modes).

## Lifecycle receipts and checkpoints

Establish the observable state an assertion needs before releasing a fixture.
Use `FifoCheckpoint` (`support.checkpoints`), `read_lines` (`support.capture`),
and `Events` (`support.events`) for blocking gates, complete stream receipts,
and process transitions. Keep cleanup in context managers or `finally` blocks.

```python
client.send(python=program, timeout_ms=0)
assert last_tool_text(client) == "\n[running; poll with an empty send]"
started.wait("worker reached the release gate")
release.release()
```

A fixture marker alone does not prove public evaluation admission. Process
startup does not prove a cell log has closed. For shutdown output, establish
cell completion and its recorded `cell_output` event before asserting which
file owns later bytes. Sleeps, broader matching, and longer timeouts do not
establish ordering.

See the [Python lifecycle cases](client_server/python/test_lifecycle.py) and
[retention cases](client_server/output/test_spools.py). Preserve complete output
assertions and use the [snapshot rules](README.md#snapshots) for normalization.
