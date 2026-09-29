"""One Python cell contract with optional R, followed by bridge-only checks."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.native import build_interposer
from support.records import ToolResult, Transcript
from support.resolvers import (
    checkpoint_uv_environment,
    send_and_collect_runtime_python_resolution,
)
from support.requirements import NATIVE_FIXTURES, R, command, requires
from support.r import isolated_r_home, r_test_environment, reference_plots
from support.python import write_test_wheel
from support.ssh import (
    SSH,
    configure,
    localhost,
    poison_controller,
    remote_command,
    client_environment,
)
from support.ssh_external import EXTERNAL_SSH, external_target


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
    retain_system_bwrap(path, environment.get("PATH"))
    environment["PATH"] = str(path)
    for name in ("R_HOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_UV"):
        environment.pop(name, None)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_warms_interpreters_without_a_send(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        modules = root / "modules"
        modules.mkdir()
        started = FifoCheckpoint.create(root / "python-started")
        release = FifoCheckpoint.create(root / "python-release")
        try:
            # fmt: python
            hook = code("""
                import builtins
                import ctypes
                import os
                from pathlib import Path

                assert ctypes.c_void_p.in_dll(ctypes.CDLL(None), "R_GlobalEnv").value
                builtins.peer_startups = getattr(builtins, "peer_startups", 0) + 1
                with open(os.environ["PEER_STARTED"], "wb", buffering=0) as signal:
                    signal.write(b"1")
                with open(os.environ["PEER_RELEASE"], "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                print("startup hook completed")
                """)
            (modules / "sitecustomize.py").write_text(hook)
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=sys.executable,
                RETICULATE_PYTHONPATH=str(modules),
                PEER_STARTED=str(started.path),
                PEER_RELEASE=str(release.path),
            )
            args = (
                execution.serve("--writable-root", str(root))
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, args, environment, root) as client:
                client.initialize_and_list_tools()
                started.wait("Python initialized after R without any send", timeout=60)
                client.request("tools/list")
                client.send(
                    python="assert peer_startups == 1; cell_count = globals().get('cell_count', 0) + 1",
                    timeout_ms=0,
                )
                assert "running" in last_result_text(
                    client
                ) or "starting" in last_result_text(client)
                release.release()
                client.send()
                assert not client.transcript[-1]["result"].get("isError", False), (
                    client.transcript[-1]
                )
                client.send(
                    python="assert peer_startups == 1 and cell_count == 1; print('initialized once, evaluated once')"
                )
                assert (
                    last_result_text(client) == "initialized once, evaluated once\n"
                ), client.transcript[-1]
                return client.finish()[4:]
        finally:
            started.close()
            release.close()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_startup_input_services_a_waiting_first_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        started = FifoCheckpoint.create(root / "started")
        release = FifoCheckpoint.create(root / "release")
        try:
            (root / "sitecustomize.py").write_text(
                code("""
                import builtins
                import os
                with open(os.environ["STARTUP_STARTED"], "wb", buffering=0) as signal:
                    signal.write(b"1")
                with open(os.environ["STARTUP_RELEASE"], "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                import sys
                import types
                class StartupPyplot(types.ModuleType):
                    def show(self, *args):
                        raise AssertionError("default show was not replaced")
                    def __setattr__(self, name, value):
                        if name == "show":
                            builtins.startup_answer = input("startup> ")
                            print("startup received:", builtins.startup_answer)
                        super().__setattr__(name, value)
                    def get_fignums(self):
                        return []
                    def close(self, *args):
                        pass
                StartupPyplot.show.__module__ = "matplotlib.pyplot"
                sys.modules["matplotlib.pyplot"] = StartupPyplot("matplotlib.pyplot")
                """)
            )
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=sys.executable,
                RETICULATE_PYTHONPATH=str(root),
                STARTUP_STARTED=str(started.path),
                STARTUP_RELEASE=str(release.path),
            )
            args = (
                execution.serve("--writable-root", str(root))
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(
                binary, args, environment, root, response_timeout=5
            ) as client:
                client.initialize_and_list_tools()
                started.wait("automatic Python startup")
                waiting = client.start_send(
                    python="assert startup_answer == 'answer'; print('first cell ran')"
                )
                release.release()
                client.receive(waiting)
                assert "[waiting for stdin]" in last_result_text(client), (
                    client.transcript[-1]
                )
                client.send(stdin="answer\n")
                assert "first cell ran" in last_result_text(client), client.transcript[
                    -1
                ]
                client.send()
                assert last_result_text(client) == "\n[idle]", client.transcript[-1]
                return client.finish()[4:]
        finally:
            started.close()
            release.close()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_absent_python_preserves_r(binary: Path, execution: Execution) -> Transcript:
    from support.resolvers import bare_runtime_environment

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment, rscript = r_test_environment()
        environment = bare_runtime_environment(environment, root)
        empty = root / "bin"
        empty.mkdir()
        environment.update(R_HOME=str(rscript.parent.parent), PATH=str(empty))
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(r="preserved <- 42L; preserved")
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.send(python="raise AssertionError('absent Python ran')")
            assert client.transcript[-1]["result"].get("isError"), client.transcript[-1]
            client.send(r="preserved")
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            return client.finish()


def exercise_peer_state(client: McpClient, trigger: str = "python-access") -> None:
    # Inspect libR's actual state, not cell order or PID. A loader
    # re-exec after this cell would lose the objects below.
    # fmt: python
    source = code("""
        import builtins
        import ctypes
        import io
        import os
        import sqlite3
        import sys
        import numpy as np

        def r_initialized():
            try:
                return bool(ctypes.c_void_p.in_dll(ctypes.CDLL(None), "R_GlobalEnv").value)
            except ValueError:
                return False

        assert r_initialized(), "R did not initialize before Python"
        assert builtins.peer_startups == 1
        persistent = object()
        connection = sqlite3.connect(":memory:")
        connection.execute("create table peer(value integer)")
        connection.execute("insert into peer values (42)")
        console_sql_connection(connection)
        np.set_printoptions(linewidth=73)
        before = (id(persistent), id(connection), sys.executable, sys.prefix,
                  os.environ["MPLCONFIGDIR"], os.environ["XDG_CACHE_HOME"])
        print("Both peer runtimes initialized")
        original_streams = (sys.stdout, sys.stderr)
        redirected_stdout = io.StringIO()
        redirected_stderr = io.StringIO()
        sys.stdout, sys.stderr = redirected_stdout, redirected_stderr
        """)
    client.send(python=source)
    assert last_result_text(client) == "Both peer runtimes initialized\n", (
        client.transcript[-1]
    )
    if trigger == "r-cell":
        client.send(
            r="peer_from_r <- 41L; stopifnot(reticulate::py_eval('persistent is not None'))"
        )
        assert last_result_text(client) == "[done]", client.transcript[-1]
    else:
        client.send(python="assert 3 < r.pi < 4; assert int(r['sum(c(20, 21))']) == 41")
        assert last_result_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    source = code("""
        assert r_initialized()
        assert sys.stdout is redirected_stdout
        assert sys.stderr is redirected_stderr
        print("redirected stdout")
        print("redirected stderr", file=sys.stderr)
        assert redirected_stdout.getvalue() == "redirected stdout\\n"
        assert redirected_stderr.getvalue() == "redirected stderr\\n"
        sys.stdout, sys.stderr = original_streams
        assert builtins.peer_startups == 1
        assert before == (
            id(persistent),
            id(connection),
            sys.executable,
            sys.prefix,
            os.environ["MPLCONFIGDIR"],
            os.environ["XDG_CACHE_HOME"],
        )
        assert connection.execute("select value from peer").fetchone() == (42,)
        assert np.get_printoptions()["linewidth"] == 73


        def nested_peer():
            return int(r["sum(c(40, 2))"])


        print("Python state and services retained")
        """)
    client.send(python=source)
    assert last_result_text(client) == "Python state and services retained\n", (
        client.transcript[-1]
    )
    client.send(sql="select value from peer")
    assert "42" in last_result_text(client), client.transcript[-1]
    client.send(r="stopifnot(identical(reticulate::py$nested_peer(), 42L))")
    assert last_result_text(client) == "[done]", client.transcript[-1]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_cross_language_access_preserves_python_runtime(
    binary: Path, execution: Execution
) -> Transcript:
    records = None
    for trigger in ("r-cell", "python-access"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / "modules"
            modules.mkdir()
            (modules / "sitecustomize.py").write_text(
                "import builtins\n"
                "builtins.peer_startups = getattr(builtins, 'peer_startups', 0) + 1\n"
            )
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=sys.executable,
                RETICULATE_PYTHONPATH=str(modules),
            )
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                exercise_peer_state(client, trigger)
                current = client.finish()[3:]
                if records is None:
                    records = current
    assert records is not None
    return records


@requires(EXTERNAL_SSH)
def test_external_peer_initialization_order(binary: Path) -> Transcript:
    records = []
    with external_target() as external, tempfile.TemporaryDirectory() as directory:
        local = Path(directory)
        hooks = external["target"]["workspace"] + "/cli/python-hooks"
        config = local / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "target": external["target"],
                    "sandbox": {
                        "environment": {
                            **external["environment"],
                            "RETICULATE_PYTHONPATH": hooks,
                        }
                    },
                }
            )
        )
        environment = client_environment(
            local, config=external.get("ssh_config"), remote_path=external.get("path")
        )
        trap = poison_controller(local, environment)
        for execution in (DIRECT, SANDBOXED):
            serve = (
                execution.serve("--writable-root", "cli")
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, serve, environment, local) as client:
                client.initialize_and_list_tools()
                # Create the startup-hook fixture on its execution host, then
                # start the generation whose in-memory continuity is exercised.
                client.send(
                    python=code("""
                    import os
                    from pathlib import Path
                    hooks = Path(os.environ["RETICULATE_PYTHONPATH"])
                    hooks.mkdir(exist_ok=True)
                    _ = (hooks / "sitecustomize.py").write_text(
                        "import builtins\\nbuiltins.peer_startups = getattr(builtins, 'peer_startups', 0) + 1\\n"
                    )
                    """)
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(control="restart")
                exercise_peer_state(client)
                records.extend(client.finish()[3:])
        assert not trap.exists(), (
            "controller inspected or resolved an execution-host runtime"
        )
    return records


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_bridge_preserves_environment_metadata(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        executable = Path(sys._base_executable)
        prefix = root / "virtualenv"
        for kind in ("base", "virtualenv", "conda-marker"):
            if kind == "virtualenv":
                subprocess.run(
                    [sys.executable, "-m", "venv", "--without-pip", str(prefix)],
                    check=True,
                )
                executable = prefix / "bin/python"
                # Explicit environments still need reticulate's default
                # NumPy declaration satisfied before bridge attachment.
                subprocess.run(
                    ["uv", "pip", "install", "--python", str(executable), "numpy"],
                    check=True,
                    capture_output=True,
                )
            elif kind == "conda-marker":
                # Exercise both values of reticulate's Conda metadata marker in
                # a test-owned environment, without installing a Conda manager.
                (prefix / "conda-meta").mkdir()
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=str(executable),
                MCP_CONSOLE_TEST_PYTHON=str(executable),
                MCP_CONSOLE_TEST_ENVIRONMENT_KIND=kind,
                MCP_CONSOLE_TEST_VIRTUALENV="" if kind == "base" else str(prefix),
            )
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                client.send(
                    python="peer_object = object(); peer_identity = id(peer_object)"
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                # fmt: r
                client.send(
                    r=code("""
                    config <- reticulate::py_config()
                    sys <- reticulate::import("sys")
                    stopifnot(
                      identical(config$python, Sys.getenv("MCP_CONSOLE_TEST_PYTHON")),
                      identical(config$prefix, sys$prefix),
                      identical(config$exec_prefix, sys$exec_prefix),
                      identical(config$base_prefix, sys$base_prefix),
                      identical(config$base_exec_prefix, sys$base_exec_prefix),
                      !isTRUE(config$ephemeral),
                      identical(
                        config$conda,
                        Sys.getenv("MCP_CONSOLE_TEST_ENVIRONMENT_KIND") == "conda-marker"
                      ),
                      identical(config$virtualenv, Sys.getenv("MCP_CONSOLE_TEST_VIRTUALENV")),
                      identical(config$virtualenv_activate, "")
                    )
                    cat("environment metadata retained\\n")
                    """)
                )
                assert last_result_text(client) == "environment metadata retained\n", (
                    kind,
                    client.transcript[-1],
                )
                client.send(
                    python="assert id(peer_object) == peer_identity; print('same interpreter')"
                )
                assert last_result_text(client) == "same interpreter\n", (
                    client.transcript[-1]
                )
                records.extend([{"environment": kind}, *client.finish()[3:]])
    return records


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_interrupt_wakes_input_before_and_after_attachment(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        release = FifoCheckpoint.create(root / "interrupt")
        environment = dict(os.environ, RETICULATE_PYTHON=sys.executable)
        serve = (
            execution.serve("--writable-root", str(root))
            if execution == SANDBOXED
            else execution.serve()
        )
        try:
            with McpClient(binary, serve, environment, root) as client:
                client.initialize_and_list_tools()
                for attached in (False, True):
                    if attached:
                        client.send(python="assert int(r['42L']) == 42")
                        assert last_result_text(client) == "[done]", client.transcript[
                            -1
                        ]
                    client.send(
                        python=code("""
                        import signal
                        import threading

                        def interrupt_from_background():
                            with open("interrupt", "rb", buffering=0) as gate:
                                assert gate.read(1) == b"1"
                            signal.pthread_kill(threading.get_ident(), signal.SIGINT)

                        sender = threading.Thread(target=interrupt_from_background)
                        sender.start()
                        try:
                            input("interrupt gate> ")
                        except KeyboardInterrupt:
                            print("input cancelled once")
                        else:
                            raise AssertionError("input was not interrupted")
                        sender.join()
                        """)
                    )
                    assert (
                        last_result_text(client)
                        == '[input requested: "interrupt gate> "]\n[waiting for stdin]'
                    ), client.transcript[-1]
                    # The public input request proves the main thread is blocked.
                    # SIGINT goes to another thread, so only the self-pipe wakes it.
                    release.release()
                    wait_for_evaluation_output(
                        client, "input cancelled once\n", "background-thread interrupt"
                    )
                    client.send(python="print('no duplicate interrupt')")
                    assert last_result_text(client) == "no duplicate interrupt\n", (
                        client.transcript[-1]
                    )
                return client.finish()[3:]
        finally:
            release.close()


def exercise_prepared_r_only(binary: Path, provider: str) -> None:
    # Extend each provider's existing public runtime-discovery acceptance.
    from support import docker, docker_sandbox

    fixture = docker if provider == "docker" else docker_sandbox
    workspace = (
        fixture.workspace() if provider == "docker" else fixture.workspace(real=True)
    )
    with workspace as root:
        environment = {
            "R_HOME": "/usr/lib/R",
            "PATH": "/no-python",
            "RETICULATE_PYTHON": "",
        }
        if provider == "docker":
            config = fixture.configure(
                root,
                fixture.image(),
                command=["/opt/analysis/bin/mcp-console"],
                environment=environment,
            )
        else:
            config = fixture.configure(
                root, command=["/usr/local/bin/mcp-console"], environment=environment
            )
        policy = json.loads(config.read_text())
        policy["sandbox"]["inherit_environment"] = False
        config.write_text(json.dumps(policy))
        with McpClient(
            binary, ("serve", "--no-sandbox"), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            fields = tool["inputSchema"]["properties"]
            assert {"r", "sql", "python"} <= fields.keys(), fields
            assert "when available" in tool["description"], tool
            result = client.send(python="raise AssertionError('unavailable cell ran')")
            assert result["isError"], result
            assert last_result_text(client) == (
                "Python cells are unavailable: the target has no Python runtime"
            ), result
            client.send(
                r="stopifnot(!reticulate::py_available(initialize = FALSE)); answer <- 42L; answer"
            )
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.send(
                r="stopifnot(!reticulate::py_available(initialize = FALSE)); answer"
            )
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.finish()
        policy["python"] = "/missing-explicit-python"
        config.write_text(json.dumps(policy))
        with McpClient(
            binary, ("serve", "--no-sandbox"), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(r="stop('invalid explicit selection ran code')")
            assert result["isError"], result
            assert "python configuration validation failed" in last_result_text(
                client
            ), result
            client.finish()


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_idle_preparation_preserves_peer_runtime(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = dict(os.environ)
        environment.pop("RETICULATE_PYTHON", None)
        arguments = root / "uv-arguments"
        environment.update(
            RETICULATE_UV=str(
                Path(__file__).parents[3] / "fixtures/record_uv_environment"
            ),
            MCP_CONSOLE_TEST_REAL_UV=shutil.which("uv"),
            MCP_CONSOLE_TEST_UV_RECORD=str(root / "uv-environment"),
            MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD=str(arguments),
        )
        serve = (
            execution.serve("--writable-root", str(root))
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(binary, serve, environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import sys; from pathlib import Path; sentinel = object(); original = sentinel; _ = Path('running-python').write_text(sys.executable)"
            )
            assert last_result_text(client) == "[done]", client.transcript[-1]
            arguments.write_text("")
            client.send(requirements={"python": ["packaging"]})
            assert last_result_text(client) == "[prepared]", client.transcript[-1]
            calls = [json.loads(line) for line in arguments.read_text().splitlines()]
            resolution = next(call for call in calls if call[:2] == ["tool", "run"])
            assert (
                resolution[resolution.index("--python") + 1]
                == (root / "running-python").read_text()
            ), resolution
            assert not any(call[:2] == ["python", "list"] for call in calls), calls
            arguments.write_text("")
            rejected = client.send(requirements={"python": ["NumPy>=0"]})
            assert rejected["isError"], rejected
            assert "changes already-declared `numpy`" in last_result_text(client), (
                client.transcript[-1]
            )
            assert arguments.read_text() == "", (
                "incompatible live declaration reached resolver"
            )
            # fmt: python
            source = code("""
                import ctypes
                import packaging

                try:
                    initialized = bool(ctypes.c_void_p.in_dll(ctypes.CDLL(None), "R_GlobalEnv").value)
                except ValueError:
                    initialized = False
                assert initialized, "R did not initialize during warmup"
                assert sentinel is original
                print("idle preparation uses the Python owner")
                """)
            client.send(python=source)
            assert (
                last_result_text(client) == "idle preparation uses the Python owner\n"
            ), client.transcript[-1]
            client.send(
                r='stopifnot("packaging" %in% reticulate::py_require()$packages, isTRUE(reticulate::py_config()$ephemeral))'
            )
            assert last_result_text(client) == "[done]", client.transcript[-1]
            return client.finish()[3:]


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


def exercise_python(client: McpClient, *, first_evaluation: int = 1) -> tuple[str, ...]:
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
    assert f"<mcp-console:python:e{first_evaluation + 1}>" in output[1], output[1]
    assert output[1].endswith("ValueError: peer runtime sentinel\n"), output[1]
    assert output[2:] == ["(42, True, True)\n", "42\n", "43\n"], output[2:]
    return tuple(output)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_python_contract_with_and_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    # The host must have R so the same test can compare both configurations.
    # First-cell language does not change the bootstrap or evaluator.
    reference = None
    records = None
    for mode in ("without-r", "r-cell-first", "python-cell-first"):
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
                if mode == "r-cell-first":
                    # Both peers are already initialized before the first cell.
                    client.send(
                        r="stopifnot(reticulate::py_available(initialize = FALSE))"
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
        other = root / "other environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(other)], check=True
        )
        assert executable.samefile(other / "bin/python")
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
        for mode in (
            "without-r",
            "python-cell-first",
            "python-cell-first-relative",
            "python-cell-first-command",
            "r-cell-first",
        ):
            workspace = root if mode == "python-cell-first-relative" else root / mode
            if workspace != root:
                workspace.mkdir()
            (workspace / "after-startup").mkdir()
            selection = (
                os.path.relpath(executable, workspace)
                if mode == "python-cell-first-relative"
                else str(executable)
            )
            environment = dict(
                os.environ,
                RETICULATE_PYTHON=selection,
                MCP_CONSOLE_TEST_PYTHON=str(workspace.resolve() / selection),
                MCP_CONSOLE_TEST_PYTHON_PREFIX=str(venv),
                MCP_CONSOLE_TEST_OTHER_PYTHON=str(other / "bin/python"),
                PYTHONPATH=str(root / "unselected-modules"),
                RETICULATE_PYTHONPATH=str(modules),
            )
            if mode == "python-cell-first-command":
                environment["RETICULATE_PYTHON"] = "python"
                environment["PATH"] = os.pathsep.join(
                    (str(executable.parent), environment["PATH"])
                )
            if mode == "without-r":
                without_r(environment, workspace)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-cell-first":
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
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix, (os.environ["VIRTUAL_ENV"], sys.prefix)
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
                    client.send(
                        python="peer_object = object(); peer_id = id(peer_object); os.chdir('after-startup')"
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    client.send(r=CLI_CHECK)
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    if mode == "python-cell-first-relative":
                        # A selection request cannot replace the initialized
                        # interpreter, including a different virtualenv alias.
                        client.send(
                            r=code("""
                            original <- Sys.getenv("RETICULATE_PYTHON")
                            for (selection in c("/incompatible-python", "managed", Sys.getenv("MCP_CONSOLE_TEST_OTHER_PYTHON"))) {
                              Sys.setenv(RETICULATE_PYTHON = selection)
                              failure <- tryCatch(reticulate::use_python(selection, required = TRUE), error = conditionMessage)
                              stopifnot(identical(failure, "Python is already initialized with another selection; restart required"))
                            }
                            Sys.setenv(RETICULATE_PYTHON = original)
                            """)
                        )
                        assert last_result_text(client) == "[done]", client.transcript[
                            -1
                        ]
                    client.send(
                        r=code("""
                        requested <- Sys.getenv("MCP_CONSOLE_TEST_OTHER_PYTHON")
                        failure <- tryCatch(
                          reticulate::use_python(requested, required = TRUE),
                          error = conditionMessage
                        )
                        stopifnot(identical(failure, "Python is already initialized with another selection; restart required"))
                        # Executable aliases within the selected environment remain valid.
                        suppressWarnings(reticulate::use_python(
                          file.path(dirname(Sys.getenv("MCP_CONSOLE_TEST_PYTHON")), "python3"),
                          required = TRUE
                        ))
                        """)
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    client.send(r='check_cli("peer-cli", "mcp_console_test_cli")')
                    assert (
                        last_result_text(client)
                        == "installed CLI uses the selected Python environment\n"
                    ), client.transcript[-1]
                    # fmt: r
                    client.send(
                        r=code("""
                        actual <- reticulate::py_config()$virtualenv_activate
                        expected <- file.path(
                          dirname(Sys.getenv("MCP_CONSOLE_TEST_PYTHON")),
                          "activate_this.py"
                        )
                        stopifnot(identical(
                          normalizePath(actual, mustWork = TRUE),
                          normalizePath(expected, mustWork = TRUE)
                        ))
                        """)
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    client.send(
                        python="assert id(peer_object) == peer_id; assert builtins.peer_bootstrap_count == 1"
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                    client.finish()
        assert records is not None
        return records


@requires(R, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_first_r_cell_observes_initialized_python(
    binary: Path, execution: Execution
) -> Transcript:
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
                stopifnot(python_initialized() == 1L)
                stopifnot(reticulate::py_available(initialize = FALSE))
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
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
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
        resolver_environment, started, release = checkpoint_uv_environment(
            remote, "numpy"
        )
        resources.callback(started.close)
        resources.callback(release.close)
        environment.update(
            (name, value)
            for name, value in resolver_environment.items()
            if name == "RETICULATE_UV" or name.startswith("MCP_CONSOLE_TEST_")
        )
        configure(local, remote, remote_command(remote, binary, environment))
        with localhost(root / "sshd") as controller:
            trap = poison_controller(root / "sshd", controller)

            class ReleaseResolverAfterPoll(McpClient):
                def send(self, **arguments: object) -> ToolResult:
                    result = super().send(**arguments)
                    if arguments == {"timeout_ms": 0}:
                        assert last_result_text(self) == (
                            "\n[running; poll with an empty send]"
                        ), result
                        release.release()
                    return result

            with ReleaseResolverAfterPoll(
                binary, execution.serve(), controller, local
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    python="import sys; peer_object = object(); peer_id = id(peer_object)",
                    timeout_ms=0,
                )
                assert last_result_text(client) == (
                    "\n[running; poll with an empty send]"
                ), client.transcript[-1]
                # Release only after the collector observes an empty running
                # poll, so completion must survive that earlier empty cut.
                collected = send_and_collect_runtime_python_resolution(
                    client, timeout_ms=0
                )
                assert collected == "[done]", repr(collected)
                started.wait("remote managed Python resolver")
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
    for mode in ("without-r", "python-cell-first", "r-cell-first"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ)
            environment.pop("RETICULATE_PYTHON", None)
            if mode == "python-cell-first":
                environment["RETICULATE_PYTHON"] = "managed"
            with_r = mode != "without-r"
            if not with_r:
                uv = shutil.which("uv")
                assert uv is not None
                without_r(environment, root)
                (Path(environment["PATH"]) / "uv").symlink_to(uv)
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                if mode == "r-cell-first":
                    client.send(
                        r="stopifnot(reticulate::py_available(initialize = FALSE))"
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(requirements={"python": ["py-yaml12"]})
                assert not client.transcript[-1]["result"].get("isError"), (
                    client.transcript[-1]
                )
                defaults = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]["python"]
                assert "numpy" in defaults, defaults
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
                    child_identity = json.loads(
                        subprocess.check_output([sys.executable, "-c", program], text=True)
                    )
                    assert child_identity == expected, (child_identity, expected)
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix, (os.environ["VIRTUAL_ENV"], sys.prefix)
                    print("managed identity and child environment agree")
                    """)
                client.send(python=source)
                assert (
                    last_result_text(client)
                    == "managed identity and child environment agree\n"
                ), client.transcript[-1]
                if with_r:
                    client.send(
                        r="stopifnot(isTRUE(reticulate::py_config()$ephemeral))"
                    )
                    assert last_result_text(client) == "[done]", client.transcript[-1]
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
def test_managed_import_after_r_startup_hook(
    binary: Path, execution: Execution
) -> Transcript:
    return attach_python_initialized_during_r_startup(binary, execution, managed=True)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_startup_uses_planned_python(
    binary: Path, execution: Execution
) -> Transcript:
    return attach_python_initialized_during_r_startup(
        binary, execution, managed=False, python_first=True
    )


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_startup_captures_package_plots(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for trigger in ("r-cell", "python-access"):
        records.extend(
            attach_python_initialized_during_r_startup(
                binary,
                execution,
                managed=False,
                python_first=True,
                startup_plots=True,
                trigger=trigger,
            )
        )
    return records


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_system_default_packages_survive_startup(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for python_first in (False, True):
        records += attach_python_initialized_during_r_startup(
            binary,
            execution,
            managed=False,
            python_first=python_first,
            system_default_packages=True,
        )
    return records


def attach_python_initialized_during_r_startup(
    binary: Path,
    execution: Execution,
    *,
    managed: bool,
    python_first: bool = False,
    startup_plots: bool = False,
    system_default_packages: bool = False,
    trigger: str = "r-cell",
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        library = root / "library"
        library.mkdir()
        environment, rscript = r_test_environment()
        expected_plots = (
            reference_plots(
                rscript,
                environment,
                "graphics::plot(1:3); graphics::plot(3:1)\n",
                width=800 / 96,
                height=600 / 96,
                dpi=96,
                pages=2,
            )
            if startup_plots
            else []
        )
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
            MCP_CONSOLE_TEST_STARTUP_PLOTS="1" if startup_plots else "0",
        )
        if system_default_packages:
            home = isolated_r_home(root, environment)
            with (home / "etc/Renviron").open("a") as stream:
                stream.write(
                    "\nR_DEFAULT_PACKAGES=" + environment["R_DEFAULT_PACKAGES"] + "\n"
                )
            environment["R_DEFAULT_PACKAGES"] = "NULL"
        if managed:
            environment.pop("RETICULATE_PYTHON")
            # R startup hooks use the managed launch selection and common
            # native bootstrap; later imports retain the same requirement owner.
            version = "==" + ".".join(map(str, sys.version_info[:3]))
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            if managed:
                client.send(requirements={"python_version": [version]})
                assert last_result_text(client) == "[prepared]", client.transcript[-1]
            if python_first:
                client.send(
                    python="before_r = object(); before_r_identity = id(before_r)"
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
            # The default package can enter the common owner during startup.
            if trigger == "python-access":
                client.send(python="assert 3 < r.pi < 4")
            else:
                client.send(r="invisible(NULL)")
            if startup_plots:
                assert_result_content(client, expected_plots)
            else:
                assert last_result_text(client) == "[done]", client.transcript[-1]
            # fmt: r
            client.send(
                r=code("""
                stopifnot(
                  "mcpconsoleearlypython" %in% getOption("defaultPackages"),
                  identical(search()[[2L]], "tools:mcp-console"),
                  identical(find("py")[[1L]], "tools:mcp-console"),
                  identical(find("sql_connection")[[1L]], "tools:mcp-console"),
                  identical(find("console_sql_connection")[[1L]], "tools:mcp-console")
                )
                """)
            )
            assert last_result_text(client) == "[done]", client.transcript[-1]
            client.send(
                r=f"stopifnot(isTRUE(reticulate::py_config()$ephemeral) == {str(managed).upper()})"
            )
            assert last_result_text(client) == "[done]", client.transcript[-1]
            if python_first:
                client.send(
                    python="assert id(before_r) == before_r_identity; assert early_executable == sys.executable; print('startup package attached to running Python')"
                )
                assert (
                    last_result_text(client)
                    == "startup package attached to running Python\n"
                ), client.transcript[-1]
                client.send(
                    r=code("""
                    incompatible <- tryCatch(
                        reticulate::use_python("/incompatible-python", required = TRUE),
                        error = conditionMessage
                    )
                    stopifnot(identical(incompatible, "Python is already initialized with another selection; restart required"))
                    """)
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
            if not managed:
                exercise_python(
                    client,
                    first_evaluation=(3 if python_first else 1)
                    + int(trigger == "python-access"),
                )
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
                    python="import yaml12; assert id(early_object) == early_identity; print('startup interpreter resolved import')"
                )
                assert last_result_text(client) == (
                    "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
                    "startup interpreter resolved import\n"
                ), client.transcript[-1]
                accepted = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert "py-yaml12" in accepted["python"], accepted
                assert accepted["python_version"] == [version], accepted
                client.send(control="restart")
                # The next startup plan retains the accepted declaration.
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
