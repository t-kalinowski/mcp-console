"""Runtime path expansion, input layering, and bounded fallback contracts."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_configuration import (
    configure,
    without_r,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.python import virtualenv_python
from support.r import r_test_environment, isolated_r_home
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import recording_ir_environment, ir_run_records
from support.snapshots import execution_snapshots
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_r_shorthand_layering_and_path_expansion(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    variants = [
        ("./chosen R", ("r.resolution=disabled",), False),
        (
            {"resolution": "disabled", "vanilla": True, "packages": []},
            ("r=./chosen R",),
            True,
        ),
        (
            {"resolution": "disabled", "vanilla": True},
            ("r=null", "r=./chosen R", "r.resolution=disabled"),
            False,
        ),
        ({"resolution": "disabled"}, ("r.executable=~/chosen R",), False),
        ({"executable": "./chosen R", "resolution": "disabled"}, (), False),
    ]
    for document, overrides, vanilla in variants:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            selected = shutil.which("R")
            assert selected is not None
            (root / "chosen R").symlink_to(selected)
            env, _ = r_test_environment()
            env.update(
                HOME=str(root),
                RETICULATE_PYTHON=sys.executable,
                R_HOME=str(root / "stale home"),
            )
            configure(root, {"r": document, "languages": ["r"]})
            arguments = execution.serve(
                "-c",
                "cache=host",
                *(item for override in overrides for item in ("-c", override)),
            )
            with McpClient(binary, arguments, env, root) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "layered R selected\n",
                    r=f'stopifnot(identical("--vanilla" %in% commandArgs(), {"TRUE" if vanilla else "FALSE"})); cat("layered R selected\\n")',
                )
                inspection = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]
                assert (
                    inspection["resolution"]["r"] == "disabled"
                    and inspection["requirements"]["r"] == []
                ), inspection
                records.extend(client.finish())
    return records


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_selected_r_installation_reaches_ir_and_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        env, record = recording_ir_environment(root)
        installed = isolated_r_home(root, env)
        env.update(
            RETICULATE_PYTHON=sys.executable,
            R_HOME="invalid-inherited-home",
            RHOME="invalid-inherited-home",
        )
        configure(
            root,
            {
                "r": {
                    "executable": str(installed / "bin/R"),
                    "resolution": "explicit",
                    "packages": [],
                },
                "python": sys.executable,
                "languages": ["r"],
                "environment": {"R_HOME": "invalid-configured-home"},
            },
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.initialize_and_list_tools()
            resources = subprocess.check_output(
                [
                    installed / "bin/R",
                    "CMD",
                    "/bin/sh",
                    "-c",
                    'printf "%s\n" "$R_SHARE_DIR" "$R_INCLUDE_DIR" "$R_DOC_DIR"',
                ],
                env={**env, "R_HOME": str(installed)},
                text=True,
            ).splitlines()
            assert len(resources) == 3
            # fmt: r
            program = code(r"""
                expected <- normalizePath("R", mustWork = TRUE)
                stopifnot(identical(normalizePath(R.home()), expected))
                stopifnot(identical(Sys.getenv("RHOME"), expected))
                stopifnot(identical(
                  unname(Sys.getenv(c("R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR"))),
                  c(RESOURCES)
                ))
                Sys.setenv(R_HOME = "changed", RHOME = "changed")
                cat("matching R installation\n")
                """).replace(
                "RESOURCES", ", ".join(json.dumps(path) for path in resources)
            )
            client.expect("matching R installation\n", r=program)
            runs = ir_run_records(record)
            assert runs and all(
                run["arguments"][run["arguments"].index("--rscript") + 1]
                == str(installed / "bin/Rscript")
                for run in runs
            ), runs
            client.send(control="restart")
            client.expect("matching R installation\n", r=program)
            assert ir_run_records(record) == runs
            return client.finish()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_native_r_executable_uses_matching_installation(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        env, _ = r_test_environment()
        installed = Path(env["R_HOME"])
        selected = root / "native R"
        selected.symlink_to(installed / "bin/exec/R")
        env.update(RETICULATE_PYTHON=sys.executable, R_HOME="invalid-inherited-home")
        configure(
            root,
            {
                "r": {"executable": str(selected), "resolution": "disabled"},
                "python": sys.executable,
                "languages": ["r"],
            },
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.initialize_and_list_tools()
            program = f'stopifnot(identical(normalizePath(R.home()), normalizePath({json.dumps(str(installed))}))); cat("native R selected\\n")'
            client.expect("native R selected\n", r=program)
            client.send(control="restart")
            client.expect("native R selected\n", r=program)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_python_venv_paths_and_present_fallback_failures(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        venv = root / "ordinary venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            capture_output=True,
            check=True,
        )
        tools = root / "tools"
        tools.mkdir()
        env = {
            **without_r(tools),
            "CONDA_PREFIX": "unrelated",
            "CONDA_DEFAULT_ENV": "unrelated",
            "HOME": str(root),
        }
        for python in (
            "./ordinary venv",
            {"existing": str(virtualenv_python(venv))},
            "~/ordinary venv",
        ):
            configure(root, {"python": python, "languages": ["python"]})
            with McpClient(
                binary, execution.serve("-c", "cache=host"), env, root
            ) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "venv retained\n",
                    python="import sys; from pathlib import Path; assert Path(sys.prefix) == Path.cwd() / 'ordinary venv'; assert sys.prefix != sys.base_prefix; print('venv retained')",
                )
                selected = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["selection"]
                assert selected["python"] == str(virtualenv_python(venv)), selected
                records.extend(client.finish())
        (root / "dangling").symlink_to(root / "absent-interpreter")
        (root / "broken-directory").mkdir()
        conda = root / "conda"
        conda.mkdir()
        (conda / "conda-meta").mkdir()
        failures = [
            ("absent", "cannot use existing Python"),
            (
                {"first_available": [{"existing": "dangling"}, {"managed": {}}]},
                "cannot use existing Python",
            ),
            (
                {
                    "first_available": [
                        {"existing": "broken-directory"},
                        {"managed": {}},
                    ]
                },
                "standard venv",
            ),
            (
                {"first_available": [{"existing": "conda"}, {"managed": {}}]},
                "Conda environments are unsupported",
            ),
        ]
        for python, expected in failures:
            configure(root, {"python": python, "languages": ["python"]})
            with McpClient(
                binary, execution.serve("-c", "cache=host"), env, root
            ) as client:
                client.initialize_and_list_tools()
                result = client.send(python="print('must not run')")
                assert (
                    result.get("isError") and expected in result["content"][0]["text"]
                ), result
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert expected in stderr, stderr
                records.extend(transcript)
                records.append({"stderr": stderr})
        return json.loads(json.dumps(records).replace(str(root), "<fixture>"))


if __name__ == "__main__":
    run_this_suite(__file__)
