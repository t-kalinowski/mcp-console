# Python documentation in a cell

Read documentation for an installed Python object in an ordinary `send(python=...)` cell.
The result reflects the active Python environment, including its package versions and local docstrings.

## Inspect an object or module

Submit this Python code, choosing a module and an object from that module:

```python
import math
import platform
import pydoc

module = math
target = math.sqrt
print(f"Python {platform.python_version()}")
print(f"Module {module.__name__}: {module.__spec__.origin}")
encoded = pydoc.render_doc(target, renderer=pydoc.plaintext).encode("utf-8")
preview = encoded[:4096].decode("utf-8", errors="ignore")
print(preview, end="")
omitted = len(encoded) - len(preview.encode("utf-8"))
if omitted:
    print(f"\n[documentation preview; {omitted} UTF-8 bytes omitted]")
```

For example, Python 3.12.15 returns:

```text
Python 3.12.15
Module math: built-in
Python Library Documentation: built-in function sqrt in module math

sqrt(x, /)
    Return the square root of x.
```

Set `target = math` to inspect the module, or select another existing function, class, or method.
Pass the object itself to `render_doc`.
String arguments such as `"some_package.function"` perform name lookup that can import modules.

`render_doc` returns text without opening a pager, browser, or HTTP server.
The `plaintext` renderer avoids terminal overstrike formatting.
Use this recipe instead of interactive `help()`, pager-based help, or command-line pydoc modes.
Python's [pydoc reference](https://docs.python.org/3/library/pydoc.html) describes the import and pager behavior.

## Identify the installed version

The header reports the running Python version and the selected module's import origin.
An origin can be a source or extension path, `built-in`, `frozen`, or `None` for a namespace package.
For an object defined elsewhere, select its defining module explicitly.

For a third-party package, query its distribution metadata by distribution name.
For example, if scikit-learn is installed:

```python
from importlib.metadata import version

print(version("scikit-learn"))
```

The import name is `sklearn`; import and distribution names can differ.
An absent distribution raises `PackageNotFoundError`.
A local module may have no distribution metadata, and a modified source tree may differ from its recorded package version.
Keep the module origin with the version when reporting what was inspected.

## Missing targets and long documentation

Resolve the target with ordinary Python object access.
For example, `target = math.missing_documentation_target` raises:

```text
AttributeError: module 'math' has no attribute 'missing_documentation_target'
```

An undefined variable raises `NameError`.
These are ordinary cell errors; correct the target in a subsequent cell.

The recipe prints at most 4,096 UTF-8 bytes of documentation, ending at a complete character, and reports the remaining byte count.
For a long example, use `import builtins`, `module = builtins`, and `target = str`.
Inspect a narrower member, such as `str.split`, when the preview omits the information you need.
The version header, origin, omission notice, and any other output also count toward Console's [8 KiB response budget](API.md#output-limits).

This bounds the documentation preview, not rendering time or memory: `render_doc` builds the complete text before the recipe shortens it.
Docstrings and signatures may be incomplete, and generated help does not include every installed package's external manuals.

## Execution and discovery

Imports and dynamic inspection can execute Python code, including custom attribute access.
Run them in the worker through the usual cell permissions and admission rules.
Help cells have the same execution authority as other submitted code.
If another cell is running, [collect its output before submitting the help cell](SEND_OPERATIONS.md#cells-and-polling).
Use ordinary interruption if inspection does not finish.

Use the [documentation index](README.md) as the shared discovery point for language-specific installed-version recipes, executed through existing `send` cells.
This Python recipe requires no additional MCP operation or resource index.
