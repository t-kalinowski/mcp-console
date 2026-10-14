# Installed R manual workflows

These YAML recordings show two complete client sessions against the HTML manuals installed with the running R: investigate supported C API entry points, and explain default versus supplied argument evaluation after consulting the function reference.
Each records the actual R version and full Console responses, with temporary cache paths normalized.
The C API session follows a bounded chapter continuation and searches its complete Markdown file with `readLines()` and `grep()`.
The language session uses only Console's R interface.
The same workflow must produce identical output through direct and sandboxed execution on one host.

Regenerate them after building the checkout:

```sh
scripts/with-checkout uv run --script tests/manual_workflows.py --output examples/r-manuals
```

Use an output directory under `/tmp` when validating another installed R version.
These are sample recordings rather than golden regressions because installed documentation changes between R releases.
The deterministic manual regressions remain in `tests/boundaries/client_server/r/test_manuals.py`, using small synthetic HTML fixtures.
No R manuals are copied into the repository.
