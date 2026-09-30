#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_exact_interleaving,
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment, reference_plots, startup_r_package
from support.native import build_interposer
from support.requirements import NATIVE_FIXTURES, requires
from support.records import Transcript
from support.resolvers import matplotlib_test_environment
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_evaluates_cells_in_persistent_reticulate_state(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
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
    output = last_result_text(client)
    assert output == "from Python\n(42, True, True, True)\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.send(python=python)
    assert last_result_text(client) == "2\n"
    client.send(python="answer")
    assert last_result_text(client) == "41\n"
    # fmt: r
    r = code(r"""
        stopifnot(!"package:reticulate" %in% search())
        py <- "user shadow"
        stopifnot(identical(py, "user shadow"))
        rm(py)
        py$answer
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.send(python=python)
    output = last_result_text(client)
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
    assert last_result_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    client.send(python="answer + 1")
    assert last_result_text(client) == "42\n"
    # fmt: python
    python = code("""
        import builtins as test_builtins

        test_original_import = test_builtins.__import__
        test_builtins.__import__ = None
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        test_builtins.__import__ = test_original_import
        answer + 1
        """)
    client.send(python=python)
    assert last_result_text(client) == "42\n"
    client.send(python="silent = True")
    assert last_result_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        rm(list = ls())
        py$assigned_from_r <- 43L
        py$answer
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        assigned_from_r
        """)
    client.send(python=python)
    assert last_result_text(client) == "43\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_returns_r_plots_from_python_bridge(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        bridge_plot <- function() {
          plot(1:3)
          invisible(NULL)
        }
        """)
    client.send(r=r)
    assert last_result_text(client) == "[done]"

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
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_returns_matplotlib_plots(binary: Path, execution: Execution) -> Transcript:
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

            reference = Path(os.environ["TMPDIR"]) / "matplotlib-reference.png"
            figure.savefig(reference, format="png")
            """)
        client.send(python=python)
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
        result["content"][0]["text"] = """before show
after show
<matplotlib figure displayhook representation>
"""
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
        assert last_result_text(client) == "[]\n"

        # fmt: python
        python = code("""
            axes.plot([1, 3], [2, 0])
            plt.get_fignums()
            """)
        client.send(python=python)
        assert last_result_text(client) == "[]\n"

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
        assert last_result_text(client) == "[]\n"

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
        assert 'File "<string>"' not in output, output
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
        assert last_result_text(client) == "[]\n"

        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_inherits_explicit_matplotlib_config(
    binary: Path, execution: Execution
) -> Transcript:
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
        client = McpClient(binary, execution.serve(), environment)
        client.initialize_and_list_tools()
        client.send(
            requirements={"python": ["matplotlib"]},
        )
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

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
        assert output == "(True, 8.25, True)\n", repr(output)
        transcript = client.finish()
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


@executions(DIRECT, SANDBOXED)
def test_inherits_default_matplotlib_config(
    binary: Path, execution: Execution
) -> Transcript:
    return inherits_matplotlib_config(binary, execution, xdg=False)


@executions(DIRECT, SANDBOXED)
def test_inherits_xdg_matplotlib_config(
    binary: Path, execution: Execution
) -> Transcript:
    return inherits_matplotlib_config(binary, execution, xdg=True)


def inherits_matplotlib_config(
    binary: Path, execution: Execution, *, xdg: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        home = temporary / "home"
        config_root = temporary / "xdg-config" if xdg else home / ".config"
        cache_root = temporary / "xdg-cache" if xdg else home / ".cache"
        matplotlib = (
            config_root / "matplotlib"
            if sys.platform == "linux"
            else home / ".matplotlib"
        )
        font_cache = (
            cache_root / "matplotlib" if sys.platform == "linux" else matplotlib
        )
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
        home.mkdir(exist_ok=True)
        environment["HOME"] = str(home)
        environment.pop("XDG_CONFIG_HOME", None)
        environment.pop("XDG_CACHE_HOME", None)
        if xdg:
            environment["XDG_CONFIG_HOME"] = str(config_root)
            environment["XDG_CACHE_HOME"] = str(cache_root)
        environment["TMPDIR"] = temporary_directory
        environment["R_LIBS_USER"] = os.pathsep.join(r_libraries)
        environment["RETICULATE_UV"] = uv
        environment["UV_CACHE_DIR"] = uv_cache
        environment["UV_PYTHON_INSTALL_DIR"] = uv_python
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPLCONFIGDIR", None)
        client = McpClient(binary, execution.serve(), environment)
        client.initialize_and_list_tools()
        client.send(
            requirements={"python": ["matplotlib"]},
        )
        assert last_result_text(client) == "[prepared]"
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
        output = last_result_text(client)
        assert output == "(True, 9.25)\n", repr(output)
        transcript = client.finish()
        assert matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 9.25\n"
        caches = list(font_cache.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


@executions(DIRECT, SANDBOXED)
def test_runs_async_python_explicitly(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    client.send(python="asyncio.run(answer())")
    assert last_result_text(client) == "42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_recovers_from_python_errors(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert client.transcript[-1]["result"]["isError"] is False, client.transcript[-1]
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert "in fail\n" in output
    assert 'File "<string>"' not in output
    assert output.endswith("ValueError: boom\n")
    # fmt: python
    python = code("""
        compile_partial = 9
        await missing()
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert not output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert 'File "<string>"' not in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.send(python='"compile_partial" in globals()')
    assert last_result_text(client) == "False\n"

    client.send(python="nul_state = 42\0")
    output = last_result_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert output == "SyntaxError: source code string cannot contain null bytes\n"
    client.send(python="1 / 0")
    output = last_result_text(client)
    assert 'File "<mcp-console:python:e5>", line 1, in <module>' in output
    assert 'File "<string>"' not in output
    assert output.endswith("ZeroDivisionError: division by zero\n")
    client.send(python="exec(\"raise RuntimeError('from exec')\")")
    output = last_result_text(client)
    assert 'File "<mcp-console:python:e6>", line 1, in <module>' in output
    assert 'File "<string>", line 1, in <module>' in output
    assert output.endswith("RuntimeError: from exec\n")
    # fmt: python
    python = code("""
        try:
            raise ValueError("cause")
        except ValueError as error:
            raise RuntimeError("chained") from error
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert 'File "<string>"' not in output
    assert "ValueError: cause\n" in output
    assert output.endswith("RuntimeError: chained\n")
    client.send(python="answer")
    assert last_result_text(client) == "41\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_releases_python_threads_before_running_init_hooks(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        started = FifoCheckpoint.create(root / "thread-started")
        release = FifoCheckpoint.create(root / "thread-release")
        completed = FifoCheckpoint.create(root / "thread-completed")
        # The hook creates a real Python thread. Its second checkpoint runs
        # after startup and a cell finish, while the worker is otherwise idle.
        python = code("""
            import os
            import threading

            def complete_after_release():
                with open(os.environ["THREAD_STARTED"], "wb", buffering=0) as signal:
                    signal.write(b"1")
                with open(os.environ["THREAD_RELEASE"], "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                with open(os.environ["THREAD_COMPLETED"], "wb", buffering=0) as signal:
                    signal.write(b"1")

            initialization_thread = threading.Thread(target=complete_after_release, daemon=True)
            initialization_thread.start()
            """)
        startup = code(f"""
            setHook("reticulate.onPyInit", function() {{
              reticulate::py_run_string({json.dumps(python)})
            }}, action = "append")
            """)
        try:
            with startup_r_package(root, startup) as environment:
                environment.update(
                    THREAD_STARTED=str(started.path),
                    THREAD_RELEASE=str(release.path),
                    THREAD_COMPLETED=str(completed.path),
                )
                args = (
                    execution.serve("--writable-root", str(root))
                    if execution == SANDBOXED
                    else execution.serve()
                )
                with McpClient(binary, args, environment, root) as client:
                    client.initialize_and_list_tools()
                    started.wait(
                        "Python thread started by initialization hook", timeout=60
                    )
                    client.send(python="assert initialization_thread.is_alive(); 42")
                    assert last_result_text(client) == "42\n"
                    release.release()
                    completed.wait("startup thread resumed while worker idle")
                    client.send(
                        python='initialization_thread.join(); hook_input = input("hook> "); hook_input',
                        stdin="after hook\n",
                    )
                    assert last_result_text(client) == (
                        "[input requested: \"hook> \"]\n'after hook'\n"
                    )
                    return client.finish()
        finally:
            for checkpoint in (started, release, completed):
                checkpoint.close()


@executions(DIRECT, SANDBOXED)
def test_routes_python_input(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.send(python=python)
    assert (
        last_result_text(client) == '[input requested: "name> "]\n[waiting for stdin]'
    )
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
    assert last_result_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.send(python=python, stdin="fd 0\n")
    assert last_result_text(client) == "'fd 0\\n'\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reads_unicode_nul_and_long_python_input(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code(r"""
            saved_object = object()
            original_object = saved_object
            first = input("unicode> ")
            second = input("long> ")
            third = input("queued> ")
            assert first == "Zażółć 🐍\0fin"
            assert second == "🐍" * 4097 + "\0tail"
            assert third == "queued"
            print(len(first), len(second), third)
            """)
        # Observe managed input after startup before timing input completion.
        client.send(python=python)
        assert last_result_text(client) == (
            '[input requested: "unicode> "]\n[waiting for stdin]'
        )
        expected = (
            '[input requested: "long> "]\n'
            '[input requested: "queued> "]\n'
            "12 4102 queued\n"
        )
        wait_for_evaluation_output(
            client,
            expected,
            "Unicode, NUL, long and queued Python input",
            stdin="Zażółć 🐍\0fin\n" + "🐍" * 4097 + "\0tail\nqueued\n",
        )
        # fmt: python
        python = code(r"""
            try:
                input("partial> ")
            except KeyboardInterrupt:
                print("input interrupted")
            """)
        client.send(python=python, stdin="🐍" * 1025 + "\0prefix")
        assert last_result_text(client) == (
            '[input requested: "partial> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "input interrupted\n",
            "partial Unicode input interruption",
            control="interrupt",
        )
        # fmt: python
        python = code(r"""
            replayed = input("replay> ")
            assert replayed == "🐍" * 1025 + "\0prefix!"
            print("complete input retained")
            """)
        wait_for_evaluation_output(
            client,
            '[input requested: "replay> "]\ncomplete input retained\n',
            "partial Unicode input replay",
            python=python,
            stdin="!\n",
        )
        client.send(python="saved_object is original_object")
        assert last_result_text(client) == "True\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_python_input_eof_retires_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Keep the cell alive after EOF to observe the input-completion notice.
        # fmt: r
        r = code(r"""
            eof_gate <- tempfile("python-input-eof-")
            cat(eof_gate)
            """)
        client.send(r=r)
        gate = FifoCheckpoint.create(Path(last_result_text(client)))
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<Python EOF checkpoint>"
        )
        try:
            # Exercise actual fd-0 EOF without closing the client's MCP transport.
            # fmt: python
            python = code(r"""
                import os

                gate_path = r.eof_gate
                eof_marker = object()
                input("ready for EOF> ")
                reader, writer = os.pipe()
                os.close(writer)
                os.dup2(reader, 0)
                os.close(reader)
                try:
                    input("EOF> ")
                except EOFError:
                    print("Python input reached EOF")
                with open(gate_path, "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                """)
            client.send(python=python)
            assert last_result_text(client) == (
                '[input requested: "ready for EOF> "]\n[waiting for stdin]'
            )
            wait_for_evaluation_output(
                client,
                '[input requested: "EOF> "]\n'
                "Python input reached EOF\n\n[running; poll with an empty send]",
                "Python EOF completes managed input before retirement",
                stdin="\n",
                timeout_ms=0,
            )
            gate.release()
            wait_for_evaluation_output(
                client,
                "[worker sideband read failed: worker sideband closed]\n"
                "[worker exited with status 0]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]",
                "Python input EOF retirement",
                expected_error=True,
            )
            client.send(python='"eof_marker" in globals()')
            assert last_result_text(client) == "False\n"
            return client.finish()
        finally:
            gate.close()


@executions(DIRECT, SANDBOXED)
def test_python_debugger_input(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.send(python=python, stdin="p debug_value\n")
    output = last_result_text(client)
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
    assert last_result_text(client) == "42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_restores_python_thread_state_after_startup_hook_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        probe = build_interposer(root, "python_exit_state")
        startup = code(f"""
            setHook("reticulate.onPyInit", function() {{
              invisible(dyn.load({json.dumps(str(probe))}))
              stop("synthetic Python initialization hook failure")
            }}, action = "append")
            """)
        with startup_r_package(root, startup) as environment:
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                result = client.send(
                    python="raise AssertionError('failed startup ran cell')"
                )
                assert result["isError"], result
                output = last_result_text(client)
                retirement = (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 1]\n"
                    "[worker stopped: in-memory state lost]"
                )
                diagnostic = (
                    "Error in fun() : synthetic Python initialization hook failure\n"
                )
                stderr = "Python bridge failed during R evaluation\nPython exit thread attached\n"
                assert output.endswith(retirement), output
                assert_exact_interleaving(
                    output.removesuffix(retirement), diagnostic, stderr
                )
                result["content"][0]["text"] = diagnostic + stderr + retirement
                (root / "startup.R").write_text("startup_recovered <- TRUE\n")
                client.send(
                    control="restart", python="assert bool(r.startup_recovered); 42"
                )
                assert last_result_text(client).endswith("42\n[done]"), (
                    client.transcript[-1]
                )
                return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
