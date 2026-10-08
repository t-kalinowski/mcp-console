"""Native project lookup retains its paths when Console replaces a library."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_configuration import configure
from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import ir_run_records, ir_requirements
from support.snapshots import execution_snapshots
from support.suites import run_this_suite


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_renv_lookup_and_console_layer_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        env, record = recording_fixture_r_environment(
            root, ("consoleambient", "consolemissing")
        )
        # The prepared project tests native lookup and activation, without
        # generating renv's separate system-package view during startup.
        env["RENV_CONFIG_SANDBOX_ENABLED"] = "FALSE"
        ir = env["MCP_CONSOLE_TEST_REAL_IR"]
        result = subprocess.run(
            [
                ir,
                "run",
                "--with",
                "renv",
                "--with",
                "reticulate",
                "--with",
                "DBI",
                "--with",
                "duckdb",
                "--with",
                "arrow",
                "--with",
                "nanoarrow",
                "--with",
                "jsonlite",
                "--with",
                "pillar",
                "--with",
                "tibble",
                "--with",
                "utf8",
                "--isolated",
                "--vanilla",
                "-e",
                "cat(.libPaths()[[1L]])",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        base = Path(result.stdout.strip())
        assert base.is_dir(), result
        root.joinpath("base-r-library").write_text(str(base))
        extra = root / "project-extra"
        extra.mkdir()
        # Prepare renv outside sandboxed execution. Native startup owns activation.
        source = code(r"""
            renv::init(project = Sys.getenv("CONSOLE_PROJECT"), bare = TRUE, restart = FALSE)
            project <- renv::paths$library(project = Sys.getenv("CONSOLE_PROJECT"))
            base <- Sys.getenv("CONSOLE_BASE")
            for (package in list.files(base, full.names = TRUE)) {
              target <- file.path(project, basename(package))
              if (!file.exists(target)) stopifnot(file.symlink(package, target))
            }
            ambient <- file.path(strsplit(Sys.getenv("MCP_CONSOLE_TEST_IR_SOURCE_LIBRARIES"), .Platform$path.sep)[[1L]][[1L]], "consoleambient")
            stopifnot(file.symlink(ambient, file.path(project, "consoleambient")))
            cat(project)
            """)
        preparation = subprocess.run(
            [Path(env["R_HOME"]) / "bin/Rscript", "--vanilla", "-"],
            input=source,
            env={
                **env,
                "R_LIBS": str(base),
                "CONSOLE_PROJECT": str(root),
                "CONSOLE_BASE": str(base),
                "RENV_CONFIG_AUTO_SNAPSHOT": "FALSE",
            },
            capture_output=True,
            text=True,
            check=True,
        )
        assert preparation.returncode == 0
        # Native startup may also leave read-only directories in worker TMPDIR.
        root.joinpath(".Rprofile").write_text(
            'source("renv/activate.R")\n.libPaths(c(.libPaths(), "project-extra"))\nlibrary(consoleambient)\nproject_paths <- .libPaths()\ndir.create(file.path(tempdir(), "native-readonly"))\nSys.chmod(file.path(tempdir(), "native-readonly"), "0555")\n'
        )
        env.update(
            RENV_CONFIG_AUTO_SNAPSHOT="FALSE",
            RENV_CONFIG_STARTUP_QUIET="TRUE",
            RENV_CONFIG_SYNCHRONIZED_CHECK="FALSE",
            RENV_CONFIG_USER_PROFILE="FALSE",
            R_PROFILE_USER=str(root / ".Rprofile"),
            RETICULATE_PYTHON=sys.executable,
        )
        configure(
            root,
            {
                "r": {"packages": [], "resolution": "automatic"},
                "python": sys.executable,
                "languages": ["r"],
            },
        )
        arguments = execution.serve(
            "-c",
            "cache=host",
            *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
        )
        with McpClient(
            binary, arguments, env, root, use_r_startup_files=True
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "ambient available\n",
                r='stopifnot(exists("project_paths"), consoleambient::fixture(), identical(as.character(packageVersion("consoleambient")), "0.0.0.9000")); cat("ambient available\\n")',
            )
            baseline = len(ir_run_records(record))
            client.expect(
                "ambient retained\n",
                r='stopifnot(requireNamespace("consoleambient"), consoleambient::fixture()); cat("ambient retained\\n")',
            )
            assert len(ir_run_records(record)) == baseline
            prepared = client.send(requirements={"r": ["consolemissing"]})
            assert not prepared.get("isError"), prepared
            runs = ir_run_records(record)[baseline:]
            assert len(runs) == 1 and "consolemissing" in ir_requirements(runs[0]), runs
            client.expect(
                "project paths retained\n",
                r='stopifnot(consolemissing::fixture(), identical(.libPaths()[.libPaths() %in% project_paths], project_paths)); cat("project paths retained\\n")',
            )
            assert len(ir_run_records(record)) == baseline + 1
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
