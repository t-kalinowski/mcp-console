"""Install validated extension names with the selected managed Python DuckDB."""

import json
import os
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

config = {"extension_directory": request["extension_directory"]}
if os.environ.get("CODEX_NETWORK_PROXY_ACTIVE") == "1":
    # Extension INSTALL does not consume the proxy environment itself.
    config["http_proxy"] = os.environ["HTTP_PROXY"]
connection = duckdb.connect(
    ":memory:",
    config=config,
)
connection.execute("SET enable_progress_bar = false")
try:
    builtin_extensions = {
        name
        for (name,) in connection.execute(
            "SELECT unnest(aliases || [extension_name]) FROM duckdb_extensions() "
            "WHERE install_mode = 'STATICALLY_LINKED'"
        ).fetchall()
    }
    for extension in extensions:
        if extension not in builtin_extensions:
            connection.install_extension(extension)
finally:
    connection.close()
