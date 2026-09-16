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
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
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
        extra = site / "extra"
        extra.mkdir()
        (site / "console-path.pth").write_text("extra\n")
        (extra / "console_unloaded.py").write_text(
            # fmt: python
            code(f"""
                origin = {name!r}
                """)
        )
        if name == "initial":
            (extra / "console_retired.py").touch()
        else:
            (site / "console-identity.pth").write_text("import console_identity\n")
            (site / "console_identity.py").write_text(
                # fmt: python
                code("""
                    import sys

                    identity = sys.prefix, sys.exec_prefix, sys.executable
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
    return environment


@executions(DIRECT, SANDBOXED)
def test_removes_previous_environment_pth_paths(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sys

                    original_prefix = sys.prefix
                    original_paths = {
                        path
                        for path in sys.path
                        if path.startswith(original_prefix) and "site-packages" in path
                    }
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
            client.initialize_and_list_tools()
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
def test_retains_loaded_distributions_on_user_paths(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        user_packages = root / "user-packages"
        metadata = user_packages / "console_user_package-1.0.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text("Name: console-user-package\nVersion: 1.0\n")
        (metadata / "top_level.txt").write_text("console_user_package\n")
        (user_packages / "console_user_package.py").write_text(
            # fmt: python
            code("""
                value = object()
                """)
        )
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import sys

                    sys.path.insert(0, "user-packages")
                    import console_user_package

                    original_value = console_user_package.value
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(requirements={"python": ["console-activation-fixture"]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import console_unloaded

                    assert "user-packages" in sys.path
                    assert console_user_package.value is original_value
                    console_unloaded.origin
                    """)
            )
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rolls_back_interrupted_python_site_activation(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root, interrupt_site=True)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import sys

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
                    assert sys.path == original_paths
                    assert (sys.prefix, sys.exec_prefix, sys.executable) == original_prefixes
                    """)
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
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
def test_interrupts_python_activation_commit(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = managed_environments(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import signal
                    import sys

                    original_prefix = sys.prefix
                    original_object = object()


                    def interrupt_activation(frame, event, argument):
                        if event != "call" or frame.f_code.co_name != "dumps":
                            return
                        message = frame.f_locals.get("obj")
                        if isinstance(message, dict) and message.get("operation") == "activate_python":
                            sys.setprofile(None)
                            assert sys.prefix != original_prefix
                            signal.raise_signal(signal.SIGINT)


                    sys.setprofile(interrupt_activation)
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
                python="sys.prefix != original_prefix, original_object is not None"
            )
            assert last_result_text(client) == "(True, True)\n", last_result_text(
                client
            )
            client.send(control="restart")
            client.send(
                # fmt: python
                python=code("""
                    import console_unloaded

                    console_unloaded.origin
                    """)
            )
            assert last_result_text(client) == "'candidate'\n", last_result_text(client)
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
