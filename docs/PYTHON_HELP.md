# Python documentation in a cell

Use `help(object)` in an ordinary `send(python=...)` cell to read installed Python documentation:

```python
help(str.upper)
```

Console returns plain text without a pager, browser, or server:

```text
Help on method_descriptor:

upper(self, /) unbound builtins.str method
    Return a copy of the string converted to uppercase.
```

Pass an existing function, class, method, or module.
For example, `import math; help(math.sqrt)` documents a function and `help(math)` documents the module.
Missing targets raise ordinary Python errors, such as `NameError` for an undefined variable or `AttributeError` for a missing member; correct the target in a subsequent cell.

Bare `help()` opens an interactive prompt instead.
If a cell is waiting at `help> `, answer with `send(stdin="quit\n")` to return to ordinary execution.

## Long documentation

Try `help(str)` for class documentation, or select a narrower member such as `help(str.isupper)`.
Console applies its existing [8 KiB response limit](API.md#output-limits), retaining the beginning and latest tail.
An omission notice names the raw log containing the full emitted help, subject to the [recording limits](RECORDING.md#retrieve-omitted-output).
Read that path with the client's filesystem tools, or in a subsequent Python cell:

```python
# Replace this path with the raw-log path from the omission notice.
with open("path from the omission notice", encoding="utf-8") as log:
    documentation = log.read()
```

The text is then available for focused searching or excerpts.
Polling again does not retrieve the omitted middle.
The response limit bounds returned text, not the time or memory spent generating help.

## Optional version and provenance

Help reflects the active environment's docstrings and signatures; it may not include a package's external manuals.
When reporting documentation, identify the Python version and the selected module's origin if useful:

```python
import math
import platform

print(platform.python_version())
print(math.__spec__.origin)
```

For a third-party package, `from importlib.metadata import version; version("scikit-learn")` reports its installed distribution version.
The corresponding import name is `sklearn`; distribution and import names can differ, and missing metadata raises `PackageNotFoundError`.
A local source tree may differ from its recorded version.

Imports and dynamic inspection can execute Python code, including custom attribute access.
Passing a string to `help`, such as `help("some_package.function")`, can import modules during name lookup; see Python's [pydoc reference](https://docs.python.org/3/library/pydoc.html).
Use the ordinary worker permissions and [cell admission rules](SEND_OPERATIONS.md#cells-and-polling): collect any running cell before submitting help, and interrupt inspection if needed.

Use the [documentation index](README.md) as the shared discovery point for installed-language help through existing cells.
