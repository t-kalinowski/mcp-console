#!/usr/bin/env -S uv run --script

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.processes import host_process_id, process_exists
from support.suites import run_this_suite


def managed_environments(root: Path, *, interrupt_site: bool = False) -> dict[str, str]:
    for name in ("initial", "candidate"):
        environment = root / name
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(environment)],
            check=True,
            capture_output=True,
        )
        site = Path(
            subprocess.check_output(
                [
                    environment / "bin/python",
                    "-c",
                    "import site; print(site.getsitepackages()[0])",
                ],
                text=True,
            ).strip()
        )
        extra = root / f"{name}-external"
        extra.mkdir()
        (site / "console-path.pth").write_text(str(extra) + "\n")
        (extra / "console_unloaded.py").write_text(
            # fmt: python
            code(f"""
                origin = {name!r}
                """)
        )
        if name == "initial":
            (extra / "console_retired.py").touch()
        else:
            metadata = site / "console_activation_fixture-1.0.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text(
                "Name: console-activation-fixture\nVersion: 1.0\n"
            )
            (metadata / "top_level.txt").write_text("console_unloaded\n")
            (site / "console-identity.pth").write_text("import console_identity\n")
            (site / "console_identity.py").write_text(
                # fmt: python
                code("""
                    import sys

                    identity = sys.prefix, sys.exec_prefix, sys.executable
                    if sys.flags.no_site:
                        import atexit

                        print("candidate startup output")
                        atexit.register(print, "candidate exit output")
                    """)
            )
        if name == "candidate" and interrupt_site:
            (site / "console-interrupt.pth").write_text("import console_interrupt\n")
            (site / "console_interrupt.py").write_text(
                # fmt: python
                code("""
                    import builtins
                    import signal
                    import sys

                    if sys.argv[0] != "-c" and not getattr(builtins, "site_interrupted", False):
                        builtins.site_interrupted = True
                        signal.raise_signal(signal.SIGINT)
                    """)
            )
    uv = root / "uv"
    # Use test-owned environments with real site processing and discovery.
    # Preserve uv's version discovery and execute its environment probe unchanged.
    real_uv = shutil.which("uv")
    assert real_uv is not None, "uv is required"
    # fmt: python
    source = code(f"""
        import os
        import sys

        if sys.argv[1:3] == ["tool", "run"]:
            name = "candidate" if "console-activation-fixture" in sys.argv else "initial"
            python = os.path.join({str(root)!r}, name, "bin", "python")
            command = sys.argv[sys.argv.index("--") + 2:]
            os.execv(python, [python, *command])
        uv = {real_uv!r}
        os.execv(uv, [uv, *sys.argv[1:]])
        """)
    uv.write_text(f"#!{sys.executable}\n" + source)
    uv.chmod(0o755)
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["RETICULATE_UV"] = str(uv)
    environment["RETICULATE_CHECK_REQUIRED_PACKAGES"] = "true"
    return environment


def initialize_managed_client(client: McpClient) -> None:
    client.initialize_and_list_tools()
    # Match the manifest to these standard-library and fixture-module environments
    # before startup; they do not provide the normal NumPy/pandas seed.
    client.send(
        # fmt: r
        r=code("""
            reticulate::py_require(character(), action = "set")
            """)
    )
    assert last_result_text(client) == "[done]", last_result_text(client)


@executions(DIRECT, SANDBOXED)
def test_removes_previous_environment_pth_paths(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sys

                    original_prefix = sys.prefix
                    original_paths = {path for path in sys.path if path.startswith(original_prefix)}
                    sys.path.insert(0, "user-added-path")
                    importlib.util.find_spec("console_retired") is not None
                    """)
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_unloaded

                    assert "user-added-path" in sys.path
                    assert not original_paths.intersection(sys.path)
                    assert importlib.util.find_spec("console_retired") is None
                    console_unloaded.origin
                    """)
            )
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_site_hooks_observe_candidate_identity(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import sys

                    original_identity = sys.prefix, sys.exec_prefix, sys.executable
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_identity

                    assert console_identity.identity != original_identity
                    assert console_identity.identity == (sys.prefix, sys.exec_prefix, sys.executable)
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rolls_back_interrupted_python_site_activation(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root, interrupt_site=True)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys

                    original_environment = dict(os.environ)
                    original_paths = list(sys.path)
                    original_prefixes = sys.prefix, sys.exec_prefix, sys.executable
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            result = client.send(
                requirements={"python": ["console-activation-fixture"]}
            )
            assert result["isError"] is True, result
            assert "KeyboardInterrupt" in last_result_text(client), last_result_text(
                client
            )
            client.send(
                # fmt: python
                python=code("""
                    assert dict(os.environ) == original_environment
                    assert sys.path == original_paths
                    assert (sys.prefix, sys.exec_prefix, sys.executable) == original_prefixes
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(
                r='"console-activation-fixture" %in% reticulate::py_require()$packages'
            )
            assert last_result_text(client) == "[1] FALSE\n", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_unloaded

                    console_unloaded.origin
                    """)
            )
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_publication_after_committing_python(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import signal
                    import sys

                    old_prefix = sys.prefix
                    marker = object()
                    marker_id = id(marker)


                    def interrupt_publication(frame, event, function):
                        if (
                            event == "c_call"
                            and getattr(function, "__name__", None) == "publish_python_activation"
                        ):
                            sys.setprofile(None)
                            assert sys.prefix != old_prefix
                            signal.raise_signal(signal.SIGINT)


                    sys.setprofile(interrupt_publication)
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            result = client.send(
                requirements={"python": ["console-activation-fixture"]}
            )
            assert result["isError"] is True, result
            assert last_result_text(client) == "KeyboardInterrupt", last_result_text(
                client
            )
            client.send(python="sys.prefix != old_prefix, id(marker) == marker_id")
            assert last_result_text(client) == "(True, True)\n", last_result_text(
                client
            )
            client.send(control="restart")
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_suspended_r_interrupt_during_publication(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import signal
                    import _mcp_console_services as services

                    original_publish = services.publish_python_activation


                    def interrupted_publish(activation):
                        signal.raise_signal(signal.SIGINT)
                        original_publish(activation)


                    services.publish_python_activation = interrupted_publish
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(
                # fmt: r
                r=code(r"""
                    tryCatch(
                      {
                        suspendInterrupts({
                          reticulate::py_require("console-activation-fixture")
                          cat("activation returned\n")
                        })
                        Sys.sleep(0) # Check R interrupts while the handler is installed.
                      },
                      interrupt = function(condition) cat("R accepted deferred interrupt\n")
                    )
                    """)
            )
            assert last_result_text(client) == (
                "activation returned\nR accepted deferred interrupt\n"
            ), last_result_text(client)
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            client.send(control="restart")
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_replacement_of_loaded_distribution(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        for name, version in (("initial", "1.0"), ("candidate", "2.0")):
            extra = root / f"{name}-external"
            metadata = extra / f"console_loaded-{version}.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text(
                f"Name: console-loaded\nVersion: {version}\n"
            )
            (metadata / "top_level.txt").write_text("console_loaded\n")
            (extra / "console_loaded.py").write_text("value = object()\n")
        with McpClient(binary, execution.serve(), environment, root) as client:
            initialize_managed_client(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_loaded
                    import os
                    import sys

                    marker = console_loaded.value
                    identity = sys.prefix, sys.exec_prefix, sys.executable, list(sys.path)
                    process_environment = dict(os.environ)
                    worker_pid = os.getpid()
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            result = client.send(
                python="cell_was_run = True",
                requirements={"python": ["console-activation-fixture"]},
            )
            assert result["isError"] is True, result
            assert last_result_text(client) == (
                "Cannot replace loaded console-loaded 1.0 with 2.0. "
                "Restart with compatible requirements; the running interpreter and objects are unchanged."
            ), last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    assert identity == (sys.prefix, sys.exec_prefix, sys.executable, sys.path)
                    assert process_environment == dict(os.environ)
                    assert console_loaded.value is marker and os.getpid() == worker_pid
                    assert "cell_was_run" not in globals()
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(control="restart")
            client.send(python="import console_unloaded; console_unloaded.origin")
            assert last_result_text(client) == "'initial'\n", last_result_text(client)
            return client.finish()


def cancelled_candidate_probe(binary: Path, execution: Execution, control: str) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        environment["TMPDIR"] = str(root)
        checkpoints = []
        try:
            with McpClient(binary, execution.serve(), environment, root) as client:
                initialize_managed_client(client)
                client.send(
                    # fmt: r
                    r=code(r"""
                        cat(
                          tempfile("probe-ready-"),
                          tempfile("probe-release-"),
                          tempfile("probe-pid-"),
                          sep = "\n"
                        )
                        """)
                )
                paths = last_result_text(client).splitlines()
                assert len(paths) == 3, paths
                ready, release = [
                    FifoCheckpoint.create(Path(path)) for path in paths[:2]
                ]
                checkpoints.extend((ready, release))
                pid_path = Path(paths[2])
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<probe ready>\n<probe release>\n<probe pid>"
                )
                candidate_site = next(
                    (root / "candidate/lib").glob("python*/site-packages")
                )
                (candidate_site / "console-probe.pth").write_text(
                    "import console_probe_gate\n"
                )
                (candidate_site / "console_probe_gate.py").write_text(
                    # fmt: python
                    code(f"""
                        import os
                        import sys
                        from pathlib import Path

                        if sys.flags.no_site:
                            Path({str(pid_path)!r}).write_text(str(os.getpid()))
                            with open({str(ready.path)!r}, "wb", buffering=0) as ready:
                                ready.write(b"1")
                            with open({str(release.path)!r}, "rb", buffering=0) as release:
                                assert release.read(1) == b"1"
                        """)
                )
                client.send(python="marker = object(); marker_id = id(marker)")
                assert last_result_text(client) == "[done]", last_result_text(client)
                preparation = client.start_send(
                    requirements={"python": ["console-activation-fixture"]}
                )
                ready.wait("candidate probe")
                child_pid = host_process_id(
                    int(pid_path.read_text()), client.process.pid
                )
                cancellation = client.start_send(control=control)
                client.receive_many([preparation, cancellation])
                assert preparation["result"]["isError"] is True, preparation
                expected = (
                    "Python preparation cancelled by restart"
                    if control == "restart"
                    else "KeyboardInterrupt"
                )
                assert preparation["result"]["content"] == [
                    {"type": "text", "text": expected}
                ], preparation
                assert not process_exists(child_pid), child_pid
                client.send(
                    python='"marker" not in globals()'
                    if control == "restart"
                    else "id(marker) == marker_id"
                )
                assert last_result_text(client) == "True\n", last_result_text(client)
                (candidate_site / "console-probe.pth").unlink()
                client.send(requirements={"python": ["console-activation-fixture"]})
                assert last_result_text(client) == "[prepared]", last_result_text(
                    client
                )
                return client.finish()
        finally:
            for checkpoint in checkpoints:
                checkpoint.close()


@executions(DIRECT, SANDBOXED)
def test_cancels_candidate_probe_and_reaps_its_child(
    binary: Path, execution: Execution
) -> list:
    return cancelled_candidate_probe(binary, execution, "interrupt")


@executions(DIRECT, SANDBOXED)
def test_restarts_during_candidate_probe(binary: Path, execution: Execution) -> list:
    return cancelled_candidate_probe(binary, execution, "restart")


if __name__ == "__main__":
    run_this_suite(__file__)
