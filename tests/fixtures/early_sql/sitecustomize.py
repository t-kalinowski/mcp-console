import duckdb
import os
import json
import __main__
import builtins

__main__.startup_sql_pid = os.getpid()
__main__.os = os
connect = duckdb.connect
observed = False


def observed_connect(*args, **kwargs):
    global observed
    behavior = os.environ["MCP_CONSOLE_TEST_SQL_BEHAVIOR"]
    if not observed:
        observed = True
        if behavior == "interrupt":
            input("SQL warmup> ")
        elif behavior == "error":
            raise RuntimeError("optional SQL warmup failed")
        elif behavior == "system-exit":
            raise SystemExit("optional SQL warmup failed")
    settings = json.loads(os.environ.get("MCP_CONSOLE_SQL_SETTINGS", "{}"))
    if settings.get("options", {}).get("threads") is not None:
        assert kwargs["config"]["threads"] == settings["options"]["threads"]
        assert args == (settings["database"],)
    connection = connect(*args, **kwargs)
    if settings.get("options", {}).get("threads") is not None:
        assert connection.execute("SELECT current_setting('threads')").fetchone() == (
            settings["options"]["threads"],
        )
    connection.execute("CREATE TABLE startup_catalog AS SELECT 42 AS answer")
    if behavior == "observe":
        with open(
            os.environ["MCP_CONSOLE_TEST_SQL_STARTED"], "wb", buffering=0
        ) as signal:
            signal.write(b"1")
        with open(
            os.environ["MCP_CONSOLE_TEST_SQL_RELEASE"], "rb", buffering=0
        ) as gate:
            assert gate.read(1) == b"1"
    return connection


duckdb.connect = observed_connect
if os.environ["MCP_CONSOLE_TEST_SQL_BEHAVIOR"] == "import-error":
    original_import = builtins.__import__

    def failed_import(name, *args, **kwargs):
        if name == "duckdb":
            builtins.__import__ = original_import
            raise ImportError("optional SQL warmup failed")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = failed_import

if os.environ["MCP_CONSOLE_TEST_SQL_BEHAVIOR"].startswith("metadata-"):
    original_import = builtins.__import__

    def failed_metadata_import(name, globals=None, *args, **kwargs):
        if (
            name == "importlib.metadata"
            and globals is not None
            and globals.get("__name__") == "_mcp_console_sql"
        ):
            builtins.__import__ = original_import
            if os.environ["MCP_CONSOLE_TEST_SQL_BEHAVIOR"] == "metadata-interrupt":
                input("SQL warmup> ")
            else:
                raise ImportError("optional SQL warmup failed")
        return original_import(name, globals, *args, **kwargs)

    builtins.__import__ = failed_metadata_import
