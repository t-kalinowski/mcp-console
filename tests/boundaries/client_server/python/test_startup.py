#!/usr/bin/env -S uv run --script

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.processes import capture_process_identity, host_process_id, live_processes
from support.requirements import FRAMEWORK_PYTHON, PYTHON_FRAMEWORK, requires
from support.resolvers import bare_runtime_environment
from support.suites import run_this_suite


def selected_python(directory: Path, python: Path) -> dict[str, str]:
    environment = bare_runtime_environment(os.environ.copy(), directory / "r-library")
    environment["RETICULATE_PYTHON"] = str(python)
    return environment


def isolated_python(directory: Path) -> tuple[Path, Path]:
    environment = directory / "python"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
    )
    python = environment / "bin/python"
    site = subprocess.check_output(
        [python, "-c", "import site; print(site.getsitepackages()[0])"], text=True
    ).strip()
    return python, Path(site)


@executions(DIRECT, SANDBOXED)
@requires(PYTHON_FRAMEWORK)
def test_embeds_framework_python(binary: Path, execution: Execution) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = selected_python(root, FRAMEWORK_PYTHON)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    answer = 41
                    answer + 1
                    """)
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_processes_initial_site_directories_once(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        # Executable .pth lines run each time the site directory is processed.
        # fmt: python
        source = code("""
            import builtins; builtins.console_startups = getattr(builtins, "console_startups", 0) + 1
            """)
        (site / "console-startup.pth").write_text(source)
        expected = int(
            subprocess.check_output(
                [python, "-c", "import builtins; print(builtins.console_startups)"],
                text=True,
            )
        )
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            selected_python(root, python),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code(f"""
                    import builtins
                    import sys

                    assert sys.flags.no_site == 0
                    builtins.console_startups == {expected}
                    """)
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            client.send(python=f"builtins.console_startups == {expected}")
            assert last_result_text(client) == "True\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_discovery_accepts_startup_and_exit_output(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        # fmt: python
        source = code("""
            import atexit
            import sys

            if sys.argv[0] == "-c":
                print("startup output without a newline", end="", flush=True)
                atexit.register(print, "discovery exit output")
            """)
        (site / "sitecustomize.py").write_text(source)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            selected_python(root, python),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="6 * 7")
            assert last_result_text(client) == "42\n", last_result_text(client)
            return client.finish()


def interrupted_initialization(
    binary: Path,
    execution: Execution,
    *,
    hook: str = "sitecustomize",
    language: str = "python",
    r_first: bool = False,
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        marker = root / "worker"
        started = FifoCheckpoint.create(root / "started")
        # Discovery runs the hook successfully; only embedded startup blocks.
        # fmt: python
        source = code(f"""
            import builtins
            import os
            import signal
            import sys
            from pathlib import Path

            if sys.argv[0] != "-c":
                builtins.startup_attempts = getattr(builtins, "startup_attempts", 0) + 1
                marker = Path({str(marker)!r})
                if not marker.exists():
                    marker.write_text(str(os.getpid()))
                    with open({str(started.path)!r}, "wb", buffering=0) as ready:
                        ready.write(b"1")
                    while True:
                        signal.pause()
            """)
        (site / f"{hook}.py").write_text(source)
        if hook != "sitecustomize":
            (site / "console-startup.pth").write_text(f"import {hook}\n")
        environment = selected_python(root, python)
        environment["PYTHONNODEBUGRANGES"] = "1"
        try:
            with McpClient(
                binary,
                execution.serve(
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ())
                ),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                if r_first:
                    client.send(r="startup_state <- 41L")
                cell = "never_run = True" if language == "python" else "SELECT 42"
                client.send(**{language: cell}, timeout_ms=0)
                assert last_result_text(client).endswith(
                    "[running; poll with an empty send]"
                )
                started.wait("embedded Python startup hook")
                worker = int(marker.read_text())
                client.send(control="interrupt", timeout_ms=10_000)
                interrupted = last_result_text(client)
                assert "KeyboardInterrupt" in interrupted, interrupted
                assert "[running; poll with an empty send]" not in interrupted, (
                    interrupted
                )
                # Retain the complete traceback; site.py locations vary with
                # the host interpreter used to construct this bare environment.
                client.transcript[-1]["result"]["content"][0]["text"] = re.sub(
                    r'(File "<frozen site>", line )\d+',
                    r"\1<line>",
                    interrupted,
                )
                client.send(
                    # fmt: python
                    python=code(f"""
                        import builtins
                        import os

                        assert os.getpid() == {worker}
                        assert builtins.startup_attempts == 2
                        "never_run" in globals()
                        """)
                )
                assert last_result_text(client) == "False\n", last_result_text(client)
                client.transcript[-1]["send"]["python"] = client.transcript[-1]["send"][
                    "python"
                ].replace(f"== {worker}", "== <worker pid>")
                client.send(python="builtins.startup_attempts")
                assert last_result_text(client) == "2\n", last_result_text(client)
                if r_first:
                    client.send(r="startup_state + 1L")
                    assert last_result_text(client) == "[1] 42\n"
                transcript = json.dumps(client.finish())
                transcript = transcript.replace(str(site), "<site-packages>")
                transcript = transcript.replace(str(root), "<workspace>")
                return json.loads(transcript)
        finally:
            started.close()


@executions(DIRECT, SANDBOXED)
def test_interrupts_embedded_sitecustomize(binary: Path, execution: Execution) -> list:
    return interrupted_initialization(binary, execution)


@executions(DIRECT, SANDBOXED)
def test_interrupts_embedded_pth(binary: Path, execution: Execution) -> list:
    return interrupted_initialization(binary, execution, hook="console_startup")


@executions(DIRECT, SANDBOXED)
def test_interrupts_embedded_startup_after_r(
    binary: Path, execution: Execution
) -> list:
    return interrupted_initialization(binary, execution, r_first=True)


def interrupted_discovery(
    binary: Path, execution: Execution, language: str, control: str = "interrupt"
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python = root / "python"
        marker = root / "discovery.json"
        started = FifoCheckpoint.create(root / "started")
        # fmt: python
        source = code(f"""
            import json
            import os
            import signal
            import sys
            from pathlib import Path

            marker = Path({str(marker)!r})
            if marker.exists():
                os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            marker.write_text(json.dumps([os.getpid(), os.getppid()]))
            with open({str(started.path)!r}, "wb", buffering=0) as ready:
                ready.write(b"1")
            while True:
                signal.pause()
            """)
        python.write_text(f"#!{sys.executable}\n" + source)
        python.chmod(0o755)
        child = None
        try:
            with McpClient(
                binary,
                execution.serve(
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ())
                ),
                selected_python(root, python),
                root,
            ) as client:
                client.initialize_and_list_tools()
                cell = "never_run = True" if language == "python" else "SELECT 42"
                client.send(**{language: cell}, timeout_ms=0)
                assert last_result_text(client).endswith(
                    "[running; poll with an empty send]"
                )
                started.wait("Python discovery")
                child, worker = json.loads(marker.read_text())
                child = host_process_id(child, client.process.pid)
                worker_identity = capture_process_identity(
                    host_process_id(worker, client.process.pid)
                )
                client.send(control=control, timeout_ms=10_000)
                expected = (
                    "KeyboardInterrupt"
                    if control == "interrupt"
                    else "[worker stopped: in-memory state lost]"
                )
                assert expected in last_result_text(client), last_result_text(client)
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    child = None
                assert child is None, "interrupted discovery child is still alive"
                if control == "restart":
                    assert not live_processes([worker_identity]), (
                        "old worker survived restart"
                    )
                    client.send(python="'never_run' in globals()")
                else:
                    client.send(
                        # fmt: python
                        python=code(f"""
                            import os

                            assert os.getpid() == {worker}
                            "never_run" in globals()
                            """)
                    )
                assert last_result_text(client) == "False\n", last_result_text(client)
                client.transcript[-1]["send"]["python"] = client.transcript[-1]["send"][
                    "python"
                ].replace(str(worker), "<worker pid>")
                return json.loads(
                    json.dumps(client.finish()).replace(str(root), "<workspace>")
                )
        finally:
            started.close()
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_discovery(binary: Path, execution: Execution) -> list:
    return interrupted_discovery(binary, execution, "python")


@executions(DIRECT, SANDBOXED)
def test_restarts_during_python_discovery(binary: Path, execution: Execution) -> list:
    return interrupted_discovery(binary, execution, "python", "restart")


if __name__ == "__main__":
    run_this_suite(__file__)
