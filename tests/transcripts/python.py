#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from _support import (
    McpClient,
    Transcript,
    assert_result_content,
    code,
    r_test_environment,
    reference_plots,
    run_this_suite,
)


PLATFORMS = {"darwin"}


def test_preserves_configured_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == '[1] "configured-by-user"\n'
    return client.finish()


def test_preserves_empty_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == '[1] ""\n'
    return client.finish()


def managed_python_transcript(binary: Path, configured: bool) -> Transcript:
    environment = os.environ.copy()
    if configured:
        environment["RETICULATE_PYTHON"] = "managed"
    else:
        environment.pop("RETICULATE_PYTHON", None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for managed-Python tests"
    environment.pop("RETICULATE_UV", None)
    environment["UV_OFFLINE"] = "1"

    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        config <- reticulate::py_config()
        history <- reticulate::py_require()$history
        stopifnot(
          identical(python, "managed"),
          file.exists(config$python),
          isTRUE(config$ephemeral),
          !any(vapply(
            history,
            function(request) identical(request$requested_from, "base"),
            logical(1L)
          ))
        )
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code(r"""
        40 + 2
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output == "42\n", repr(output)
    return client.finish()


def test_evaluates_with_default_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=False)


def test_evaluates_with_explicit_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=True)


def normalize_resolution_error(error: str, invalid: str | None = None) -> str:
    error, python_patch = re.subn(
        r'(?m)^(  "python": "\d+\.\d+)\.\d+( \(reticulate default\)",)$',
        r"\1.x\2",
        error,
        count=1,
    )
    assert python_patch == 1, error
    if invalid is not None:
        error, uv_indentation = re.subn(
            rf"(?m)^(?P<indent> *)({re.escape(invalid)})\n(?P=indent)(?P<caret> +\^)$",
            lambda match: f"{match.group(2)}\n{match.group('caret')}",
            error,
        )
        assert uv_indentation == 1, error
    return "\n".join(line.rstrip() for line in error.splitlines())


def test_prepares_initial_python_requirements(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "/mcp-console-prepare-must-replace-python"
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    invalid = "not a valid requirement !!!"

    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    resolution_error = result["content"][0]["text"]
    recorded_error = normalize_resolution_error(resolution_error, invalid)
    result["content"][0]["text"] = recorded_error
    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    result["content"][0]["text"] = normalize_resolution_error(
        result["content"][0]["text"], invalid
    )
    assert result["content"][0]["text"] == recorded_error
    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": ["numpy\npandas"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirement strings must not contain NUL or line breaks"
    )
    # fmt: r
    r = code(r"""
        seed <- tail(reticulate::py_require()$history, 1L)[[1L]]
        stopifnot(
          identical(seed$requested_from, "mcp-console"),
          identical(seed$action, "set"),
          identical(seed$packages, c("numpy", "py-yaml12"))
        )
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    return client.finish()


def test_restart_loses_state_and_retains_python_requirements(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.call_tool("send", python="restart_marker = 42")
    assert last_tool_text(client) == "[done]"

    client.call_tool("session", action="restart")
    assert last_tool_text(client) == "[restarted]"

    # fmt: python
    python = code("""
        import yaml12

        "restart_marker" in globals(), yaml12.__name__
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client.finish()


def test_requires_restart_for_late_python_requirements(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    client.call_tool("send", python="sentinel = 42")
    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "restart required"
    client.call_tool("send", python="sentinel")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def test_layers_python_requirements_declared_by_r_packages(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[1] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [rscript.with_name("R"), "CMD", "INSTALL", "--library", library, fixture],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import importlib.util
            import sys

            runtime_marker = 42
            initial_prefix = sys.prefix
            importlib.util.find_spec("yaml12") is None
            """)
        client.call_tool("send", python=python)
        assert last_tool_text(client) == "True\n"

        # fmt: r
        r = code(r"""
            initial_libpython <- reticulate::py_config()$libpython
            initial_worker <- Sys.getpid()
            """)
        client.call_tool("send", r=r)
        assert last_tool_text(client) == "[done]"

        client.call_tool("send", r="library(mcpconsolepyrequire)")
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            identical(reticulate::py_config()$libpython, initial_libpython) &&
              identical(Sys.getpid(), initial_worker)
            """)
        client.call_tool("send", r=r)
        assert last_tool_text(client) == "[1] TRUE\n"

        # fmt: python
        python = code("""
            import yaml12

            (runtime_marker, yaml12.__name__, sys.prefix != initial_prefix)
            """)
        client.call_tool("send", python=python)
        output = last_tool_text(client)
        assert output == "(42, 'yaml12', True)\n", repr(output)

        client.call_tool("session", action="restart")
        assert last_tool_text(client) == "[restarted]"

        # fmt: python
        python = code("""
            import yaml12

            ("runtime_marker" in globals(), yaml12.__name__)
            """)
        client.call_tool("send", python=python)
        assert last_tool_text(client) == "(False, 'yaml12')\n"

        client.call_tool(
            "session",
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "[prepared]"
        return client.finish()


def test_resolves_package_requirements_before_python_initializes(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[1] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [rscript.with_name("R"), "CMD", "INSTALL", "--library", library, fixture],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            library(mcpconsolepyrequire)
            request <- tail(reticulate::py_require()$history, 1L)[[1L]]
            stopifnot(
              identical(request$requested_from, "mcpconsolepyrequire"),
              isTRUE(request$env_is_package)
            )
            """)
        client.call_tool("send", r=r)
        assert last_tool_text(client) == "[done]"

        # Replace the worker after the requirement declaration has completed,
        # but before Python has initialized.
        # fmt: r
        r = code(r"""
            tools::pskill(Sys.getpid(), signal = 9L)
            """).removesuffix("\n")
        client.call_tool("send", r=r)
        assert client.transcript[-1]["result"]["isError"] is True

        # fmt: r
        r = code(r"""
            "py-yaml12" %in% reticulate::py_require()$packages
            """)
        client.call_tool("send", r=r)
        output = last_tool_text(client)
        assert output == ("[1] TRUE\n[worker restarted: in-memory state lost]\n"), repr(
            output
        )

        # fmt: python
        python = code("""
            import yaml12

            yaml12.__name__
            """)
        client.call_tool("send", python=python)
        assert last_tool_text(client) == "'yaml12'\n"
        return client.finish()


def test_does_not_checkpoint_python_requirements_from_failed_cell(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(reticulate::py_config())
        invisible(reticulate::py_require("py-yaml12"))
        stopifnot(reticulate::py_module_available("yaml12"))
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.call_tool("send", r=r)
    assert client.transcript[-1]["result"]["isError"] is True

    # fmt: r
    r = code(r"""
        "py-yaml12" %in% reticulate::py_require()$packages
        """)
    client.call_tool("send", r=r)
    output = last_tool_text(client)
    assert output == ("[1] FALSE\n[worker restarted: in-memory state lost]\n"), repr(
        output
    )

    client.call_tool(
        "session",
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    output = last_tool_text(client)
    assert output == "restart required", repr(output)
    return client.finish()


def test_reports_restart_required_while_python_is_running(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            runtime_generation_marker = "original runtime retained"

            import time
            from pathlib import Path

            temporary = Path(__import__("os").environ["TMPDIR"])
            (temporary / "python-evaluation-running").touch()
            while not (temporary / "release-python").exists():
                time.sleep(0.01)
            """)
        client.call_tool("send", python=python, timeout_ms=0)
        assert last_tool_text(client) == "\n[running]"
        running = wait_for_worker_file(
            Path(temporary_directory),
            "python-evaluation-running",
            client,
        )
        release = running.parent / "release-python"

        session_returned = threading.Event()
        forced_release = threading.Event()

        def release_blocked_evaluation() -> None:
            if not session_returned.wait(2):
                forced_release.set()
                release.touch()

        watchdog = threading.Thread(target=release_blocked_evaluation)
        watchdog.start()
        client.call_tool(
            "session",
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        session_returned.set()
        watchdog.join()
        assert not forced_release.is_set(), "session waited for the running evaluation"
        assert last_tool_text(client) == "restart required"

        release.touch()
        client.call_tool("send")
        assert last_tool_text(client) == "[done]"
        client.call_tool("send", python="runtime_generation_marker")
        assert last_tool_text(client) == "'original runtime retained'\n"
        return client.finish()


def test_does_not_parse_requirements_as_rscript_options(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        marker = Path(temporary_directory) / "host-r-code-ran"
        expression = (
            "base::writeLines('executed', base::Sys.getenv('MCP_CONSOLE_HOST_MARKER'))"
        )
        environment = os.environ.copy()
        environment["RETICULATE_PYTHON"] = "/mcp-console-prepare-must-replace-python"
        environment["MCP_CONSOLE_HOST_MARKER"] = str(marker)
        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        client.call_tool(
            "session",
            action="prepare",
            requirements={"python": ["-e", expression]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert not marker.exists(), "requirement executed as unsandboxed R code"
        assert "managed Python resolution failed" in result["content"][0]["text"]
        result["content"][0]["text"] = normalize_resolution_error(
            result["content"][0]["text"]
        )
        return client.finish()


def test_forces_uv_offline_in_builtin_worker(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["UV_OFFLINE"] = "0"
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("UV_OFFLINE", unset = NA_character_)
        """)
    client.call_tool("send", r=r)
    assert last_tool_text(client) == '[1] "1"\n'
    return client.finish()


def test_evaluates_cells_in_persistent_reticulate_state(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        from_r <- 40L
        python_source_visible <- function() {
          calls <- vapply(sys.calls(), deparse1, character(1))
          marker <- paste0("unique_python_", "source_marker")
          any(grepl(marker, calls, fixed = TRUE))
        }
        """)
    client.call_tool("send", r=r)
    # fmt: python
    python = code("""
        answer = r.from_r + 1
        print("from Python")
        answer + 1
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output == "from Python\n42\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "2\n"
    client.call_tool("send", python="answer")
    assert last_tool_text(client) == "41\n"
    client.call_tool("send", r="reticulate::py$answer")
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output == "False\n", repr(output)
    # fmt: r
    r = code(r"""
        .mcp_console_private <- "user value"
        .mcp_console_python_source <- "user source"
        .mcp_console_python_filename <- "user filename"
        is.null <- function(...) FALSE
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "[done]"
    client.call_tool("send", python="answer + 1")
    assert last_tool_text(client) == "42\n"
    client.call_tool("send", python="silent = True")
    assert last_tool_text(client) == "[done]"
    return client.finish()


def test_returns_r_plots_from_python_bridge(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        bridge_plot <- function() {
          plot(1:3)
          invisible(NULL)
        }
        """)
    client.call_tool("send", r=r)
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
    client.call_tool("send", python=python)
    assert_result_content(
        client,
        ["before plot\nafter plot\n", expected_plot[0]],
    )
    return client.finish()


def test_returns_matplotlib_plots(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
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
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["FONTCONFIG_FILE"] = str(fontconfig)
        environment["MPLCONFIGDIR"] = str(temporary / "host-matplotlib")
        environment["XDG_CACHE_HOME"] = str(temporary / "host-cache")
        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        client.call_tool(
            "session",
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"

        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib.pyplot as plt

            figure, axes = plt.subplots()
            axes.plot([1, 2, 3], [3, 1, 2])
            reference = Path(os.environ["TMPDIR"]) / "matplotlib-reference.png"
            figure.savefig(reference, format="png")
            """)
        client.call_tool("send", python=python)
        reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-reference.png",
            client,
        )
        assert reference.is_file()
        assert last_tool_text(client) == "[done]"

        # fmt: python
        python = code("""
            shown_figure, shown_axes = plt.subplots()
            shown_axes.plot([1, 2, 3], [1, 3, 2])
            shown_reference = Path(os.environ["TMPDIR"]) / "matplotlib-shown-reference.png"
            shown_figure.savefig(shown_reference, format="png")
            print("before show")
            plt.show()
            print("after show")
            """)
        client.call_tool("send", python=python)
        shown_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-shown-reference.png",
            client,
        )
        assert_result_content(
            client,
            ["before show\n", shown_reference.read_bytes(), "after show\n"],
            image_reference="live shown matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            displayed_figure, displayed_axes = plt.subplots()
            displayed_axes.plot([1, 2, 3], [2, 3, 1])
            displayed_reference = Path(os.environ["TMPDIR"]) / "matplotlib-displayed-reference.png"
            displayed_figure.savefig(displayed_reference, format="png")
            displayed_figure
            """)
        client.call_tool("send", python=python)
        displayed_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-displayed-reference.png",
            client,
        )
        assert_result_content(
            client,
            [displayed_reference.read_bytes()],
            image_reference="live displayed matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            container_figure, container_axes = plt.subplots()
            container = container_axes.errorbar([1, 2], [2, 1], yerr=[0.2, 0.3], fmt="none")
            container_reference = Path(os.environ["TMPDIR"]) / "matplotlib-container-reference.png"
            container_figure.savefig(container_reference, format="png")
            container
            """)
        client.call_tool("send", python=python)
        container_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-container-reference.png",
            client,
        )
        assert_result_content(
            client,
            [container_reference.read_bytes()],
            image_reference="live container matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            root_figure = plt.figure()
            subfigure = root_figure.subfigures(1, 1)
            subfigure_axes = subfigure.subplots()
            subfigure_axes.plot([1, 2], [1, 2])
            subfigure_reference = Path(os.environ["TMPDIR"]) / "matplotlib-subfigure-reference.png"
            root_figure.savefig(subfigure_reference, format="png")
            subfigure_axes
            """)
        client.call_tool("send", python=python)
        subfigure_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-subfigure-reference.png",
            client,
        )
        assert_result_content(
            client,
            [subfigure_reference.read_bytes()],
            image_reference="live subfigure matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            axes.plot([1, 3], [2, 0])
            figure
            """)
        client.call_tool("send", python=python)
        output = last_tool_text(client)
        assert output.startswith("<Figure size "), output
        assert output.endswith(" with 1 Axes>\n"), output
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<retained closed matplotlib figure>\n"
        )

        # fmt: python
        python = code("""
            error_figure, error_axes = plt.subplots()
            error_axes.plot([1, 2], [2, 1])
            error_reference = Path(os.environ["TMPDIR"]) / "matplotlib-error-reference.png"
            error_figure.savefig(error_reference, format="png")
            raise ValueError("cell failed")
            """)
        client.call_tool("send", python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("ValueError: cell failed\n"), output
        result["content"][0]["text"] = (
            "<python traceback ending in ValueError: cell failed>\n"
        )
        error_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-error-reference.png",
            client,
        )
        assert error_reference.is_file()
        assert_result_content(client, [result["content"][0]["text"]])

        client.call_tool("send", python="plt.get_fignums()")
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
            plt.show()
            """)
        client.call_tool("send", python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("RuntimeError: plot render failed\n"), output
        result["content"][0]["text"] = (
            "<matplotlib render traceback ending in RuntimeError: plot render failed>\n"
        )
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

        client.call_tool("send", python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"
        return client.finish()


def test_runs_async_python_explicitly(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "[done]"
    client.call_tool("send", python="asyncio.run(answer())")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def test_recovers_from_python_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.call_tool("send", python=python)
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
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.call_tool("send", python='"compile_partial" in globals()')
    assert last_tool_text(client) == "False\n"

    client.call_tool("send", python="nul_state = 42\0")
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert "SyntaxError" in output
    assert "null bytes" in output
    client.call_tool("send", python="answer")
    assert last_tool_text(client) == "41\n"
    return client.finish()


def test_routes_python_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == '[input requested: "name> "]\n[stdin needed]'
    client.call_tool("send", stdin="Ada\n")
    assert last_tool_text(client) == "'Ada'\n"

    # fmt: python
    python = code("""
        color = input("color> ")
        color
        """)
    client.call_tool("send", python=python, stdin="blue\n")
    assert last_tool_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.call_tool("send", python=python, stdin="fd 0\n")
    assert last_tool_text(client) == "'fd 0\\n'\n"
    return client.finish()


def test_python_debugger_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 1, output
    assert output.endswith("\n[stdin needed]"), output

    client.call_tool("send", stdin="p debug_value\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 1, output
    assert "41\n" in output, output
    assert output.endswith("\n[stdin needed]"), output

    client.call_tool("send", stdin="continue\n")
    assert last_tool_text(client) == "[done]"
    client.call_tool("send", python="debug_value")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def test_restarts_after_python_bridge_failure(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python_worker_marker <- TRUE
        Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "Python bridge failed during R evaluation\n"
        "Error in py_discover_config(required_module, use_environment) : \n"
        "  Python specified in RETICULATE_PYTHON "
        "(/mcp-console-missing-python) does not exist\n"
        "[worker sideband read failed: worker sideband closed]"
    )
    client.call_tool("send", r='exists("python_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == (
        "[1] FALSE\n[worker restarted: in-memory state lost]\n"
    )
    client.call_tool("send", python="6 * 7")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


def wait_for_worker_file(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 10
    while True:
        paths = list(root.glob(f"**/{name}"))
        if paths:
            assert len(paths) == 1, paths
            return paths[0]
        assert client.process.poll() is None, (
            "mcp-console stopped before worker checkpoint"
        )
        assert time.monotonic() < deadline, f"worker did not create {name}"
        time.sleep(0.01)


if __name__ == "__main__":
    run_this_suite(__file__)
