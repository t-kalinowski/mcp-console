"""Prepare extensions with the selected Python provider's DuckDB build."""

import json
import sys

import duckdb

requirements = json.load(sys.stdin)
with duckdb.connect() as connection:
    for extension in requirements["extensions"]:
        installed = connection.execute(
            "SELECT installed FROM duckdb_extensions() WHERE extension_name = ?",
            [extension],
        ).fetchone()
        if installed is None or not installed[0]:
            connection.install_extension(extension)
