#!/usr/bin/env -S uv run --script

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    McpClient,
    Transcript,
    assert_result_content,
    code,
    r_test_environment,
    reference_plots,
    run_this_suite,
    stop_client,
    wait_for_evaluation_output,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}

from client_server._harness import (
    assert_exact_interleaving,
    _python_last_tool_text as last_tool_text,
    matplotlib_test_environment,
)


def test_evaluates_cells_in_persistent_reticulate_state(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        from_r <- 40L
        python_source_visible <- function() {
          calls <- vapply(sys.calls(), deparse1, character(1))
          marker <- paste0("unique_python_", "source_marker")
          any(grepl(marker, calls, fixed = TRUE))
        }
        reticulate::py_run_string(
          r"---(
        test_sys = __import__("sys")
        test_types = __import__("types")
        __import__ = None
        exec = None
        setattr = None
        _io = "user io"
        _main = "user main"
        _sys = "user sys"
        sorted = "user sorted"
        test_sys.modules["matplotlib.pyplot"] = test_types.SimpleNamespace(
            get_fignums=lambda: [],
            close=lambda *_args, **_kwargs: None,
        )
        )---"
        )
        """)
    client.send(r=r)
    # fmt: python
    python = code("""
        answer = r.from_r + 1
        print("from Python")
        (
            answer + 1,
            (__import__, exec, setattr) == (None, None, None),
            (_io, _main, _sys, sorted) == ("user io", "user main", "user sys", "user sorted"),
            "_mcp_console" not in globals()
            and test_sys.modules["_mcp_console"].__name__ == "_mcp_console",
        )
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "from Python\n(42, True, True, True)\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.send(python=python)
    assert last_tool_text(client) == "2\n"
    client.send(python="answer")
    assert last_tool_text(client) == "41\n"
    # fmt: r
    r = code(r"""
        stopifnot(!"package:reticulate" %in% search())
        py <- "user shadow"
        stopifnot(identical(py, "user shadow"))
        rm(py)
        py$answer
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "False\n", repr(output)
    # fmt: r
    r = code(r"""
        .mcp_console_private <- "user value"
        .mcp_console_python_source <- "user source"
        .mcp_console_python_filename <- "user filename"
        is.null <- function(...) FALSE
        """)
    client.send(r=r)
    client.send(python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        import builtins as test_builtins

        test_original_import = test_builtins.__import__
        test_builtins.__import__ = None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        test_builtins.__import__ = test_original_import
        answer + 1
        """)
    client.send(python=python)
    assert last_tool_text(client) == "42\n"
    client.send(python="silent = True")
    assert last_tool_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        rm(list = ls())
        py$assigned_from_r <- 43L
        py$answer
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        assigned_from_r
        """)
    client.send(python=python)
    assert last_tool_text(client) == "43\n"
    return client._finish()


def test_returns_r_plots_from_python_bridge(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        bridge_plot <- function() {
          plot(1:3)
          invisible(NULL)
        }
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    expected_plot = reference_plots(
        rscript,
        environment,
        r + "bridge_plot()\n",
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    # fmt: python
    python = code("""
        print("before plot")
        r.bridge_plot()
        print("after plot")
        """)
    client.send(python=python)
    assert_result_content(
        client,
        ["before plot\nafter plot\n", expected_plot[0]],
    )
    return client._finish()


def test_returns_matplotlib_plots(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace-one"
        workspace.mkdir()
        system_fonts = sorted(
            path
            for path in Path("/System/Library/Fonts").iterdir()
            if path.is_file() and path.suffix.lower() in {".otf", ".ttc", ".ttf"}
        )
        assert system_fonts, "test system font is required"
        system_font = system_fonts[0]
        profiler_output = temporary / "system-profiler.plist"
        profiler_output.write_bytes(
            plistlib.dumps([{"_items": [{"path": str(system_font)}]}])
        )
        path = os.environ.get("PATH")
        assert path is not None, "PATH is required"
        probe = temporary / "bin" / "system_profiler"
        probe.parent.mkdir()
        probe.write_text(
            code(r"""
                #!/bin/sh
                set -eu
                test "$#" -eq 2
                test "$1" = "-xml"
                test "$2" = "SPFontsDataType"
                : > "$TMPDIR/mcp-console-font-discovery"
                /bin/cat "$MCP_CONSOLE_TEST_SYSTEM_PROFILER_OUTPUT"
                """),
            encoding="utf-8",
        )
        probe.chmod(0o755)
        fontconfig = temporary / "fonts.conf"
        fontconfig.write_text(
            code(r"""
                <?xml version="1.0"?>
                <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
                <fontconfig>
                  <cachedir prefix="xdg">mcp-console-test</cachedir>
                </fontconfig>
                """),
            encoding="utf-8",
        )
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["FONTCONFIG_FILE"] = str(fontconfig)
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment["MCP_CONSOLE_TEST_SYSTEM_PROFILER_OUTPUT"] = str(profiler_output)
        environment["PATH"] = os.pathsep.join((str(probe.parent), path))
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPL_IGNORE_SYSTEM_FONTS", None)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            reticulate::py_require("matplotlib")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        host_discovery = temporary / "mcp-console-font-discovery"
        assert host_discovery.is_file()
        persistent_caches = list(host_matplotlib.glob("fontlist-v*.json"))
        assert len(persistent_caches) == 1, persistent_caches
        persistent_cache_bytes = persistent_caches[0].read_bytes()
        host_discovery.unlink()

        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib
            import matplotlib.pyplot as plt

            assert (
                Path(matplotlib.matplotlib_fname()).resolve()
                == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve()
            )
            assert matplotlib.rcParams["lines.linewidth"] == 7.25

            later_figure, later_axes = plt.subplots(num=20)
            later_axes.plot([1, 2, 3], [1, 2, 1])
            later_reference = Path(os.environ["TMPDIR"]) / "matplotlib-later-reference.png"
            later_figure.savefig(later_reference, format="png")

            figure, axes = plt.subplots(num=10)
            axes.plot([1, 2, 3], [3, 1, 2])
            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            invalid_cache.write_text(
                '{"__class__":"FontManager","_version":999}',
                encoding="utf-8",
            )

            reference = Path(os.environ["TMPDIR"]) / "matplotlib-reference.png"
            figure.savefig(reference, format="png")
            """)
        client.send(python=python)
        assert not list(temporary.rglob("mcp-console-font-discovery"))
        reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-reference.png",
            client,
        )
        later_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-later-reference.png",
            client,
        )
        assert_result_content(
            client,
            [reference.read_bytes(), later_reference.read_bytes()],
            image_reference="live matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            shown_figure, shown_axes = plt.subplots()
            shown_axes.plot([1, 2, 3], [1, 3, 2])
            shown_reference = Path(os.environ["TMPDIR"]) / "matplotlib-shown-reference.png"
            shown_figure.savefig(shown_reference, format="png")
            print("before show")
            plt.show()
            print("after show")
            shown_figure
            """)
        client.send(python=python)
        shown_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-shown-reference.png",
            client,
        )
        result = client.transcript[-1]["result"]
        output = result["content"][0]["text"]
        assert output.startswith("before show\nafter show\n<Figure size "), output
        assert output.endswith(" with 1 Axes>\n"), output
        result["content"][0]["text"] = (
            "before show\nafter show\n<matplotlib figure displayhook representation>\n"
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], shown_reference.read_bytes()],
            image_reference="live shown matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            closed_figure, closed_axes = plt.subplots()
            closed_axes.plot([1, 2, 3], [2, 1, 3])
            closed_reference = Path(os.environ["TMPDIR"]) / "matplotlib-closed-reference.png"
            closed_figure.savefig(closed_reference, format="png")
            plt.close(closed_figure)
            plt.get_fignums()
            """)
        client.send(python=python)
        closed_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-closed-reference.png",
            client,
        )
        assert closed_reference.is_file()
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            axes.plot([1, 3], [2, 0])
            plt.get_fignums()
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            error_figure, error_axes = plt.subplots()
            error_axes.plot([1, 2], [2, 1])
            error_reference = Path(os.environ["TMPDIR"]) / "matplotlib-error-reference.png"
            error_figure.savefig(error_reference, format="png")
            raise ValueError("cell failed")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("ValueError: cell failed\n"), output
        error_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-error-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], error_reference.read_bytes()],
            image_reference="live error-cell matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            def fail_plot_capture(*args, **kwargs):
                raise RuntimeError("plot render failed")


            failed_figure = plt.figure()
            failed_figure.savefig = fail_plot_capture
            figure, axes = plt.subplots()
            axes.plot([1, 3], [2, 0])
            second_reference = Path(os.environ["TMPDIR"]) / "matplotlib-second-reference.png"
            figure.savefig(second_reference, format="png")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("RuntimeError: plot render failed\n"), output
        second_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-second-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], second_reference.read_bytes()],
            image_reference="live second matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"

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
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        client.send(python="(cache_link_replaced, __import__('yaml12').__name__)")
        assert last_tool_text(client) == "(True, 'yaml12')\n"

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            marker = Path(os.environ["TMPDIR"]) / "mcp-console-font-discovery"
            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            invalid_cache_was_seeded = invalid_cache.exists()

            import matplotlib
            import matplotlib.font_manager

            config = Path(matplotlib.matplotlib_fname())
            font_cache = next(Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json"))
            try:
                with font_cache.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                font_cache_read_only = True
            else:
                font_cache_read_only = False

            try:
                with config.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                config_read_only = True
            else:
                config_read_only = False

            try:
                config.with_name("worker-payload").write_text("payload", encoding="utf-8")
            except PermissionError:
                config_directory_read_only = True
            else:
                config_directory_read_only = False

            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                font_cache_read_only,
                config_read_only,
                config_directory_read_only,
                private_probe.read_text(encoding="utf-8") == "ok",
                marker.exists(),
                invalid_cache_was_seeded,
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 7.25, True, True, True, True, False, False)\n", repr(
            output
        )
        assert not list(temporary.rglob("mcp-console-font-discovery"))
        transcript = client._finish()
        assert (
            host_matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 7.25\n"
        )
        assert not (host_matplotlib / "worker-payload").exists()
        assert len(persistent_caches) == 1, persistent_caches
        assert persistent_caches[0].read_bytes() == persistent_cache_bytes
        assert not (persistent_caches[0].parent / "fontlist-v999.json").exists()
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_inherits_explicit_matplotlib_config(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        explicit = temporary / "explicit"
        explicit.mkdir()
        explicit_rc = explicit / "matplotlibrc"
        explicit_rc.write_text("lines.linewidth: 8.25\n", encoding="utf-8")
        inherited = temporary / "inherited"
        inherited.mkdir()
        (inherited / "matplotlibrc").write_text(
            "lines.linewidth: 18.25\n",
            encoding="utf-8",
        )
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(inherited)
        environment["MATPLOTLIBRC"] = str(explicit_rc)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(explicit_rc)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.send(
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            config = Path(matplotlib.matplotlib_fname())
            try:
                with config.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                config_read_only = True
            else:
                config_read_only = False

            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                config_read_only,
                private_probe.read_text(encoding="utf-8") == "ok",
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 8.25, True, True)\n", repr(output)
        transcript = client._finish()
        assert explicit_rc.read_text(encoding="utf-8") == "lines.linewidth: 8.25\n"
        assert not list(explicit.glob("fontlist-v*.json"))
        caches = list(inherited.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_inherits_default_matplotlib_config(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        home = temporary / "home"
        matplotlib = home / ".matplotlib"
        matplotlib.mkdir(parents=True)
        matplotlibrc = matplotlib / "matplotlibrc"
        matplotlibrc.write_text("lines.linewidth: 9.25\n", encoding="utf-8")
        r_environment, rscript = r_test_environment()
        # fmt: r
        source = code(r"""
            writeLines(.libPaths())
            """)
        r_libraries = subprocess.run(
            [rscript, "--vanilla", "-e", source],
            check=True,
            capture_output=True,
            text=True,
            env=r_environment,
        ).stdout.splitlines()
        uv = shutil.which("uv")
        assert uv is not None, "real uv is required for managed-Python tests"
        uv_cache = subprocess.run(
            [uv, "cache", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        uv_python = subprocess.run(
            [uv, "python", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["HOME"] = str(home)
        environment["TMPDIR"] = temporary_directory
        environment["R_LIBS_USER"] = os.pathsep.join(r_libraries)
        environment["RETICULATE_UV"] = uv
        environment["UV_CACHE_DIR"] = uv_cache
        environment["UV_PYTHON_INSTALL_DIR"] = uv_python
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPLCONFIGDIR", None)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.send(
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            (
                Path(matplotlib.matplotlib_fname()).resolve()
                == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 9.25)\n", repr(output)
        transcript = client._finish()
        assert matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 9.25\n"
        caches = list(matplotlib.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_runs_async_python_explicitly(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(python="asyncio.run(answer())")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_recovers_from_python_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert "in fail\n" in output
    assert output.endswith("ValueError: boom\n")
    # fmt: python
    python = code("""
        compile_partial = 9
        await missing()
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.send(python='"compile_partial" in globals()')
    assert last_tool_text(client) == "False\n"

    client.send(python="nul_state = 42\0")
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert "SyntaxError" in output
    assert "null bytes" in output
    client.send(python="answer")
    assert last_tool_text(client) == "41\n"
    return client._finish()


def test_releases_python_threads_before_running_init_hooks(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        release: Path | None = None
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(loadNamespace("reticulate"))
                hook_failed <- FALSE
                setHook(
                  "reticulate.onPyInit",
                  function() {
                    if (!hook_failed) {
                      hook_failed <<- TRUE
                      stop("synthetic Python initialization hook failure")
                    }
                  },
                  action = "prepend"
                )
                message <- tryCatch(
                  {
                    invisible(reticulate::py_config())
                    "Python initialization unexpectedly succeeded"
                  },
                  error = conditionMessage
                )
                cat(message, "\n", sep = "")
                """)
            client.send(r=r)
            output = last_tool_text(client)
            assert output == "synthetic Python initialization hook failure\n", repr(
                output
            )

            # fmt: python
            python = code("""
                import os
                import threading
                import time
                from pathlib import Path

                directory = Path(os.environ["TMPDIR"])
                started = directory / "python-init-hook-thread-started"
                release = directory / "release-python-init-hook-thread"
                completed = directory / "python-init-hook-thread-completed"


                def complete_after_release():
                    started.touch()
                    while not release.exists():
                        time.sleep(0.01)
                    completed.touch()


                initialization_thread = threading.Thread(
                    target=complete_after_release,
                    daemon=True,
                )
                initialization_thread.start()
                while not started.exists():
                    time.sleep(0.01)
                """)
            client.send(python=python)
            assert last_tool_text(client) == "[done]"

            started = wait_for_worker_file(
                Path(temporary_directory),
                "python-init-hook-thread-started",
                client,
            )
            release = started.parent / "release-python-init-hook-thread"
            release.touch()
            wait_for_worker_file(
                Path(temporary_directory),
                "python-init-hook-thread-completed",
                client,
            )

            client.send(
                python='hook_input = input("hook> "); hook_input',
                stdin="after hook\n",
            )
            assert last_tool_text(client) == (
                "[input requested: \"hook> \"]\n'after hook'\n"
            )
            return client._finish()
        finally:
            if release is not None and release.parent.exists():
                release.touch(exist_ok=True)
            stop_client(client)


def test_routes_python_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.send(python=python)
    assert last_tool_text(client) == '[input requested: "name> "]\n[waiting for stdin]'
    wait_for_evaluation_output(
        client,
        "'Ada'\n",
        "Python stdin routing",
        stdin="Ada\n",
        timeout_ms=0,
    )

    # fmt: python
    python = code("""
        color = input("color> ")
        color
        """)
    client.send(python=python, stdin="blue\n")
    assert last_tool_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.send(python=python, stdin="fd 0\n")
    assert last_tool_text(client) == "'fd 0\\n'\n"
    return client._finish()


def test_python_debugger_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.send(python=python, stdin="p debug_value\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 2, output
    assert output.endswith('41\n[input requested: "(Pdb) "]\n[waiting for stdin]'), (
        output
    )

    wait_for_evaluation_output(
        client,
        "[done]",
        "Python debugger input",
        stdin="continue\n",
        timeout_ms=3_000,
    )
    client.send(python="debug_value")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_restarts_after_python_bridge_failure(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python_worker_marker <- TRUE
        Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
        invisible(suppressMessages(base::trace(
          "py_discover_config",
          tracer = quote(base::signalCondition(base::structure(
            base::list(message = "synthetic interrupt", call = NULL),
            class = c("interrupt", "condition")
          ))),
          print = FALSE,
          where = asNamespace("reticulate")
        )))
        """)
    client.send(r=r)
    client.send(python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    bridge_failure = "Python bridge failed during R evaluation\n"
    python_failure = (
        "Error in py_discover_config(required_module, use_environment) : \n"
        "  Python specified in RETICULATE_PYTHON "
        "(/mcp-console-missing-python) does not exist\n"
    )
    worker_failure = (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 1]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )
    output = result["content"][0]["text"]
    assert output.endswith(worker_failure), output
    assert_exact_interleaving(
        output.removesuffix(worker_failure),
        bridge_failure,
        python_failure,
    )
    result["content"][0]["text"] = bridge_failure + python_failure + worker_failure
    client.send(r='exists("python_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    client.send(python="6 * 7")
    assert last_tool_text(client) == "42\n"
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
