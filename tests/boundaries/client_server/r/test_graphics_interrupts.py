#!/usr/bin/env -S uv run --script

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.processes import (
    ProcessIdentity,
    capture_process_identity,
    current_process_identity,
)
from support.progress import without_elapsed
from support.r import r_test_environment, reference_plots
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite


@contextmanager
def finalizing_plot(
    binary: Path, execution: Execution
) -> Iterator[tuple[McpClient, FifoCheckpoint, ProcessIdentity, Path, list[bytes]]]:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as fixtures:
        directory = Path(temporary)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary
        plots = reference_plots(
            rscript,
            environment,
            "plot(1:3); plot(3:1)",
            width=2,
            height=2,
            dpi=96,
            pages=2,
        )
        source = Path(__file__).resolve().parents[3] / "fixtures/r_graphics_gate.c"
        shutil.copyfile(source, directory / source.name)
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "SHLIB",
                "-o",
                "mcp_test_graphics_gate.so",
                source.name,
            ],
            cwd=directory,
            env=environment,
            check=True,
            capture_output=True,
        )
        started = fixtures.enter_context(
            closing(FifoCheckpoint.create(directory / "close-started"))
        )
        release = fixtures.enter_context(
            closing(FifoCheckpoint.create(directory / "close-release"))
        )
        arguments = (
            execution.serve("--writable-root", temporary)
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(
            binary,
            arguments,
            environment,
            current_directory=directory,
        ) as client:
            client.initialize_and_list_tools()
            # The public R tracer gates automatic cell-end device closure.
            # This specifies recovery ordering, not native rendering latency.
            client.expect(
                'Tracing function "dev.off" in package "namespace:grDevices"\n',
                # fmt: r
                r=code(r"""
                    options(
                      console.plot.width_in = 2,
                      console.plot.height_in = 2,
                      console.plot.dpi = 96
                    )
                    plot_state <- 42L
                    cat(Sys.getpid(), file = "worker-pid")
                    cat(tempdir(), file = "worker-tempdir")
                    graphics_gate_dll <- dyn.load("./mcp_test_graphics_gate.so")
                    graphics_gate <- getNativeSymbolInfo(
                      "mcp_test_wait_graphics_gate",
                      PACKAGE = graphics_gate_dll
                    )
                    invisible(trace(
                      "dev.off",
                      where = asNamespace("grDevices"),
                      print = FALSE,
                      tracer = quote({
                        invisible(.Call(
                          .GlobalEnv$graphics_gate,
                          "close-started",
                          "close-release"
                        ))
                      })
                    ))
                    """),
            )
            worker = capture_process_identity(
                int((directory / "worker-pid").read_text())
            )
            worker_tempdir = Path((directory / "worker-tempdir").read_text())
            try:
                client.send(r="plot(1:3)", timeout_ms=0)
                output = without_elapsed(last_tool_text(client))
                assert output == "\n[running; poll with an empty send]", output
                client.transcript[-1]["result"]["content"][0]["text"] = output
                started.wait("automatic managed device closure")
                yield client, release, worker, worker_tempdir, plots
            finally:
                # Release on assertion failure before the client retires resources.
                release.release()


@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_interrupt_preserves_plot_and_state_after_deferred_closure(
    binary: Path, execution: Execution
) -> Transcript:
    with finalizing_plot(binary, execution) as (
        client,
        release,
        worker,
        temporary,
        plots,
    ):
        client.send(control="interrupt", timeout_ms=0)
        output = without_elapsed(last_tool_text(client))
        assert output == "\n[running; poll with an empty send]", output
        client.transcript[-1]["result"]["content"][0]["text"] = output
        assert current_process_identity(worker[0]) == worker

        release.release()
        client.send(timeout_ms=30_000)
        assert_result_content(
            client, [plots[0], "\n"], image_reference="live Rscript interrupted plot"
        )
        assert current_process_identity(worker[0]) == worker
        assert not list((temporary / "mcp-console-plots").glob("*.png"))

        client.send(
            # fmt: r
            r=code(r"""
                invisible(untrace("dev.off", where = asNamespace("grDevices")))
                stopifnot(identical(plot_state, 42L), is.null(dev.list()))
                plot(3:1)
                """)
        )
        assert_result_content(
            client,
            [
                'Untracing function "dev.off" in package "namespace:grDevices"\n',
                plots[1],
            ],
            image_reference="live Rscript recovery plot",
        )
        return client.finish()


@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_restart_retires_worker_during_deferred_plot_closure(
    binary: Path, execution: Execution
) -> Transcript:
    with finalizing_plot(binary, execution) as (
        client,
        release,
        worker,
        temporary,
        plots,
    ):
        # Leave the native FIFO read blocked; public restart must retire it.
        client.send(control="restart")
        assert "[worker stopped: in-memory state lost]" in last_tool_text(client)
        assert "[starting new worker]" in last_tool_text(client)
        assert current_process_identity(worker[0]) != worker
        assert all(
            part["type"] == "text"
            for part in client.transcript[-1]["result"]["content"]
        )
        assert not temporary.exists()

        client.send(
            # fmt: r
            r=code(r"""
                stopifnot(!exists("plot_state", inherits = FALSE), is.null(dev.list()))
                options(
                  console.plot.width_in = 2,
                  console.plot.height_in = 2,
                  console.plot.dpi = 96
                )
                plot(1:3)
                """)
        )
        assert_result_content(
            client, [plots[0]], image_reference="live Rscript replacement plot"
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
