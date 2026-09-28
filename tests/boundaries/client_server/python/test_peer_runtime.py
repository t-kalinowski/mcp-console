"""One Python cell contract with optional R, followed by bridge-only checks."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.native import build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, R, command, requires
from support.r import r_test_environment
from support.python import write_test_wheel
from support.ssh import SSH, configure, localhost, poison_controller, remote_command


# fmt: python
CLI_SOURCE = code("""
    import json
    import os
    import sys
    import numpy as np


    def main() -> None:
        print(
            json.dumps(
                {
                    "module": __name__,
                    "executable": sys.executable,
                    "prefix": sys.prefix,
                    "virtualenv": os.environ.get("VIRTUAL_ENV"),
                    "values": np.arange(3).tolist(),
                }
            )
        )
    """)

# fmt: r
CLI_CHECK = code("""
    check_cli <- function(command, module) {
      selected <- reticulate::py_config()$python
      prefix <- reticulate::import("sys")$prefix
      stopifnot(identical(
        unname(Sys.which(command)),
        file.path(dirname(selected), command)
      ))
      output <- system2(command, stdout = TRUE)
      stopifnot(is.null(attr(output, "status")))
      child <- jsonlite::fromJSON(output)
      # Installers may use bin/python or bin/python3 in their shebang.
      # Check executable identity and the virtualenv independently.
      stopifnot(
        identical(child$module, module),
        identical(normalizePath(child$executable), normalizePath(selected)),
        identical(normalizePath(child$prefix), normalizePath(prefix)),
        identical(child$virtualenv, Sys.getenv("VIRTUAL_ENV")),
        identical(child$values, 0:2)
      )
      cat("installed CLI uses the selected Python environment\\n")
    }
    """)


def without_r(environment: dict[str, str], root: Path) -> None:
    path = root / "empty-path"
    path.mkdir()
    environment["PATH"] = str(path)
    for name in ("R_HOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_UV"):
        environment.pop(name, None)


@executions(DIRECT, SANDBOXED)
def test_standalone_python_contract(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = dict(
            os.environ,
            RETICULATE_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
        )
        without_r(environment, root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            exercise_python(client)
            return client.finish()[3:]


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_module_configuration(binary: Path, execution: Execution) -> Transcript:
    records = None
    for with_r in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ)
            environment.pop("RETICULATE_PYTHON", None)
            if not with_r:
                uv = shutil.which("uv")
                assert uv is not None
                without_r(environment, root)
                (Path(environment["PATH"]) / "uv").symlink_to(uv)
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                client.send(requirements={"python": ["matplotlib"]})
                assert last_result_text(client) == "[prepared]", client.transcript[-1]
                # Defaults apply at the first import, not at a later cell or
                # bridge attachment. Each module keeps subsequent user choices.
                # fmt: python
                source = code("""
                    import numpy as np
                    import pandas as pd
                    import matplotlib.pyplot as plt

                    assert np.get_printoptions()["linewidth"] == 200
                    assert pd.get_option("display.width") == 200
                    np.set_printoptions(linewidth=73)
                    pd.set_option("display.width", 79)
                    custom_show = lambda *args, **kwargs: "user show"
                    plt.show = custom_show
                    print("shared module defaults installed")
                    """)
                client.send(python=source)
                assert (
                    last_result_text(client) == "shared module defaults installed\n"
                ), client.transcript[-1]
                if with_r:
                    client.send(
                        r="reticulate::py_eval(\"id(custom_show) == id(__import__('matplotlib.pyplot', fromlist=['show']).show)\")"
                    )
                    assert last_result_text(client) == "[1] TRUE\n", client.transcript[
                        -1
                    ]
                client.send(
                    python='assert np.get_printoptions()["linewidth"] == 73; assert pd.get_option("display.width") == 79; assert plt.show is custom_show; print("user module options retained")'
                )
                assert last_result_text(client) == "user module options retained\n", (
                    client.transcript[-1]
                )
                current = client.finish()[3:]
                if records is None:
                    records = current
    assert records is not None
    return records


def exercise_python(client: McpClient) -> tuple[str, ...]:
    # Every configuration exercises the same evaluator and NumPy availability
    # without referring to the R/Python bridge.
    # fmt: python
    first = code("""
        import os
        import sys
        import numpy as np
        assert np.arange(3).tolist() == [0, 1, 2]
        assert os.path.samefile(sys.executable, os.environ["MCP_CONSOLE_TEST_PYTHON"])
        assert os.path.realpath(sys.prefix) == os.path.realpath(
            os.environ["MCP_CONSOLE_TEST_PYTHON_PREFIX"]
        )
        peer_value = 40
        print("shared stdout")
        sys.stderr.write("shared stderr\\n")
        peer_value + 1
        """)
    # fmt: python
    failure = code("""
        peer_value += 2
        raise ValueError("peer runtime sentinel")
        """)
    # fmt: python
    recovery = code("""
        (
            peer_value,
            "_mcp_console" not in globals(),
            "_mcp_console" in sys.modules,
        )
        """)
    # fmt: python
    shadow = code("""
        exec = None
        eval = None
        compile = None
        peer_value
        """)
    # fmt: python
    after_shadow = code("""
        peer_value += 1
        peer_value
        """)
    output = []
    for source in (first, failure, recovery, shadow, after_shadow):
        response = client.send(python=source)
        assert not response.get("isError"), response
        output.append(last_result_text(client))
        if len(output) == 1:
            assert output[0] == "shared stdout\nshared stderr\n41\n", output[0]
    assert output[0] == "shared stdout\nshared stderr\n41\n", output[0]
    assert "<mcp-console:python:e2>" in output[1], output[1]
    assert output[1].endswith("ValueError: peer runtime sentinel\n"), output[1]
    assert output[2:] == ["(42, True, True)\n", "42\n", "43\n"], output[2:]
    return tuple(output)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_python_contract_with_and_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    # The host must have R so the same test can compare both configurations.
    # "python-first" describes cell order, not late R initialization: current
    # workers still initialize R eagerly whenever the selected session has R.
    reference = None
    records = None
    for mode in ("without-r", "r-first", "python-first"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            # Keep the exact same interpreter in all three configurations.
            # An explicit selection bypasses managed Python package resolution.
            config.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
            environment = dict(
                os.environ,
                MCP_CONSOLE_TEST_PYTHON=sys.executable,
                MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
            )
            environment.pop("RETICULATE_PYTHON", None)
            if mode == "without-r":
                without_r(environment, root)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-first":
                    # R execution itself must not force CPython initialization.
                    client.send(
                        r="stopifnot(!reticulate::py_available(initialize = FALSE))"
                    )
                    assert last_result_text(client) == "[done]"
                actual = exercise_python(client)
                if reference is None:
                    reference = actual
                else:
                    # Preserve and compare complete error text, not summaries.
                    assert actual == reference, (mode, actual, reference)
                if mode != "without-r":
                    # Only the mixed configurations add cross-language checks.
                    # The Python state above must remain the same state seen by R.
                    # fmt: r
                    r = code("""
                        stopifnot(identical(reticulate::py_eval("peer_value"), 43L))
                        peer_from_r <- 44L
                        """)
                    client.send(r=r)
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    client.send(python="(int(r.peer_from_r), peer_value)")
                    assert last_result_text(client) == "(44, 43)\n"
                transcript = client.finish()[3:]
                if mode == "without-r":
                    records = transcript
    # One representative transcript; all common Python outputs were asserted
    # byte-for-byte equal above. Bridge checks assert their distinct results.
    assert records is not None
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_virtualenv_bootstrap(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "selected environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
        )
        executable = venv / "bin/python"
        # Reticulate declares NumPy by default, including for explicit Python
        # selections. Satisfy that declaration before exercising the bridge.
        index = write_test_wheel(
            root, "mcp_console_test_cli", CLI_SOURCE, command="peer-cli"
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(executable),
                "--index",
                index.as_uri(),
                "numpy",
                "mcp-console-test-cli",
            ],
            check=True,
            capture_output=True,
        )
        modules = root / "modules"
        modules.mkdir()
        # Inspection runs isolated; this hook observes the embedded interpreter
        # and ordinary children after Console has applied the shared contract.
        # fmt: python
        hook = code("""
            import builtins
            import os
            import shutil
            import sys

            builtins.peer_bootstrap_count = getattr(builtins, "peer_bootstrap_count", 0) + 1
            builtins.peer_bootstrap = (sys.prefix, os.environ.get("VIRTUAL_ENV"), shutil.which("python"))
            """)
        (modules / "sitecustomize.py").write_text(hook)
        (modules / "peer_module.py").write_text("value = 41\n")
        # CPython's pyvenv.cfg is the environment owner. A bridge must not run
        # an activation script again after the interpreter is already live.
        (venv / "bin/activate_this.py").write_text(
            "raise AssertionError('bridge reactivated the selected interpreter')\n"
        )
        reference = None
        records = None
        for mode in ("without-r", "python-first", "r-first"):
            workspace = root / mode
            workspace.mkdir()
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=str(executable),
                MCP_CONSOLE_TEST_PYTHON=str(executable),
                MCP_CONSOLE_TEST_PYTHON_PREFIX=str(venv),
                PYTHONPATH=str(root / "unselected-modules"),
                RETICULATE_PYTHONPATH=str(modules),
            )
            if mode == "without-r":
                without_r(environment, workspace)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-first":
                    client.send(r='reticulate::py_run_string("peer_from_r = object()")')
                    assert last_result_text(client) == "[done]"
                exercise_python(client)
                # fmt: python
                source = code("""
                    import json
                    import subprocess
                    import builtins
                    import peer_module

                    assert peer_module.value == 41
                    assert os.environ["PYTHONPATH"] == os.environ["RETICULATE_PYTHONPATH"]
                    assert builtins.peer_bootstrap_count == 1
                    assert builtins.peer_bootstrap == (sys.prefix, sys.prefix, sys.executable), (
                        builtins.peer_bootstrap
                    )
                    expected = [
                        sys.executable,
                        sys.prefix,
                        sys.exec_prefix,
                        sys.base_prefix,
                        sys.base_exec_prefix,
                    ]
                    assert sys.executable == os.environ["MCP_CONSOLE_TEST_PYTHON"], (
                        sys.executable,
                        os.environ["MCP_CONSOLE_TEST_PYTHON"],
                    )
                    assert os.path.dirname(sys.executable) not in sys.path, sys.path
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    program = "import sys, json; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix]))"
                    for command in (sys.executable, "python"):
                        child = json.loads(subprocess.check_output([command, "-c", program], text=True))
                        assert child == expected, (child, expected)
                    print("selected environment retained by interpreter and children")
                    """)
                client.send(python=source)
                actual = last_result_text(client)
                assert (
                    actual
                    == "selected environment retained by interpreter and children\n"
                ), actual
                if reference is None:
                    reference = actual
                    records = client.finish()[3:]
                else:
                    assert actual == reference
                    client.send(r=CLI_CHECK)
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    client.send(r='check_cli("peer-cli", "mcp_console_test_cli")')
                    assert (
                        last_result_text(client)
                        == "installed CLI uses the selected Python environment\n"
                    ), client.transcript[-1]
                    client.finish()
        assert records is not None
        return records


@requires(R, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_r_does_not_initialize_python(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        probe = build_interposer(root, "python_initialized")
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()
            # fmt: r
            source = code("""
                probe <- dyn.load(PROBE_PATH)
                r_value <- new.env()
                r_value$answer <- 42L
                r_answer <- 42L
                python_initialized <- function() {
                  .C(
                    getNativeSymbolInfo(
                      "mcp_console_probe_python_initialized",
                      PACKAGE = probe
                    ),
                    value = 0L
                  )$value
                }
                stopifnot(python_initialized() == -1L)
                stopifnot(!reticulate::py_available(initialize = FALSE))
                r_value$answer
                """).replace("PROBE_PATH", json.dumps(str(probe)))
            client.send(r=source)
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = source.replace(
                str(probe), "<Python initialization probe>"
            )
            client.send(
                python="peer_object = object(); peer_identity = id(peer_object); int(r.r_answer)"
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            client.send(r="stopifnot(python_initialized() == 1L); r_value$answer")
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.send(
                python="assert id(peer_object) == peer_identity; print('runtime objects retained')"
            )
            assert last_result_text(client) == "runtime objects retained\n", (
                client.transcript[-1]
            )
            return client.finish()[3:]


@requires(R, SSH, command("uv"), command("ir"))
@executions(DIRECT, SANDBOXED)
def test_remote_managed_identity_survives_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        environment, _ = r_test_environment()
        environment = {
            name: value
            for name, value in environment.items()
            if name
            in {
                "PATH",
                "HOME",
                "R_HOME",
                "R_LIBS",
                "R_LIBS_USER",
                "R_LIBS_SITE",
                "R_PROFILE_USER",
                "IR_CACHE_DIR",
                "UV_CACHE_DIR",
            }
        }
        configure(local, remote, remote_command(remote, binary, environment))
        with localhost(root / "sshd") as controller:
            trap = poison_controller(root / "sshd", controller)
            with McpClient(binary, execution.serve(), controller, local) as client:
                client.initialize_and_list_tools()
                client.send(
                    python="import sys; peer_object = object(); peer_id = id(peer_object)"
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(requirements={"python": ["py-yaml12"]})
                assert not client.transcript[-1]["result"].get("isError"), (
                    client.transcript[-1]
                )
                client.send(
                    python="import yaml12; assert id(peer_object) == peer_id; print('live identity retained')"
                )
                assert last_result_text(client) == "live identity retained\n", (
                    client.transcript[-1]
                )
                client.send(
                    control="restart",
                    python="import yaml12; print('accepted environment retained')",
                )
                assert (
                    last_result_text(client)
                    == "[worker stopped: in-memory state lost]\n[starting new worker]\naccepted environment retained\n[done]"
                ), client.transcript[-1]
                records = client.finish()[3:]
            assert not trap.exists(), (
                "controller inspected or resolved a remote runtime"
            )
        return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_managed_bootstrap_and_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    records = None
    for with_r in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ)
            environment.pop("RETICULATE_PYTHON", None)
            if not with_r:
                uv = shutil.which("uv")
                assert uv is not None
                without_r(environment, root)
                (Path(environment["PATH"]) / "uv").symlink_to(uv)
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                defaults = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]["python"]
                assert "numpy" in defaults, defaults
                if with_r:
                    client.send(
                        r="stopifnot(!reticulate::py_available(initialize = FALSE))"
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(requirements={"python": ["py-yaml12"]})
                assert not client.transcript[-1]["result"].get("isError"), (
                    client.transcript[-1]
                )
                # fmt: python
                source = code("""
                    import json
                    import os
                    import subprocess
                    import sys
                    import yaml12
                    import numpy as np

                    assert np.arange(3).tolist() == [0, 1, 2]

                    peer_object = object()
                    peer_id = id(peer_object)
                    peer_library = sys.base_prefix
                    expected = [
                        sys.executable,
                        sys.prefix,
                        sys.exec_prefix,
                        sys.base_prefix,
                        sys.base_exec_prefix,
                    ]
                    program = "import sys, json; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix]))"
                    assert (
                        json.loads(subprocess.check_output([sys.executable, "-c", program], text=True))
                        == expected
                    )
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    print("managed identity and child environment agree")
                    """)
                client.send(python=source)
                assert (
                    last_result_text(client)
                    == "managed identity and child environment agree\n"
                ), client.transcript[-1]
                client.send(
                    python="import more_itertools; assert id(peer_object) == peer_id; assert sys.base_prefix == peer_library; print('live import retained objects')"
                )
                assert last_result_text(client) == "live import retained objects\n", (
                    client.transcript[-1]
                )
                client.send(python="raise ValueError('after accepted activation')")
                assert last_result_text(client).endswith(
                    "ValueError: after accepted activation\n"
                ), client.transcript[-1]
                client.send(control="restart")
                client.send(
                    python="import yaml12, more_itertools; print('accepted additions survived restart')"
                )
                assert (
                    last_result_text(client) == "accepted additions survived restart\n"
                ), client.transcript[-1]
                client.send(python="import os; os._exit(47)")
                client.send(
                    python="import yaml12, more_itertools; print('accepted additions survived crash')"
                )
                assert (
                    last_result_text(client) == "accepted additions survived crash\n"
                ), client.transcript[-1]
                current = client.finish()[3:]
                if not with_r:
                    records = current
    assert records is not None
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_r_commands_follow_managed_python_activation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        index = write_test_wheel(
            root, "mcp_console_test_cli", CLI_SOURCE, command="peer-cli"
        )
        write_test_wheel(
            root, "mcp_console_test_cli_added", CLI_SOURCE, command="peer-cli-added"
        )
        environment = dict(
            os.environ, UV_INDEX=index.as_uri(), UV_INDEX_STRATEGY="first-index"
        )
        environment.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"python": ["mcp-console-test-cli"]})
            assert last_result_text(client) == "[prepared]", client.transcript[-1]
            client.send(
                python="import numpy as np; peer_object = object(); peer_id = id(peer_object)"
            )
            assert last_result_text(client) == "[done]", client.transcript[-1]
            client.send(r=CLI_CHECK)
            assert last_result_text(client) == "[done]", client.transcript[-1]
            client.send(
                r='check_cli("peer-cli", "mcp_console_test_cli"); stopifnot(Sys.which("peer-cli-added") == "")'
            )
            assert (
                last_result_text(client)
                == "installed CLI uses the selected Python environment\n"
            ), client.transcript[-1]
            client.send(requirements={"python": ["mcp-console-test-cli-added"]})
            assert last_result_text(client) == "[prepared]", client.transcript[-1]
            client.send(
                python="assert id(peer_object) == peer_id; assert np.arange(3).tolist() == [0, 1, 2]"
            )
            assert last_result_text(client) == "[done]", client.transcript[-1]
            for command, module in (
                ("peer-cli", "mcp_console_test_cli"),
                ("peer-cli-added", "mcp_console_test_cli_added"),
            ):
                client.send(r=f'check_cli("{command}", "{module}")')
                assert (
                    last_result_text(client)
                    == "installed CLI uses the selected Python environment\n"
                ), client.transcript[-1]
            client.send(control="restart")
            client.send(r=CLI_CHECK)
            assert last_result_text(client) == "[done]", client.transcript[-1]
            client.send(r='check_cli("peer-cli-added", "mcp_console_test_cli_added")')
            assert (
                last_result_text(client)
                == "installed CLI uses the selected Python environment\n"
            ), client.transcript[-1]
            return client.finish()[3:]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_attaches_to_python_initialized_during_r_startup(
    binary: Path, execution: Execution
) -> Transcript:
    return attach_python_initialized_during_r_startup(binary, execution, managed=False)


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_managed_import_after_python_initialized_during_r_startup(
    binary: Path, execution: Execution
) -> Transcript:
    return attach_python_initialized_during_r_startup(binary, execution, managed=True)


def attach_python_initialized_during_r_startup(
    binary: Path, execution: Execution, *, managed: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        library = root / "library"
        library.mkdir()
        environment, rscript = r_test_environment()
        fixture = Path(__file__).resolve().parents[3] / "fixtures/early_python"
        subprocess.run(
            [rscript.with_name("R"), "CMD", "INSTALL", f"--library={library}", fixture],
            check=True,
            capture_output=True,
            env=environment,
        )
        environment.update(
            R_LIBS=os.pathsep.join(
                filter(None, (str(library), environment.get("R_LIBS")))
            ),
            R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsoleearlypython",
            RETICULATE_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON=sys.executable,
            MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
        )
        if managed:
            environment.pop("RETICULATE_PYTHON")
            # The startup package selects a prepared environment without
            # running reticulate's resolver inside the worker sandbox.
            virtualenv = root / "early-python"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(virtualenv)],
                check=True,
                capture_output=True,
            )
            early_python = virtualenv / "bin/python"
            subprocess.run(
                ["uv", "pip", "install", "--python", str(early_python), "numpy"],
                check=True,
                capture_output=True,
            )
            environment["MCP_CONSOLE_TEST_EARLY_PYTHON"] = str(early_python)
            version = "==" + ".".join(map(str, sys.version_info[:3]))
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            if managed:
                client.send(requirements={"python_version": [version]})
                assert last_result_text(client) == "[prepared]", client.transcript[-1]
            else:
                exercise_python(client)
            client.send(
                # fmt: python
                python=code("""
                    import json
                    import subprocess

                    assert id(early_object) == early_identity
                    assert sys.executable == early_executable
                    assert (
                        sys.prefix,
                        sys.exec_prefix,
                        sys.base_prefix,
                        sys.base_exec_prefix,
                    ) == early_prefixes
                    assert os.environ["PATH"] == early_path
                    child = json.loads(
                        subprocess.check_output([sys.executable, "-c", early_child_program], text=True)
                    )
                    assert child == early_child, (child, early_child)
                    print("attached to the existing interpreter")
                    """)
            )
            assert (
                last_result_text(client) == "attached to the existing interpreter\n"
            ), client.transcript[-1]
            client.send(r='reticulate::py_eval("id(early_object) == early_identity")')
            assert last_result_text(client) == "[1] TRUE\n", client.transcript[-1]
            if managed:
                client.send(
                    python="import yaml12; assert id(early_object) == early_identity; print('adopted interpreter resolved import')"
                )
                assert last_result_text(client) == (
                    "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
                    "adopted interpreter resolved import\n"
                ), client.transcript[-1]
                accepted = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert "py-yaml12" in accepted["python"], accepted
                assert accepted["python_version"] == [version], accepted
                client.send(control="restart")
                # The startup package chooses its environment again before
                # Console attaches. Server-retained declarations survive that
                # adoption; it does not replay activation into the live runtime.
                retained = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert retained == accepted, (retained, accepted)
            records = client.finish()[3:]
            if managed:
                records = json.loads(
                    json.dumps(records).replace(version, "<selected Python version>")
                )
            return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_managed_import_failures(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_without_r import (
        automatic_activation_failure_requires_restart,
        automatic_resolution_failure_and_cancel_keep_accepted_state,
    )

    records = None
    for with_r in (False, True):
        failures = automatic_resolution_failure_and_cancel_keep_accepted_state(
            binary, execution, with_r=with_r
        )
        activation = automatic_activation_failure_requires_restart(
            binary, execution, with_r=with_r
        )
        if records is None:
            records = failures + activation
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_shared_managed_tool_activation_failure(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_without_r import (
        live_python_activation_failure_requires_restart,
    )

    records = None
    for with_r in (False, True):
        current = live_python_activation_failure_requires_restart(
            binary, execution, with_r=with_r
        )
        if records is None:
            records = current
    return records
