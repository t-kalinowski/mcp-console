"""Install validated extension names with the selected managed Python DuckDB."""

import json
import re
import sys

request = json.load(sys.stdin)
extensions = request["extensions"]
assert isinstance(extensions, list) and extensions
assert all(
    isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
    for name in extensions
)

try:
    import duckdb
except ImportError as error:
    raise SystemExit(
        "DuckDB extension preparation needs an importable duckdb package in the "
        "candidate Python environment; include duckdb in requirements.python"
    ) from error

connection = duckdb.connect(
    ":memory:",
    config={
        "extension_directory": request["extension_directory"],
    },
)
connection.execute("SET enable_progress_bar = false")
try:
    for extension in extensions:
        connection.install_extension(extension)
finally:
    connection.close()
