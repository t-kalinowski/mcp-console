"""Real prepared-image acceptance, shared across Docker and SBX."""

import json
import os
import signal
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.assertions import assert_result_content, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.docker import (
    daemon_available,
    configure as configure_docker,
    absent as docker_absent,
    workspace as docker_workspace,
    cli_peer as docker_peer,
    calls as docker_calls,
    docker,
)
from support.docker_sandbox import (
    sbx,
    configure as configure_sbx,
    workspace as sbx_workspace,
    cli_peer as sbx_peer,
    calls as sbx_calls,
    absent as sbx_absent,
)
from support.events import Events
from support.execution import DIRECT, Execution, executions
from support.normalization import code
from support.requirements import PROCESS_EVENTS, WORKER, Requirement, requires
from support.suites import run_this_suite
from boundaries.client_server.server.test_no_r import no_r_environment

DOCKER_PYTHON = Requirement(
    "R-free Docker image",
    bool(os.environ.get("MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE")) and daemon_available(),
    "requires MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE built from examples/docker/Dockerfile.python",
)
SBX_PYTHON = Requirement(
    "R-free SBX template",
    bool(os.environ.get("MCP_CONSOLE_TEST_SBX_PYTHON_TEMPLATE"))
    and WORKER.available
    and shutil.which("sbx") is not None,
    "requires MCP_CONSOLE_TEST_SBX_PYTHON_TEMPLATE built from examples/docker-sandbox/Dockerfile.python",
)


@contextmanager
def prepared(
    binary: Path,
    provider: str,
    *,
    python: str | None = None,
    inherit=True,
    sandbox=False,
    workload=None,
    setup=None,
    languages=None,
    peer_mode=None,
):
    fixture = docker_workspace() if provider == "docker" else sbx_workspace(real=True)
    with fixture as root:
        if provider == "docker":
            configure_docker(root, os.environ["MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE"])
        else:
            configure_sbx(
                root, template=os.environ["MCP_CONSOLE_TEST_SBX_PYTHON_TEMPLATE"]
            )
        config = root / ".agents/console/config.yaml"
        value = json.loads(config.read_text())
        if python is not None:
            value["python"] = python
        value["sandbox"]["inherit_environment"] = inherit
        value["sandbox"]["environment"].update(
            PATH="/opt/console-python/bin:/usr/local/bin:/usr/bin:/bin",
            HOME="/root" if provider == "docker" else "/home/agent",
        )
        value["sandbox"]["environment"].update(workload or {})
        # These commands exist on the target PATH but must never be invoked.
        # Both providers use a real shared path; only their CLI observation is wrapped.
        target_programs = root / "target-programs"
        target_programs.mkdir()
        for name in (
            "uv",
            "ir",
            *(("mcp-console-sandbox",) if provider == "sbx" else ()),
        ):
            program = target_programs / name
            program.write_text(
                f"#!/bin/sh\necho {name} >> '{root / 'target-invoked'}'\nexit 99\n"
            )
            program.chmod(0o755)
        value["target"]["compute"]["mounts"].append(
            {"source": str(root), "target": str(root), "access": "read_write"}
        )
        value["sandbox"]["environment"]["PATH"] = (
            str(target_programs) + ":" + value["sandbox"]["environment"]["PATH"]
        )
        if setup is not None:
            setup(root, value)
        config.write_text(json.dumps(value))
        forbidden = root / "controller-programs"
        forbidden.mkdir()
        for name in ("R", "Rscript", "python", "python3", "uv", "ir"):
            program = forbidden / name
            program.write_text(
                f"#!/bin/sh\necho {name} >> '{root / 'invoked'}'\nexit 99\n"
            )
            program.chmod(0o755)
        peer = (
            docker_peer(root / "peer")
            if provider == "docker"
            else sbx_peer(root / "peer", real=True)
        )
        if peer_mode is not None:
            (root / "peer/mode").write_text(peer_mode)
            (root / "peer/reached").symlink_to(root / "gate")
        env = dict(
            peer,
            PATH=str(forbidden) + os.pathsep + peer["PATH"],
            R_HOME="/controller-r-must-not-be-used",
            RETICULATE_PYTHON="/controller-python-must-not-be-used",
        )
        if languages is not None:
            env["MCP_CONSOLE_LANGUAGES"] = languages
        flags = (
            ("serve", "--no-sandbox")
            if provider == "docker" and not sandbox
            else ("serve",)
        )
        try:
            with McpClient(binary, flags, env, root) as client:
                yield client, root
        finally:
            assert not (root / "invoked").exists(), (
                "controller invoked a target runtime or resolver"
            )
            assert not (root / "target-invoked").exists(), (
                "prepared target invoked a dependency resolver"
            )
            operations = docker_calls(root) if provider == "docker" else sbx_calls(root)
            names = {
                call["args"][call["args"].index("--name") + 1]
                for call in operations
                if "create" in call["args"]
            }
            for name in names:
                if provider == "docker":
                    docker_absent(name)
                else:
                    result = sbx("ls", "--json")
                    assert result.returncode == 0, result.stderr
                    assert not any(
                        vm["name"] == name
                        for vm in json.loads(result.stdout)["sandboxes"]
                    ), result.stdout


def sql_first(binary: Path, provider: str) -> list:
    with prepared(binary, provider) as (client, root):
        client.initialize_and_list_tools()
        tool = client.transcript[-1]["result"]["tools"][0]
        fields = tool["inputSchema"]["properties"]
        assert {"r", "python", "sql"} <= fields.keys()
        assert fields["requirements"]["properties"]["action"]["enum"] == ["get"]
        assert "Console-owned" in tool["description"]
        assert "preinstalled" in tool["description"]
        client.send(requirements={"action": "get"})
        snapshot = client.transcript[-1]["result"]["structuredContent"]
        assert snapshot["requirements"]["python"] == []
        client.send(
            sql="CREATE TABLE retained AS SELECT 42 AS answer; SELECT * FROM retained"
        )
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(
            # fmt: python
            python=code("""
                import os, sys, shutil, subprocess

                assert shutil.which("R") is None
                assert shutil.which("Rscript") is None
                assert not os.environ.get("R_HOME")
                assert sys.executable == "/opt/console-python/bin/python3"
                assert sys.prefix == "/opt/console-python"
                assert (
                    subprocess.check_output(
                        [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                    ).strip()
                    == sys.prefix
                )
                retained = 41
                print(retained + 1)
                """)
        )
        assert last_result_text(client) == "42\n", last_result_text(client)
        client.send(sql="SELECT * FROM retained")
        assert "42" in last_result_text(client)
        client.send(control="restart")
        client.send(python='print("retained" in globals())')
        assert last_result_text(client) == "False\n"
        client.send(sql="SHOW TABLES")
        assert "retained" not in last_result_text(client), last_result_text(client)
        session = next(root.glob(".agents/console/sessions/*"))
        quarto = (session / "transcript.qmd").read_text()
        assert "ir render" not in quarto, quarto
        assert "Execute these cells in the preinstalled target environment" in quarto, (
            quarto
        )
        assert "execute:\n  eval: false" not in quarto, quarto
        assert "\nknitr:" not in quarto and "\nir:" not in quarto, quarto
        assert "Files and environments remain remote" in quarto, quarto
        result, diagnostics = client.finish_with_standard_error()
        return result[3:]


def behavior(binary: Path, provider: str, *, sandbox=False) -> list:
    with prepared(binary, provider, sandbox=sandbox) as (client, root):
        client.initialize_and_list_tools()
        client.send(
            # fmt: python
            python=code("""
                import os, sys, subprocess, tempfile
                from pathlib import Path

                assert not Path("/usr/lib/R").exists()
                assert not Path("/opt/console-python/lib/python3.12/site-packages/reticulate").exists()
                assert os.environ["MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION"] == "0"
                assert os.environ["RETICULATE_USE_MANAGED_VENV"] == "no"
                storage = Path(os.environ["TMPDIR"])
                assert storage.is_dir()
                assert any(
                    path.stat().st_mode & 0o077 == 0
                    for path in (storage, *storage.parents)
                    if path != Path("/")
                )
                child = subprocess.check_output(
                    ["python", "-c", "import sys; print(sys.executable); print(sys.prefix)"], text=True
                )
                child_executable, child_prefix = child.splitlines()
                assert Path(child_executable).samefile(sys.executable)
                assert child_prefix == sys.prefix
                retained = 41
                catalog = sql_connection()
                for setting in ("secret_directory", "temp_directory"):
                    selected = Path(
                        catalog.execute("SELECT current_setting(?)", [setting]).fetchone()[0]
                    )
                    assert selected.is_relative_to(storage)
                catalog.execute("CREATE TABLE retained AS SELECT 42 AS answer")
                import sqlite3

                custom = sqlite3.connect(":memory:")
                console_sql_connection(custom)
                print(retained + 1)
                """)
        )
        assert last_result_text(client) == "42\n", last_result_text(client)
        client.send(sql="CREATE TABLE custom_state AS SELECT 7 AS value")
        client.send(sql="SELECT * FROM custom_state")
        assert "7" in last_result_text(client)
        client.send(python="console_sql_connection(None)")
        client.send(sql="SELECT * FROM retained")
        assert "42" in last_result_text(client)
        client.send(
            sql="SELECT current_setting('autoinstall_known_extensions') AS auto_install"
        )
        assert "false" in last_result_text(client).lower(), last_result_text(client)
        client.send(sql="LOAD fts")
        assert not client.transcript[-1]["result"].get("isError")
        assert "Error:" not in last_result_text(client), last_result_text(client)
        client.send(
            python="import matplotlib.pyplot as plt; _ = plt.plot([1, 2], [3, 4]);"
        )
        session = next(root.glob(".agents/console/sessions/*"))
        artifact = next((session / "artifacts").iterdir())
        assert_result_content(
            client, [artifact.read_bytes()], image_reference="controller plot"
        )
        client.send(python='print(input("prepared prompt: "))')
        assert "[waiting for stdin]" in last_result_text(client)
        wait_for_evaluation_output(
            client, "input value\n", "prepared input", stdin="input value\n"
        )
        wait_for_evaluation_output(
            client,
            "interrupt gate\n\n[running; poll with an empty send]",
            "prepared loop",
            python='import time; print("interrupt gate", flush=True); time.sleep(3600)',
            timeout_ms=1,
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_result_text(client), last_result_text(client)
        # pdb installs its own SIGINT behavior after continue. Exercise an
        # ordinary interrupt first, preserving that interpreter behavior.
        client.send(python='import pdb; pdb.set_trace(); print("debugger resumed")')
        assert "[waiting for stdin]" in last_result_text(client)
        wait_for_evaluation_output(
            client, "debugger resumed\n", "prepared debugger", stdin="continue\n"
        )
        client.send(python="print(retained)")
        assert last_result_text(client) == "41\n"
        for mutation in (
            {"action": "add", "python": ["six"]},
            {"action": "set", "python": ["six"]},
            {"action": "reset"},
        ):
            client.send(
                control="restart",
                stdin="must not queue\n",
                python="retained = -1",
                requirements=mutation,
            )
            assert client.transcript[-1]["result"].get("isError")
            assert "dynamic environment resolution is disabled" in last_result_text(
                client
            )
        client.send(python="print(retained)")
        assert last_result_text(client) == "41\n"
        client.send(python='print(input("after rejected mutation: "))')
        assert "[waiting for stdin]" in last_result_text(client)
        wait_for_evaluation_output(
            client,
            "accepted input\n",
            "rejected input was not delivered",
            stdin="accepted input\n",
        )
        client.send(python="import mcp_console_missing_prepared_distribution")
        assert "ModuleNotFoundError" in last_result_text(client)
        client.send(python="import os; os._exit(17)")
        assert "17" in last_result_text(client), last_result_text(client)
        client.send(python='print("retained" in globals())')
        assert last_result_text(client).endswith("False\n"), last_result_text(client)
        client.send(sql="SHOW TABLES")
        assert "retained" not in last_result_text(client)
        config = root / ".agents/console/config.yaml"
        config.write_text("invalid: [")
        client.send(control="restart")
        client.send(python="import sys; print(sys.executable)")
        assert last_result_text(client) == "/opt/console-python/bin/python3\n"
        journal = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        assert journal[0]["target"]["runtime"]["kind"] == "python"
        assert journal[0]["target"]["runtime"]["managed"] is False
        assert (
            len([event for event in journal if event["event"] == "target_generation"])
            == 3
        )
        records, diagnostics = client.finish_with_standard_error()
        return json.loads(json.dumps(records[3:]).replace(str(root), "<prepared-test>"))


def selected_and_minimal(binary: Path, provider: str) -> list:
    records = []
    for selected in (
        "../opt/console-python/bin/python",
        "../opt/console-minimal/bin/python",
    ):
        with prepared(
            binary,
            provider,
            python=selected,
            inherit=False,
            workload={
                "RETICULATE_PYTHON": "/invalid-legacy-selection",
                "PYTHONHOME": "/invalid-python-home",
                "PYTHONPLATLIBDIR": "invalid-layout",
            },
        ) as (client, root):
            client.initialize_and_list_tools()
            expected = "/workspace/" + selected
            client.send(
                # fmt: python
                python=code(f"""
                    import sys, os, subprocess
                    assert sys.executable == {expected!r}
                    assert 'PYTHONHOME' not in os.environ and 'PYTHONPLATLIBDIR' not in os.environ
                    assert subprocess.check_output([sys.executable, '-c', 'import sys; print(sys.prefix)'], text=True).strip() == sys.prefix
                    print('selected interpreter retained')
                    """)
            )
            assert last_result_text(client) == "selected interpreter retained\n", (
                last_result_text(client)
            )
            if "minimal" in selected:
                client.send(sql="SELECT 42 AS answer")
                assert "DuckDB is unavailable" in last_result_text(client), (
                    last_result_text(client)
                )
                source = "image" if provider == "docker" else "template"
                assert f"prepared {source}" in last_result_text(client), (
                    last_result_text(client)
                )
                client.send(
                    python="import sqlite3; selected_connection = sqlite3.connect(':memory:'); console_sql_connection(selected_connection)"
                )
                client.send(sql="SELECT 42 AS answer")
                assert "42" in last_result_text(client)
                client.send(python="import pandas")
                assert "ModuleNotFoundError" in last_result_text(client)
                assert source in last_result_text(client), last_result_text(client)
                client.send(python="print(41 + 1)")
                assert last_result_text(client) == "42\n"
            records.extend(client.finish_with_standard_error()[0][3:])
    return records


def probe_setup(root: Path, value: dict, mode: str) -> None:
    """Install a real CPython startup hook in the disposable execution resource."""
    hook = root / "probe-hook.py"
    hook.write_text(
        code(f"""
        import builtins, json, os, sys, sysconfig
        from pathlib import Path

        root = Path({str(root)!r})
        inspecting = sys.flags.isolated and sys.argv[0] == "-c"
        if inspecting:
            storage = Path(os.environ["TMPDIR"])
            assert storage.stat().st_mode & 0o777 == 0o700
            assert "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ
            (root / "probe-observed").write_text(json.dumps({{"storage": str(storage)}}))
            original_import = builtins.__import__
            def without_analysis(name, *args, **kwargs):
                assert name.split(".")[0] not in ("duckdb", "numpy", "pandas", "matplotlib", "rpy2"), name
                return original_import(name, *args, **kwargs)
            builtins.__import__ = without_analysis
            print("arbitrary Python startup stdout must not become protocol data")
            if {mode!r} in ("missing-library", "ephemeral-library", "unusable-library"):
                original = sysconfig.get_config_var
                library = Path(original("LIBDIR")) / original("LDLIBRARY")
                if {mode!r} == "ephemeral-library":
                    import shutil
                    shutil.copyfile(library, storage / library.name)
                elif {mode!r} == "unusable-library":
                    (storage / library.name).write_text("not a shared library\\n")
                def get_config_var(name):
                    return str(storage) if name == "LIBDIR" else original(name)
                sysconfig.get_config_var = get_config_var
        elif "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
            (root / "worker-started").touch()
            if {mode!r} == "startup-failure":
                sys.prefix = "changed-by-installed-startup-hook"
        """)
    )
    entry = root / "prepare-probe.py"
    entry.write_text(
        code(f"""
        import importlib.util, os, shutil, site, sys
        from pathlib import Path

        installed = importlib.util.find_spec("sitecustomize")
        destination = Path(installed.origin) if installed else Path(site.getsitepackages()[0]) / "sitecustomize.py"
        shutil.copyfile({str(hook)!r}, destination)
        os.execv("/usr/local/bin/mcp-console" if Path("/usr/local/bin/mcp-console").exists() else "/opt/console-python/bin/mcp-console", ["mcp-console", *sys.argv[1:]])
        """)
    )
    command = ["/opt/console-python/bin/python3", str(entry)]
    if value["target"]["compute"]["kind"] == "docker_sandbox":
        command = ["/usr/bin/sudo", "-n", "--preserve-env=PATH,HOME", *command]
    value["target"]["command"] = command


def inspection_boundary(binary: Path, provider: str) -> list:
    with prepared(
        binary, provider, setup=lambda root, value: probe_setup(root, value, "noisy")
    ) as (client, root):
        client.initialize_and_list_tools()
        assert (root / "probe-observed").exists(), client._diagnostics()
        assert not (root / "worker-started").exists(), (
            "probe started the analysis worker"
        )
        observed = json.loads((root / "probe-observed").read_text())
        client.send(
            python="import os; from pathlib import Path; print(Path("
            + repr(observed["storage"])
            + ").exists())"
        )
        assert last_result_text(client) == "False\n", (
            "probe storage survived disposable resource retirement"
        )
        assert (root / "worker-started").exists()
        assert "arbitrary Python startup" not in json.dumps(client.transcript)
        client.finish_with_standard_error()
        return [
            {
                "probe_storage_private": True,
                "worker_not_started_by_probe": True,
                "optional_packages_not_imported_by_probe": True,
                "startup_stdout_is_not_protocol_data": True,
            }
        ]


@contextmanager
def unusable_library_client(binary: Path, provider: str):
    if provider == "direct":
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            library = root / "unusable.so"
            library.write_text("not a shared library\n")
            selected = root / "selected-python"
            selected.write_text(
                f"#!{sys.executable}\n"
                + code(f"""
                    import sys, sysconfig

                    original = sysconfig.get_config_var
                    def get_config_var(name):
                        if name in ("LIBDIR", "PYTHONFRAMEWORKPREFIX"):
                            return {str(root)!r}
                        if name == "LDLIBRARY":
                            return "unusable.so"
                        return original(name)
                    sysconfig.get_config_var = get_config_var
                    program = sys.argv.index("-c") + 1
                    source = sys.argv[program]
                    sys.argv = ["-c", *sys.argv[program + 1:]]
                    exec(source)
                    """)
            )
            selected.chmod(0o755)
            environment = no_r_environment(root)
            environment["RETICULATE_PYTHON"] = str(selected)
            with McpClient(binary, DIRECT.serve(), environment, root) as client:
                yield client
    else:
        with prepared(
            binary,
            provider,
            setup=lambda root, value: probe_setup(root, value, "unusable-library"),
        ) as (client, _):
            yield client


@executions(
    DIRECT,
    Execution("docker", (DOCKER_PYTHON,)),
    Execution("sbx", (SBX_PYTHON,)),
)
def test_rejects_unusable_python_library(binary: Path, execution: Execution) -> list:
    with unusable_library_client(binary, execution.name) as client:
        error = client.startup_error()
        assert "selected Python embedding library is unusable" in error, error
        client.stdin.close()
        assert client.stdout.read(timeout=40) == ""
        errors = client.stderr.read(timeout=40)
        assert "selected Python embedding library is unusable" in errors, errors
        assert client.process.wait(timeout=5) != 0
    return [{"unusable_library_rejected_before_worker_startup": True}]


def rejected_probes(binary: Path, provider: str) -> list:
    records = []
    for mode, selected, workload, expected in (
        (
            "missing-executable",
            "/missing-target-interpreter",
            {},
            "selected Python executable",
        ),
        (
            "nonexecutable",
            "/etc/hostname",
            {},
            "failed to inspect selected Python executable",
        ),
        (
            "invalid-r",
            None,
            {"R_HOME": "/missing-target-R"},
            "prepared target R discovery failed",
        ),
        ("broken-r", None, {}, "prepared target R discovery failed"),
        ("broken-python3", None, {}, "selected Python executable"),
        ("missing-library", None, {}, "embedding library is missing"),
        ("ephemeral-library", None, {}, "disposable probe storage"),
    ):

        def setup(root, value):
            if mode in ("missing-library", "ephemeral-library"):
                probe_setup(root, value, mode)
            elif mode in ("broken-r", "broken-python3"):
                programs = root / "target-programs"
                if mode == "broken-r":
                    executable = programs / "R"
                    executable.write_text("#!/bin/sh\nexit 91\n")
                    executable.chmod(0o755)
                else:
                    (programs / "python3").symlink_to("/missing-python3-on-target")

        with prepared(
            binary, provider, python=selected, workload=workload, setup=setup
        ) as (client, root):
            client.startup_error()
            client.stdin.close()
            assert client.stdout.read(timeout=40) == ""
            errors = client.stderr.read(timeout=40)
            assert expected in errors, errors
            assert client.process.wait(timeout=5) != 0
            assert not (root / "worker-started").exists()
            records.append(
                {
                    "mode": mode,
                    "rejected_before_worker_startup": True,
                    "owned_probe_retired": True,
                }
            )
    return records


def bare_selection_and_languages(binary: Path, provider: str) -> list:
    def setup(root, value):
        (root / "selected-python").symlink_to("/opt/console-python/bin/python")
        assert not (root / "selected-python").exists(), (
            "target interpreter unexpectedly exists on controller"
        )
        value["target"]["workspace"] = str(root)

    with prepared(
        binary,
        provider,
        python="selected-python",
        inherit=False,
        setup=setup,
        languages="python",
    ) as (client, root):
        client.initialize_and_list_tools()
        fields = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
            "properties"
        ]
        assert "r" not in fields and "sql" not in fields
        client.send(sql="SELECT 42")
        assert client.transcript[-1]["result"].get("isError")
        for control in ({}, {"control": "restart"}):
            if control:
                client.send(**control)
            client.send(python="import sys; print(sys.executable)")
            assert last_result_text(client) == str(root / "selected-python") + "\n", (
                last_result_text(client)
            )
        client.finish_with_standard_error()
        return [
            {
                "bare_python_path_is_target_workspace_relative": True,
                "language_restrictions_preserved": True,
                "restart_retains_selection": True,
            }
        ]


def startup_failure(binary: Path, provider: str) -> list:
    with prepared(
        binary,
        provider,
        setup=lambda root, value: probe_setup(root, value, "startup-failure"),
    ) as (client, root):
        client.initialize_and_list_tools()
        client.send(python="raise AssertionError('failed startup ran code')")
        result = client.transcript[-1]["result"]
        assert result.get("isError"), result
        assert "embedded Python prefix differs" in last_result_text(client), (
            last_result_text(client)
        )
        assert "failed startup ran code" not in last_result_text(client)
        client.finish_with_standard_error()
        assert (root / "probe-hook.py").exists(), (
            "failed startup deleted shared user files"
        )
        return [
            {
                "startup_failed_before_code": True,
                "owned_resource_retired": True,
                "shared_files_preserved": True,
            }
        ]


def selection_compatibility(binary: Path, provider: str) -> list:
    for legacy in (False, True):

        def setup(root, value):
            programs = root / "target-programs"
            (programs / "python").symlink_to("/opt/console-python/bin/python")
            value["sandbox"]["environment"]["PATH"] = str(programs)
            if legacy:
                value["sandbox"]["environment"]["RETICULATE_PYTHON"] = (
                    "../opt/console-python/bin/python"
                )

        with prepared(binary, provider, inherit=False, setup=setup) as (client, root):
            client.initialize_and_list_tools()
            expected = (
                "/workspace/../opt/console-python/bin/python"
                if legacy
                else str(root / "target-programs/python")
            )
            client.send(python="import sys; print(sys.executable)")
            assert last_result_text(client) == expected + "\n", last_result_text(client)
            client.send(control="restart")
            client.send(python="import sys; print(sys.executable)")
            assert last_result_text(client) == expected + "\n", last_result_text(client)
            client.finish_with_standard_error()
    return [
        {
            "PATH_python_fallback": True,
            "legacy_selection": True,
            "retained_on_restart": True,
        }
    ]


def generation(root: Path) -> dict:
    journal = next(root.glob(".agents/console/sessions/*/internal/events.jsonl"))
    return [
        event
        for line in journal.read_text().splitlines()
        if (event := json.loads(line))["event"] == "target_generation"
    ][-1]


def offline_extension(binary: Path, provider: str) -> list:
    with prepared(binary, provider) as (client, root):
        client.initialize_and_list_tools()
        client.send(
            python="import os; print(os.environ.get('MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY'))"
        )
        assert last_result_text(client) == "None\n"
        current = generation(root)
        if provider == "docker":
            result = docker("network", "disconnect", "bridge", current["container_id"])
            assert result.returncode == 0, result.stderr
        else:
            name = current["sandbox"]["name"]
            result = sbx("policy", "deny", "network", "--sandbox", name, "**")
            assert result.returncode == 0, result.stderr
            policy = sbx(
                "policy",
                "check",
                "network",
                "--sandbox",
                name,
                "--json",
                "extensions.duckdb.org:443",
            )
            assert json.loads(policy.stdout)["allowed"] is False, policy.stdout
        client.send(
            python=code("""
                import urllib.error, urllib.request
                try:
                    urllib.request.urlopen("https://extensions.duckdb.org/", timeout=3).close()
                    raise AssertionError("worker still has outbound network access")
                except urllib.error.URLError:
                    print("worker network blocked")
                """)
        )
        assert last_result_text(client) == "worker network blocked\n", last_result_text(
            client
        )
        client.send(
            sql="LOAD fts; SELECT current_setting('extension_directory') AS image_cache"
        )
        assert not client.transcript[-1]["result"].get("isError")
        assert "Error:" not in last_result_text(client), last_result_text(client)
        if provider == "sbx":
            result = sbx(
                "policy",
                "rm",
                "network",
                "--sandbox",
                name,
                "--resource",
                "**",
                "--force",
            )
            assert result.returncode == 0, result.stderr
        client.finish_with_standard_error()
    return [
        {
            "worker_network_blocked": True,
            "prepared_extension_loaded": True,
            "controller_cache_unused": True,
        }
    ]


def connection_loss(binary: Path, provider: str) -> list:
    for victim in ("attachment", "input-eof"):
        with prepared(binary, provider) as (client, root):
            client.initialize_and_list_tools()
            client.send(
                python="import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], start_new_session=True); retained = 42; print('detached child started')"
            )
            assert last_result_text(client) == "detached child started\n"
            current = generation(root)
            operations = docker_calls(root) if provider == "docker" else sbx_calls(root)
            attachment = [
                call
                for call in operations
                if (
                    "start" in call["args"]
                    if provider == "docker"
                    else call["args"][0] == "exec"
                )
            ][-1]
            with Events() as events:
                events.watch_process(attachment["ppid"])
                if victim == "attachment":
                    os.kill(attachment["pid"], signal.SIGKILL)
                else:
                    client.stdin.close()
                assert attachment["ppid"] in events.wait(20), (
                    "owner did not finish bounded retirement"
                )
            if provider == "docker":
                docker_absent(current["container_id"])
            else:
                sbx_absent(**current["sandbox"])
            if victim == "attachment":
                client.send(control="restart")
                client.send(python="print('retained' in globals())")
                assert last_result_text(client) == "False\n", last_result_text(client)
                client.finish_with_standard_error()
            else:
                client.stdout.read(timeout=10)
                client.stderr.read(timeout=10)
                client.process.wait(timeout=5)
    return [
        {
            "attachment_loss_retired_before_replacement": True,
            "input_eof_retired_detached_child": True,
        }
    ]


def cancelled_probe(binary: Path, provider: str) -> list:
    gate = None

    def setup(root, value):
        nonlocal gate
        gate = FifoCheckpoint.create(root / "gate")

    try:
        with prepared(
            binary,
            provider,
            setup=setup,
            peer_mode="create-gate" if provider == "docker" else "probe-gate",
        ) as (client, root):
            gate.wait("owned resource created before probe completion", timeout=30)
            client.stdin.close()
            assert client.stdout.read(timeout=20) == ""
            errors = client.stderr.read(timeout=20)
            assert "cancel" in errors, errors
            assert client.process.wait(timeout=5) != 0
            assert not (root / "target-invoked").exists()
    finally:
        if gate is not None:
            gate.close()
    return [
        {
            "cancelled_before_readiness": True,
            "owned_probe_retired": True,
            "user_files_preserved": True,
        }
    ]


@requires(DOCKER_PYTHON)
def test_docker_sql_first(binary: Path) -> list:
    return sql_first(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_sql_first(binary: Path) -> list:
    return sql_first(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_python_lifecycle(binary: Path) -> list:
    return behavior(binary, "docker")


@requires(DOCKER_PYTHON)
def test_docker_native_runner_python_lifecycle(binary: Path) -> list:
    return behavior(binary, "docker", sandbox=True)


@requires(SBX_PYTHON)
def test_sbx_python_lifecycle(binary: Path) -> list:
    return behavior(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_selected_and_minimal(binary: Path) -> list:
    return selected_and_minimal(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_selected_and_minimal(binary: Path) -> list:
    return selected_and_minimal(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_inspection_boundary(binary: Path) -> list:
    return inspection_boundary(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_inspection_boundary(binary: Path) -> list:
    return inspection_boundary(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_rejected_probes(binary: Path) -> list:
    return rejected_probes(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_rejected_probes(binary: Path) -> list:
    return rejected_probes(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_bare_selection_and_languages(binary: Path) -> list:
    return bare_selection_and_languages(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_bare_selection_and_languages(binary: Path) -> list:
    return bare_selection_and_languages(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_startup_failure(binary: Path) -> list:
    return startup_failure(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_startup_failure(binary: Path) -> list:
    return startup_failure(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_selection_compatibility(binary: Path) -> list:
    return selection_compatibility(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_selection_compatibility(binary: Path) -> list:
    return selection_compatibility(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_offline_extension(binary: Path) -> list:
    return offline_extension(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_offline_extension(binary: Path) -> list:
    return offline_extension(binary, "sbx")


@requires(DOCKER_PYTHON, PROCESS_EVENTS)
def test_docker_connection_loss(binary: Path) -> list:
    return connection_loss(binary, "docker")


@requires(SBX_PYTHON, PROCESS_EVENTS)
def test_sbx_connection_loss(binary: Path) -> list:
    return connection_loss(binary, "sbx")


@requires(DOCKER_PYTHON)
def test_docker_cancelled_probe(binary: Path) -> list:
    return cancelled_probe(binary, "docker")


@requires(SBX_PYTHON)
def test_sbx_cancelled_probe(binary: Path) -> list:
    return cancelled_probe(binary, "sbx")


if __name__ == "__main__":
    run_this_suite(__file__)
