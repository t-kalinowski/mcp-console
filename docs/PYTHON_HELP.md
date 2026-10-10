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

## Official Python manuals

`_console.python_docs()` looks up a prepared plain-text manual on the **worker host**:

```python
docs = _console.python_docs()
print(docs)
```

It returns `None` if no cache exists for the running interpreter's major/minor.
Lookup never downloads, writes, starts a browser/server, or prevents ordinary Python use offline.
A corrupt or unreadable cache raises an ordinary Python exception.
The tool hint is available before runtime startup; lookup happens inside an admitted Python cell.
Collect a running cell before submitting documentation code.

When available, the result includes a `pathlib.Path` in `directory`, the active `python_version` and `python_executable`, a short `indexes` list, and the preparation receipt.
Read the short section indexes before choosing a page:

```python
docs = _console.python_docs()
assert docs is not None, "Ask the host administrator to prepare this Python manual."
manual = docs["directory"]
print((manual / "library/index.txt").read_text(encoding="utf-8"))
```

The starting pages are `contents.txt`, `library/index.txt`, `tutorial/index.txt`, and `reference/index.txt`.
Use ordinary file reading and searching; the existing response and raw-log limits apply:

```python
# Find standard-library pages mentioning json.loads.
for page in (manual / "library").rglob("*.txt"):
    if "json.loads" in page.read_text(encoding="utf-8"):
        print(page.relative_to(manual))
```

### Prepare on the host

Run this explicit administration command in a trusted host shell, outside evaluated cells:

```sh
mcp-console prepare-python-docs --python /path/to/the/workers/python
```

Use the executable reported by `import sys; print(sys.executable)` in that worker.
The command runs that selected interpreter with isolated Python startup and downloads the official plain-text ZIP linked from its major/minor [download page](https://docs.python.org/3.13/download.html).
It does not use project configuration, managed package resolution, or change the resolver's network policy.
Executable selection, the host environment and cache location are trusted administration inputs.
Console never invokes this command from cells or startup; a sandboxed invocation is refused.
Network failure leaves no published partial cache; ordinary cells and `help(object)` remain usable.

By default the cache is `python-docs/` under the worker's `MCP_CONSOLE_HOME`, or `~/.agents/console/` when that variable is absent.
Set **an absolute** `MCP_CONSOLE_PYTHON_DOCS` directory in both administration and worker launch environments to select another location.
Each major/minor occupies its own directory.
A valid existing cache is reused without networking; changing the selected interpreter or restarting chooses the matching worker-local cache, never another minor as a fallback.
To refresh a cached minor, the host administrator removes that minor's directory and prepares it again while workers are stopped.
For remote or container deployments, prepare on that worker host with its interpreter and environment; a controller-local path need not exist there.

An offline administrator can import a downloaded ZIP and its saved official download page:

```sh
mcp-console prepare-python-docs --python /path/to/python \
  --archive /path/to/python-docs-text.zip --download-page /path/to/download.html
```

The saved page must identify the selected minor's official plain-text ZIP and documentation release.
The receipt distinguishes local inputs from a live download; these files are trusted inputs, not an authenticity proof.
It records source/final URLs, archive and download-page SHA-256 hashes, compressed/expanded sizes, entry count, and `download_page_release`.
That release is the **download page's label at preparation**, not a promise that the archive matches the interpreter's exact historical patch.
Official minor-series archives may be updated.
The exact downloaded ZIP is identified by its SHA-256; original copyright and license files are retained.

Preparation bounds the archive at 32 MiB, expanded content at 128 MiB, each member at 8 MiB, and the archive at 4,096 entries.
It rejects traversal, absolute/backslash or case aliases, duplicate members, links and unsupported ZIP entries before publishing a complete cache atomically.

### Read-only boundary

The default native sandbox permits persistent host reads and denies writes outside granted temporary/workspace roots.
Keep documentation storage outside those writable roots and any explicit write grants.
The native permissions enforce that boundary; file modes alone do not.
With `--no-sandbox`, an explicit writable cache grant, or another policy permitting writes, the cache is **not guaranteed read-only** to evaluated code.
Host filesystem permissions and custom native policies can deny reads; the cache must be readable to the worker account.
Windows installations outside standard roots can need [trusted host read-access setup](WINDOWS.md#validation).
Lookup reports a worker-local path; it neither grants access nor changes policy.

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
