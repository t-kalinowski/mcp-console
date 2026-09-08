#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import matplotlib_test_environment
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_preserves_matplotlib_cache_across_activation_and_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            reticulate::py_require("matplotlib")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        persistent_caches = list(host_matplotlib.glob("fontlist-v*.json"))
        assert len(persistent_caches) == 1, persistent_caches
        persistent_cache_bytes = persistent_caches[0].read_bytes()
        client.send(
            python=code("""
            import os
            from pathlib import Path

            import matplotlib

            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            _ = invalid_cache.write_text(
                '{"__class__":"FontManager","_version":999}', encoding="utf-8"
            )
            """)
        )
        assert last_result_text(client) == "[done]"
        # Replacing the private link must not make a later runtime resolution
        # overwrite user-owned worker state or discard the worker.
        # fmt: python
        python = code("""
            private_cache = next(
                path
                for path in Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json")
                if path.is_symlink()
            )
            private_cache_bytes = private_cache.read_bytes()
            private_cache.unlink()
            private_cache.write_bytes(private_cache_bytes)
            cache_link_replaced = True
            """)
        client.send(python=python)
        assert last_result_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        client.send(python="(cache_link_replaced, __import__('yaml12').__name__)")
        assert last_result_text(client) == "(True, 'yaml12')\n"

        client.send(control="restart")
        assert last_result_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib
            import matplotlib.font_manager

            config = Path(matplotlib.matplotlib_fname())
            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                private_probe.read_text(encoding="utf-8") == "ok",
            )
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert output == "(True, 7.25, True)\n", repr(output)
        transcript = client.finish()
        assert (
            host_matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 7.25\n"
        )
        assert len(persistent_caches) == 1, persistent_caches
        assert persistent_caches[0].read_bytes() == persistent_cache_bytes
        assert not (persistent_caches[0].parent / "fontlist-v999.json").exists()
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
