"""Public MCP coverage with no R executable visible to the local server."""

import json
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import UNPRIVILEGED, requires
from support.normalization import code, normalize_python_resolution_error
from support.native import build_interposer


def environment(path: Path) -> dict[str, str]:
    env = dict(os.environ, PATH=str(path))
    for name in (
        "R_HOME",
        "R_LIBS",
        "R_LIBS_USER",
        "RETICULATE_PYTHON",
        "RETICULATE_UV",
    ):
        env.pop(name, None)
    return env


def preparation_environment(root: Path) -> dict[str, str]:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/sans_r_uv.py"
    uv = root / "uv"
    uv.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    uv.chmod(0o755)
    invalid = root / "invalid-python"
    # A resolver can succeed while its interpreter is not embeddable.
    # fmt: python
    inspection = code("""
        import json
        import os
        import signal
        import sys
        from pathlib import Path

        # Cache warmup has no output-file argument. Gate only the subsequent
        # native inspector, which supplies its private result path.
        if len(sys.argv) == 4:
            sys.exit(0)
        root = Path(__file__).parent
        if (root / "mode").read_text() == "inspection-interrupt":
            signal.signal(signal.SIGINT, lambda *_: sys.exit("fixture Python inspection interrupted"))
            (root / "resolver-pid").write_text(str(os.getpid()))
            with (root / "started").open("wb", buffering=0) as stream:
                stream.write(b"1")
            signal.pause()
            raise AssertionError("cancelled inspection continued")
        result = dict(executable=str(Path(__file__)), libpython=str(root / "missing-libpython"))
        for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix"):
            result[name] = str(root)
        Path(sys.argv[-1]).write_text(json.dumps(result))
        """)
    invalid.write_text(f"#!{sys.executable}\n" + inspection)
    invalid.chmod(0o755)
    env = environment(root)
    env["MCP_CONSOLE_TEST_PREPARATION"] = str(root)
    env["MCP_CONSOLE_TEST_REAL_UV"] = shutil.which("uv")
    return env


def preparation_records(records: Transcript, root: Path) -> Transcript:
    for record in records:
        for content in record.get("result", {}).get("content", []):
            if content.get("type") != "text":
                continue
            text = (
                content["text"]
                .replace(str(root.resolve()), "<preparation>")
                .replace(str(root), "<preparation>")
            )
            if text.startswith("managed Python resolution failed:"):
                text = normalize_python_resolution_error(text)
            content["text"] = text
    return records


@executions(DIRECT, SANDBOXED)
def test_prepares_managed_python_at_startup_and_restart(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    records = []
    for with_code in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            uv = workspace / "uv"
            uv.symlink_to(shutil.which("uv"))
            with McpClient(
                binary, execution.serve(), environment(workspace), workspace
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    requirements={"python": ["py-yaml12"]},
                    **(
                        {"python": "import yaml12; print('startup package available')"}
                        if with_code
                        else {}
                    ),
                )
                assert not client.transcript[-1]["result"]["isError"], (
                    client.transcript[-1]
                )
                schema = client.transcript[2]["result"]["tools"][0]["inputSchema"]
                assert {"r", "sql"}.isdisjoint(schema["properties"])
                requirement_schema = schema["properties"]["requirements"]
                assert set(requirement_schema["properties"]) == {"python"}
                client.send(
                    # fmt: python
                    python=code("""
                        import os, subprocess, sys, numpy, pandas, yaml12

                        assert "RETICULATE_PYTHON" not in os.environ
                        subprocess.run([sys.executable, "-c", "import numpy, pandas, yaml12"], check=True)
                        identity = object()
                        identity_id = id(identity)
                        print("startup packages available")
                        """)
                )
                assert last_result_text(client) == "startup packages available\n"
                # A no-op must not invoke uv or replace the running interpreter.
                uv.unlink()
                client.send(requirements={"python": ["py-yaml12", "numpy"]})
                assert last_result_text(client) == "[prepared]"
                client.send(
                    python="assert id(identity) == identity_id; print('same worker')"
                )
                assert last_result_text(client) == "same worker\n"
                client.send(
                    requirements={"python": ["py-yaml12"]},
                    python="assert id(identity) == identity_id; print('no-op cell')",
                )
                assert last_result_text(client) == "no-op cell\n"
                client.send(
                    python="input('old worker> '); open('old-worker-consumed-input', 'w').close()"
                )
                assert "[waiting for stdin]" in last_result_text(client)
                uv.symlink_to(shutil.which("uv"))
                client.send(
                    control="restart",
                    requirements={"python": ["more-itertools"]},
                    stdin="replacement input\n",
                    **(
                        {"python": "assert 'identity' not in globals(); print(input())"}
                        if with_code
                        else {}
                    ),
                )
                assert not client.transcript[-1]["result"]["isError"], (
                    client.transcript[-1]
                )
                if not with_code:
                    client.send(
                        python="assert 'identity' not in globals(); print(input())"
                    )
                assert "replacement input\n" in last_result_text(client)
                assert not (workspace / "old-worker-consumed-input").exists()
                # Plain restart and crash replacement retain the accepted result.
                uv.unlink()
                for control in ({}, {"control": "restart"}, {"crash": True}):
                    if control.pop("crash", False):
                        client.send(python="import os; os._exit(47)")
                        assert "status 47" in last_result_text(client), (
                            client.transcript[-1]
                        )
                    client.send(
                        **control,
                        # fmt: python
                        python=code("""
                            import os, subprocess, sys, numpy, pandas, yaml12, more_itertools

                            assert "RETICULATE_PYTHON" not in os.environ
                            probe = "import numpy, pandas, yaml12, more_itertools"
                            subprocess.run([sys.executable, "-c", probe], check=True)
                            subprocess.run(["python", "-c", probe], check=True)
                            print("cumulative packages retained")
                            """),
                    )
                    assert "cumulative packages retained\n" in last_result_text(
                        client
                    ), client.transcript[-1]
                client.send(requirements={"python": ["more-itertools", "py-yaml12"]})
                assert last_result_text(client) == "[prepared]"
                client.send(
                    control="restart",
                    requirements={"python": ["more-itertools", "py-yaml12"]},
                    python="import yaml12, more_itertools; print('retained restart')",
                )
                assert "retained restart\n" in last_result_text(client), (
                    client.transcript[-1]
                )
                records.append({"startup_and_restart_with_code": with_code})
                records.extend(client.finish())
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            quarto = (session / "transcript.qmd").read_text()
            assert "  packages: []\n" in quarto
            for package in ("numpy", "pandas", "py-yaml12", "more-itertools"):
                assert f"    - {package}\n" in quarto
    return TranscriptWithCompanions(
        records, {"qmd": quarto.replace(str(workspace.resolve()), "<workspace>")}
    )


@executions(DIRECT, SANDBOXED)
def test_failed_managed_preparation_preserves_worker_and_input(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = preparation_environment(root)
        started = FifoCheckpoint.create(root / "started")
        try:
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                client.send(
                    python="identity = object(); identity_id = id(identity); 42",
                    stdin="retained input\n",
                )
                assert last_result_text(client) == "42\n"
                for with_code in (False, True):
                    for mode, expected in (
                        ("failure", "fixture Python resolution failed"),
                        ("inspection", "selected Python embedding library is missing"),
                        ("interrupt", "fixture Python resolution interrupted"),
                        (
                            "inspection-interrupt",
                            "fixture Python inspection interrupted",
                        ),
                    ):
                        (root / "mode").write_text(mode)
                        arguments = dict(
                            control="restart",
                            requirements={"python": ["py-yaml12"]},
                            stdin="must not reach old worker\n",
                            **(
                                {
                                    "python": "raise AssertionError('failed restart ran code')"
                                }
                                if with_code
                                else {}
                            ),
                        )
                        if mode in ("interrupt", "inspection-interrupt"):
                            pending = client.start_send(**arguments)
                            started.wait("candidate resolver entered")
                            interrupt = client.start_send(control="interrupt")
                            client.receive_many([pending, interrupt])
                            response = pending["result"]
                            pid = int((root / "resolver-pid").read_text())
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                pass
                            else:
                                raise AssertionError(
                                    "resolver child survived interruption"
                                )
                        else:
                            response = client.send(**arguments)
                        assert response["isError"], response
                        text = "".join(
                            item.get("text", "") for item in response["content"]
                        )
                        assert expected in text, response
                        client.send(
                            python="assert id(identity) == identity_id; print('objects intact')"
                        )
                        assert last_result_text(client) == "objects intact\n"
                before = (root / "resolutions.jsonl").read_text()
                for request in (
                    {"requirements": {"python": ["py-yaml12"]}},
                    {
                        "requirements": {"python": ["py-yaml12"]},
                        "python": "identity = None",
                        "stdin": "rejected live input\n",
                    },
                    {
                        "control": "restart",
                        "requirements": {"python": ["./local-package"]},
                        "stdin": "invalid input\n",
                    },
                    {"requirements": {"r": ["cli"]}},
                    {"requirements": {"duckdb": ["json"]}},
                ):
                    response = client.send(**request)
                    assert response.get("isError", True), response
                assert (root / "resolutions.jsonl").read_text() == before
                client.send(python="assert id(identity) == identity_id; print(input())")
                assert (
                    last_result_text(client)
                    == '[input requested: ""]\nretained input\n'
                ), client.transcript[-1]
                client.send(python="print(input('remaining> '))")
                assert "[waiting for stdin]" in last_result_text(client)
                client.send(
                    control="interrupt",
                    requirements={"python": ["py-yaml12"]},
                    python="identity = None",
                    stdin="rejected interrupt input\n",
                )
                assert client.transcript[-1]["result"]["isError"], client.transcript[-1]
                client.send()
                assert "[waiting for stdin]" in last_result_text(client), (
                    client.transcript[-1]
                )
                wait_for_evaluation_output(
                    client,
                    "fresh input\n",
                    "queue remained empty",
                    stdin="fresh input\n",
                )
                # Failed candidates were never retained: the addition still needs resolution.
                (root / "mode").write_text("success")
                client.send(
                    control="restart",
                    requirements={"python": ["py-yaml12"]},
                    python="import yaml12; print('accepted')",
                )
                assert "accepted\n" in last_result_text(client), client.transcript[-1]
                assert (root / "resolutions.jsonl").read_text() != before
                records = preparation_records(client.finish(), root)
            (session,) = (root / ".agents/console/sessions").iterdir()
            events = [
                json.loads(line)
                for line in (session / "internal/events.jsonl").read_text().splitlines()
            ]
            accepted = [
                event
                for event in events
                if event["event"] == "python_environment_accepted"
            ]
            assert len(accepted) == 1, accepted
            assert set(accepted[0]["packages"]) == {"numpy", "pandas", "py-yaml12"}
            return records
        finally:
            started.close()


@executions(DIRECT, SANDBOXED)
def test_retries_failed_prestart_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for with_code in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = preparation_environment(root)
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                for mode in ("failure", "inspection"):
                    (root / "mode").write_text(mode)
                    client.send(
                        requirements={"python": ["py-yaml12"]},
                        **(
                            {
                                "python": "raise AssertionError('failed preparation ran code')",
                                "stdin": "rejected input\n",
                            }
                            if with_code
                            else {}
                        ),
                    )
                    assert client.transcript[-1]["result"]["isError"], (
                        client.transcript[-1]
                    )
                (root / "mode").write_text("success")
                client.send(requirements={"python": ["py-yaml12"]})
                assert last_result_text(client) == "[prepared]"
                client.send(python="import yaml12; print(input('prepared> '))")
                assert "[waiting for stdin]" in last_result_text(client), (
                    client.transcript[-1]
                )
                wait_for_evaluation_output(
                    client,
                    "fresh input\n",
                    "failed prestart did not queue input",
                    stdin="fresh input\n",
                )
                records.append({"with_code": with_code})
                records.extend(preparation_records(client.finish(), root))
    return records


@executions(DIRECT, SANDBOXED)
def test_shutdown_cancels_sans_r_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for restart, mode in (
        (False, "interrupt"),
        (True, "interrupt"),
        (False, "inspection-interrupt"),
        (True, "inspection-interrupt"),
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = preparation_environment(root)
            (root / "mode").write_text(mode)
            started = FifoCheckpoint.create(root / "started")
            try:
                with McpClient(binary, execution.serve(), env, root) as client:
                    client.initialize_and_list_tools()
                    if restart:
                        client.send(python="retained = 42; retained")
                        assert last_result_text(client) == "42\n"
                    client.start_send(
                        requirements={"python": ["py-yaml12"]},
                        python="raise AssertionError('cancelled preparation ran code')",
                        **({"control": "restart"} if restart else {}),
                    )
                    started.wait("resolver entered before input closure")
                    pid = int((root / "resolver-pid").read_text())
                    client.stdin.close()
                    assert client.process.wait(timeout=10) == 0
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise AssertionError("cancelled resolver survived shutdown")
                    records.append(
                        {
                            "restart": restart,
                            "phase": mode,
                            "stdout": client.stdout.read(),
                            "stderr": client.stderr.read(),
                        }
                    )
                (session,) = (root / ".agents/console/sessions").iterdir()
                events = [
                    json.loads(line)
                    for line in (session / "internal/events.jsonl")
                    .read_text()
                    .splitlines()
                ]
                assert all(
                    event["event"] != "python_environment_accepted" for event in events
                )
            finally:
                started.close()
    return records


@executions(DIRECT, SANDBOXED)
def test_resolves_default_python_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        uv = shutil.which("uv")
        assert uv is not None
        (path / "uv").symlink_to(uv)
        with McpClient(binary, execution.serve(), environment(path)) as client:
            client.initialize_and_list_tools()
            schema = client.transcript[-1]["result"]["tools"][0]
            assert {"r", "sql"}.isdisjoint(schema["inputSchema"]["properties"])
            assert (
                "without R" in schema["description"]
                or "R and SQL" in schema["description"]
            )
            client.send(
                # fmt: python
                python=code("""
                    import json
                    import os
                    import subprocess
                    import sys
                    import numpy
                    import pandas

                    assert "RETICULATE_PYTHON" not in os.environ
                    assert sys.prefix != sys.base_prefix
                    base = os.path.join(sys.base_prefix, "bin", "python3")
                    probe = "import importlib.util; assert importlib.util.find_spec('pandas') is None"
                    subprocess.run([base, "-I", "-c", probe], check=True)
                    probe = "import json, sys, pandas; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix]))"
                    child = json.loads(subprocess.check_output([sys.executable, "-c", probe], text=True))
                    assert child == [sys.executable, sys.prefix, sys.exec_prefix]
                    child = json.loads(subprocess.check_output(["python", "-c", probe], text=True))
                    assert child == [sys.executable, sys.prefix, sys.exec_prefix]
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    import multiprocessing

                    with multiprocessing.get_context("spawn").Pool(1) as pool:
                        child = pool.apply(eval, ("__import__('sys').executable",))
                    assert child == sys.executable
                    # Release the pool's semaphores before restarting a worker
                    # whose interpreter is intentionally never finalized.
                    del pool
                    import gc

                    gc.collect()
                    selected = sys.executable
                    retained = 41
                    identity = object()
                    identity_id = id(identity)
                    retained + 1
                    """)
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            client.send(python="assert id(identity) == identity_id; retained + 2")
            assert last_result_text(client) == "43\n", client.transcript[-1]
            for request in (
                {"r": "1"},
                {"sql": "SELECT 1"},
                {"requirements": {"python": ["six"]}},
                {"requirements": {"r": ["cli"]}},
                {"requirements": {"duckdb": ["json"]}},
            ):
                result = client.send(**request)
                assert result.get("isError", True), result
                client.send(python="assert id(identity) == identity_id; retained + 2")
                assert last_result_text(client) == "43\n"
            client.send(python="import mcp_console_package_that_does_not_exist")
            assert "automatic package installation is unavailable" in last_result_text(
                client
            )
            assert "user-selected" not in last_result_text(client)
            client.send(python="assert id(identity) == identity_id; retained + 2")
            assert last_result_text(client) == "43\n"
            client.send(python="print(selected)")
            selected = last_result_text(client).strip()
            # Resolution cannot run again: remove uv after successful selection.
            (path / "uv").unlink()
            discovered_r = path / "R"
            discovered_r.write_text(
                code("""
                #!/bin/sh
                exit 48
                """)
            )
            discovered_r.chmod(0o755)
            client.send(
                control="restart", python="import sys, pandas; print(sys.executable)"
            )
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n"
                + selected
                + "\n[done]"
            ), client.transcript[-1]
            records = client.finish()
            for record in records:
                if "result" in record:
                    for content in record["result"].get("content", []):
                        if content.get("type") == "text":
                            content["text"] = content["text"].replace(
                                selected, "<selected Python>"
                            )
            return records


@executions(DIRECT, SANDBOXED)
def test_uses_path_python_without_uv(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        subprocess.run(
            ["uv", "pip", "install", "--python", selected, "matplotlib"],
            check=True,
            capture_output=True,
        )
        env = environment(venv / "bin")
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys
                    import tempfile
                    from pathlib import Path
                    import matplotlib.pyplot as plt

                    assert "RETICULATE_PYTHON" not in os.environ
                    assert sys.prefix != sys.base_prefix
                    value = 40
                    temporary = Path(tempfile.gettempdir())
                    (temporary / "owned.txt").write_text("owned")
                    print(temporary)
                    """)
            )
            temporary = Path(last_result_text(client).strip())
            assert temporary.is_dir(), client.transcript[-1]
            client.send(
                python="value += 1; print('before error'); raise ValueError('recoverable')"
            )
            assert "ValueError: recoverable" in last_result_text(client)
            client.send(python="value + 1")
            assert last_result_text(client) == "42\n"
            client.send(python="answer = input('value> '); answer")
            assert "[waiting for stdin]" in last_result_text(client)
            wait_for_evaluation_output(
                client, "'hello'\n", "sans-R input", stdin="hello\n"
            )
            client.send(
                # fmt: python
                python=code("""
                    _ = plt.plot([1, 2], [3, 4])
                    plt.gcf().savefig(temporary / "expected.png")
                    """)
            )
            assert_result_content(
                client,
                [(temporary / "expected.png").read_bytes()],
                image_reference="selected environment savefig {page}",
            )
            client.send(python="input('interrupt> ')")
            client.send(control="interrupt")
            assert "KeyboardInterrupt" in last_result_text(client)
            client.send(python="value + 1")
            assert last_result_text(client) == "42\n"
            client.send(control="restart", python="'value' in globals()")
            assert (
                last_result_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\nFalse\n[done]"
            ), client.transcript[-1]
            assert not temporary.exists(), "retired worker storage remains"
            client.send(python="import tempfile; print(tempfile.gettempdir())")
            replacement = Path(last_result_text(client).strip())
            assert replacement.is_dir() and replacement != temporary
            records = client.finish()
            assert not replacement.exists(), "shutdown worker storage remains"
            assert selected.exists(), "retirement deleted the selected environment"
            for record in records:
                if "result" in record:
                    for content in record["result"].get("content", []):
                        if content.get("type") == "text":
                            content["text"] = (
                                content["text"]
                                .replace(str(temporary), "<worker temporary>")
                                .replace(str(replacement), "<replacement temporary>")
                            )
            return records


@executions(DIRECT, SANDBOXED)
def test_reports_missing_interpreters(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(
            [binary, *execution.serve()],
            env=environment(Path(directory)),
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "neither `uv`, `python3`, nor `python`" in result.stderr, result.stderr
        return [{"stderr": result.stderr}]


@executions(DIRECT, SANDBOXED)
def test_resolver_failure_does_not_fall_back(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "uv").symlink_to(shutil.which("uv"))
        (path / "python3").symlink_to(sys.executable)
        env = environment(path)
        env["UV_PYTHON_PREFERENCE"] = "invalid-console-acceptance-preference"
        # Keep MCP input open: the resolver failure, rather than input-owner
        # cancellation, must determine the outcome.
        with McpClient(binary, execution.serve(), env) as client:
            client.process.wait(timeout=30)
            diagnostic = client.stderr.read()
            assert client.process.returncode != 0
            assert "managed Python version resolution failed" in diagnostic, diagnostic
            assert "invalid-console-acceptance-preference" in diagnostic, diagnostic
            return [{"stderr": diagnostic}]


@executions(DIRECT, SANDBOXED)
def test_uses_python_when_python3_is_absent(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), environment(path)) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import sys; assert sys.executable.endswith('/python'); identity = object(); identity_id = id(identity); 42"
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            for request in (
                {"requirements": {"python": ["py-yaml12"]}},
                {
                    "control": "restart",
                    "requirements": {"python": ["py-yaml12"]},
                    "python": "raise AssertionError('non-managed restart ran')",
                },
            ):
                result = client.send(**request)
                assert result["isError"], result
                assert "non-managed Python session" in last_result_text(client)
                client.send(python="assert id(identity) == identity_id; 42")
                assert last_result_text(client) == "42\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_broken_r_instead_of_selecting_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        env = environment(path)
        env["R_HOME"] = str(path / "missing-r")
        result = subprocess.run(
            [binary, *execution.serve()],
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0 and "Rscript" in result.stderr, result.stderr
        explicit = result.stderr.replace(str(path), "<fixture>")
        env.pop("R_HOME")
        broken = path / "R"
        broken.write_text(
            code("""
            #!/bin/sh
            echo 'broken discovered R' >&2
            exit 41
            """)
        )
        broken.chmod(0o755)
        result = subprocess.run(
            [binary, *execution.serve()],
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0 and "broken discovered R" in result.stderr, (
            result.stderr
        )
        return [{"invalid_R_HOME": explicit}, {"broken_R": result.stderr}]


@executions(DIRECT, SANDBOXED)
def test_cleans_temporary_storage_after_startup_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        site = Path(
            subprocess.check_output(
                [selected, "-c", "import site; print(site.getsitepackages()[0])"],
                text=True,
            ).strip()
        )
        # This is a real selected interpreter's startup hook, not an evaluator.
        # Inspection succeeds; only the subsequently embedded worker exits.
        # fmt: python
        hook = code("""
            import os
            from pathlib import Path

            if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                Path("startup-temporary").write_text(os.environ["TMPDIR"])
                Path(os.environ["TMPDIR"], "owned-before-failure").touch()
                os._exit(47)
            """)
        (site / "sitecustomize.py").write_text(hook)
        arguments = (
            execution.serve("--writable-root", str(root))
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(
            binary, arguments, environment(venv / "bin"), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('startup failure ran the cell')"
            )
            assert result["isError"] and "status 47" in last_result_text(client), result
            temporary = Path((root / "startup-temporary").read_text())
            assert not temporary.exists(), "failed worker storage remains"
            assert selected.exists(), "startup failure deleted the environment"
            return client.finish()


@executions(DIRECT)
def test_describes_direct_python_session(
    binary: Path, execution: Execution
) -> Transcript:
    return describe_session(binary, execution)


@executions(SANDBOXED)
def test_describes_sandboxed_python_session(
    binary: Path, execution: Execution
) -> Transcript:
    return describe_session(binary, execution)


def describe_session(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), environment(path)) as client:
            client.initialize_and_list_tools()
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_python_execution(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        with McpClient(
            binary,
            execution.serve(),
            environment(workspace),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="recorded_value = 41; recorded_value + 1")
            assert last_result_text(client) == "42\n"
            client.send(python="raise ValueError('recorded failure')")
            assert "ValueError: recorded failure" in last_result_text(client)
            client.send(control="restart", python="'recorded_value' in globals()")
            assert (
                last_result_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\nFalse\n[done]"
            )
            records = client.finish()
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            markdown = (session / "transcript.md").read_text()
            quarto = (session / "transcript.qmd").read_text()
            assert "recorded_value = 41" in markdown and "recorded_value = 41" in quarto
            assert "  python-packages: []\n" in quarto
            assert "ValueError: recorded failure" in markdown
            assert (session / "outputs/call-000001.log").read_text() == "42\n"
            events = [
                json.loads(line)
                for line in (session / "internal/events.jsonl").read_text().splitlines()
            ]
            assert events[0]["event"] == "session_started"
            assert sum(event["event"] == "tool_call" for event in events) == 3
            records.append(
                {
                    "recording": {
                        "events": [event["event"] for event in events],
                        "markdown and quarto": "Python source retained",
                        "output log": "42\n",
                    }
                }
            )
            return records


@executions(DIRECT, SANDBOXED)
def test_preserves_explicit_python_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        env = environment(Path(directory))
        env["RETICULATE_PYTHON"] = sys.executable
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import os, sys; assert sys.executable == os.environ['RETICULATE_PYTHON']; identity = object(); identity_id = id(identity); 42"
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            for control in ({}, {"control": "restart"}):
                result = client.send(**control, requirements={"python": ["py-yaml12"]})
                assert result["isError"], result
                assert "non-managed Python session" in last_result_text(client)
                client.send(python="assert id(identity) == identity_id; 42")
                assert last_result_text(client) == "42\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_and_replaces_a_failed_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), environment(path)) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 41")
            wait_for_evaluation_output(
                client,
                "loop entered\n\n[running; poll with an empty send]",
                "Python loop entry",
                # fmt: python
                python=code("""
                    print("loop entered")
                    # Keep interrupt locations stable across Python bytecode versions.
                    while True: pass  # fmt: skip
                    """),
                timeout_ms=100,
            )
            client.send(control="interrupt")
            assert "KeyboardInterrupt" in last_result_text(client), client.transcript[
                -1
            ]
            client.send(python="retained + 1")
            assert last_result_text(client) == "42\n"
            client.send(python="import tempfile; print(tempfile.gettempdir())")
            temporary = Path(last_result_text(client).strip())
            client.send(python="import os; os._exit(23)")
            assert "status 23" in last_result_text(client), client.transcript[-1]
            client.send(python="assert 'retained' not in globals(); 42")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            assert not temporary.exists(), "failed worker storage remains"
            records = client.finish()
            for record in records:
                for content in record.get("result", {}).get("content", []):
                    if content["type"] == "text":
                        content["text"] = content["text"].replace(
                            str(temporary), "<worker temporary>"
                        )
            return records


@executions(DIRECT, SANDBOXED)
def test_uses_path_uv_with_legacy_managed_uv_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "uv").symlink_to(shutil.which("uv"))
        env = environment(path)
        env["RETICULATE_UV"] = "managed"
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(python="import numpy, pandas; 42")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(SANDBOXED)
def test_preserves_explicit_selection_in_sandbox_environment(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for inherit in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "inherit_environment": inherit,
                            "environment": {
                                "RETICULATE_PYTHON": "/invalid/project/python"
                            },
                        }
                    }
                )
            )
            env = environment(workspace)
            env["RETICULATE_PYTHON"] = sys.executable
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                for control in ({}, {"control": "restart"}):
                    client.send(
                        **control,
                        # fmt: python
                        python=code("""
                            import os
                            import sys

                            assert os.environ["RETICULATE_PYTHON"] == sys.executable
                            print("explicit selection retained")
                            """),
                    )
                    assert "explicit selection retained\n" in last_result_text(client)
                records.extend(client.finish())
    return records


@executions(DIRECT, SANDBOXED)
def test_inspection_excludes_workspace_and_pythonpath(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        poisoned_path = workspace / "pythonpath"
        poisoned_path.mkdir()
        # fmt: python
        payload = code("""
            from pathlib import Path

            Path("host-import-executed").touch()
            raise RuntimeError("inspection imported workspace code")
            """)
        (workspace / "ctypes.py").write_text(payload)
        (poisoned_path / "sitecustomize.py").write_text(payload)
        env = environment(workspace)
        env["PYTHONPATH"] = str(poisoned_path)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            assert not (workspace / "host-import-executed").exists()
            # Workload imports keep their ordinary semantics inside the worker.
            (workspace / "ctypes.py").unlink()
            (poisoned_path / "sitecustomize.py").unlink()
            shutil.rmtree(poisoned_path / "__pycache__", ignore_errors=True)
            client.send(python="41 + 1")
            assert last_result_text(client) == "42\n"
            return client.finish()


def failed_native_startup(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        probe = build_interposer(workspace, "python_exit_state")
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        site = Path(
            subprocess.check_output(
                [selected, "-c", "import site; print(site.getsitepackages()[0])"],
                text=True,
            ).strip()
        )
        # A real installed startup hook changes only the embedded worker.
        # fmt: python
        hook = code(f"""
            import os
            import sys

            if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                import ctypes
                from pathlib import Path

                probe = ctypes.CDLL({str(probe)!r})
                Path("startup-temporary").write_text(os.environ["TMPDIR"])
                sys.prefix = "changed-by-startup-hook"
            """)
        (site / "sitecustomize.py").write_text(hook)
        arguments = (
            execution.serve("--writable-root", str(workspace))
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(
            binary, arguments, environment(venv / "bin"), workspace
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('failed startup ran cell')"
            )
            assert result["isError"], result
            records = client.finish()
            temporary = Path((workspace / "startup-temporary").read_text())
            assert not temporary.exists(), "failed worker storage remains"
            assert selected.exists(), "failed startup removed selected environment"
            for record in records:
                for content in record.get("result", {}).get("content", []):
                    if content["type"] == "text":
                        content["text"] = content["text"].replace(
                            str(venv), "<selected environment>"
                        )
            return records


@executions(DIRECT, SANDBOXED)
def test_startup_failure_restores_python_thread(
    binary: Path, execution: Execution
) -> Transcript:
    records = failed_native_startup(binary, execution)
    diagnostic = records[-1]["result"]["content"][0]["text"]
    assert "Python exit thread attached\n" in diagnostic, diagnostic
    assert "Python exit thread detached" not in diagnostic, diagnostic
    return records


@executions(DIRECT, SANDBOXED)
def test_startup_failure_preserves_python_exception(
    binary: Path, execution: Execution
) -> Transcript:
    records = failed_native_startup(binary, execution)
    diagnostic = records[-1]["result"]["content"][0]["text"]
    assert (
        "RuntimeError: embedded Python prefix differs from the selected environment"
        in diagnostic
    ), diagnostic
    assert "'changed-by-startup-hook' != '<selected environment>'" in diagnostic, (
        diagnostic
    )
    return records


@executions(DIRECT, SANDBOXED)
def test_uses_environment_through_directory_alias(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        original = workspace / "original"
        original.mkdir()
        venv = original / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        alias = workspace / "alias"
        alias.symlink_to(original, target_is_directory=True)
        env = environment(alias / "environment/bin")
        env["MCP_CONSOLE_TEST_ENVIRONMENT"] = str(venv)
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys
                    import subprocess

                    assert "RETICULATE_PYTHON" not in os.environ
                    assert os.path.samefile(sys.prefix, os.environ["MCP_CONSOLE_TEST_ENVIRONMENT"])
                    assert os.path.samefile(sys.exec_prefix, sys.prefix)
                    child = subprocess.check_output(
                        [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                    ).strip()
                    assert os.path.samefile(child, sys.prefix)
                    print("selected environment retained through directory alias")
                    """),
            )
            assert (
                last_result_text(client)
                == "selected environment retained through directory alias\n"
            ), client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_consumes_idle_interrupt_before_next_python_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), environment(path)) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 40")
            client.send(control="interrupt")
            client.send(python="retained += 1; retained")
            assert last_result_text(client) == "41\n", client.transcript[-1]
            client.send(control="interrupt", python="retained += 1; retained")
            assert last_result_text(client) == "42\n[done]", client.transcript[-1]
            client.send(python="retained")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_imports_workspace_modules_without_pythonpath(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        (workspace / "workspace_module.py").write_text("value = 20\n")
        package = workspace / "workspace_package"
        package.mkdir()
        (package / "__init__.py").write_text("value = 22\n")
        subdirectory = workspace / "subdirectory"
        subdirectory.mkdir()
        (subdirectory / "after_chdir.py").write_text("value = 43\n")
        env = environment(workspace)
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import sys
                        import workspace_module
                        import workspace_package

                        assert "PYTHONPATH" not in os.environ
                        assert sys.path[0] == ""
                        assert workspace_module.value + workspace_package.value == 42
                        os.chdir("subdirectory")
                        import after_chdir

                        after_chdir.value
                        """),
                )
                assert "43\n" in last_result_text(client), client.transcript[-1]
                assert not client.transcript[-1]["result"]["isError"]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_ignores_pythonhome_for_selected_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return ignores_python_layout_override(binary, execution, "PYTHONHOME")


@executions(DIRECT, SANDBOXED)
def test_ignores_pythonplatlibdir_for_selected_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return ignores_python_layout_override(binary, execution, "PYTHONPLATLIBDIR")


def ignores_python_layout_override(
    binary: Path, execution: Execution, variable: str
) -> Transcript:
    records = []
    for inherit in (True, False):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            venv = workspace / "environment"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", venv],
                check=True,
                capture_output=True,
            )
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "inherit_environment": inherit,
                            "environment": {variable: "unavailable-configured-layout"},
                        }
                    }
                )
            )
            env = environment(venv / "bin")
            env[variable] = "unavailable-inherited-layout"
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                for control in ({}, {"control": "restart"}):
                    client.send(
                        **control,
                        # fmt: python
                        python=code(f"""
                            import os
                            import sys
                            import subprocess

                            assert "{variable}" not in os.environ
                            assert "RETICULATE_PYTHON" not in os.environ
                            assert os.path.samefile(sys.prefix, "environment")
                            assert sys.prefix != sys.base_prefix
                            child = subprocess.check_output(
                                [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                            ).strip()
                            assert os.path.samefile(child, sys.prefix)
                            print("selected environment retained")
                            """),
                    )
                    assert "selected environment retained\n" in last_result_text(
                        client
                    ), client.transcript[-1]
                records.extend(client.finish())
    return records


@executions(DIRECT, SANDBOXED)
def test_accepts_parent_components_in_selected_executable(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        (workspace / "alias").mkdir()
        selected = workspace / "alias/../environment/bin/python3"
        env = environment(workspace)
        env["RETICULATE_PYTHON"] = str(selected)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import subprocess
                        import sys

                        selected = os.environ["RETICULATE_PYTHON"]
                        assert "/../" in selected
                        assert sys.executable == selected
                        assert os.path.samefile(sys.prefix, "environment")
                        assert sys.prefix != sys.base_prefix
                        child = subprocess.check_output(
                            [sys.executable, "-c", "import sys; print(sys.executable); print(sys.prefix)"],
                            text=True,
                        ).splitlines()
                        assert os.path.samefile(child[0], selected)
                        assert os.path.samefile(child[1], sys.prefix)
                        print("selected executable and environment retained")
                        """),
                )
                assert (
                    "selected executable and environment retained\n"
                    in last_result_text(client)
                ), client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_excludes_executable_directory_from_imports(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--copies", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        site = Path(
            subprocess.check_output(
                [selected, "-I", "-c", "import site; print(site.getsitepackages()[0])"],
                text=True,
            ).strip()
        )
        (site / "selected_package.py").write_text("value = 42\n")
        (venv / "bin/json.py").write_text(
            "raise RuntimeError('imported executable directory')\n"
        )
        (venv / "bin/selected_package.py").write_text("value = -1\n")
        env = environment(venv / "bin")
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import json
                        import os
                        import sys
                        import selected_package

                        assert sys.path[0] == ""
                        assert os.path.dirname(sys.executable) not in sys.path
                        assert selected_package.value == 42
                        json.dumps({"selected package": selected_package.value})
                        """),
                )
                assert not client.transcript[-1]["result"]["isError"], (
                    client.transcript[-1]
                )
                assert "'{\"selected package\": 42}'\n" in last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_managed_python_defaults(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            binary, execution.serve(), environment(workspace), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import numpy, pandas

                    retained = 42
                    retained
                    """)
            )
            assert last_result_text(client) == "42\n"
            client.send(requirements={"python": ["six"]})
            assert client.transcript[-1]["result"]["isError"]
            client.send(python="retained")
            assert last_result_text(client) == "42\n"
            records = client.finish()
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        quarto = (session / "transcript.qmd").read_text()
        assert "  python-packages:\n    - numpy\n    - pandas\n" in quarto, quarto
        assert "  packages: []\n" in quarto, quarto
        assert "six" not in quarto, quarto
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        assert events[0]["dynamic_resolution"] is False
        assert events[0]["python_preparation"] is True
        return TranscriptWithCompanions(
            records, {"qmd": quarto.replace(str(workspace.resolve()), "<workspace>")}
        )


@requires(UNPRIVILEGED)
@executions(DIRECT)
def test_reports_direct_storage_retirement_failure(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for stage in ("restart", "shutdown", "startup failure"):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            venv = workspace / "environment"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", venv],
                check=True,
                capture_output=True,
            )
            site = Path(
                subprocess.check_output(
                    [
                        venv / "bin/python3",
                        "-I",
                        "-c",
                        "import site; print(site.getsitepackages()[0])",
                    ],
                    text=True,
                ).strip()
            )
            # fmt: python
            restrict = code("""
                import os
                from pathlib import Path

                temporary = Path(os.environ["TMPDIR"])
                Path("worker-temporary").write_text(str(temporary))
                restricted = temporary / "restricted"
                restricted.mkdir()
                (restricted / "retained.txt").write_text("private contents")
                restricted.chmod(0)
                """)
            if stage == "startup failure":
                # fmt: python
                hook = code("""
                    import os
                    from pathlib import Path

                    if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                        temporary = Path(os.environ["TMPDIR"])
                        Path("worker-temporary").write_text(str(temporary))
                        restricted = temporary / "restricted"
                        restricted.mkdir()
                        (restricted / "retained.txt").write_text("private contents")
                        restricted.chmod(0)
                        os._exit(47)
                    """)
                (site / "sitecustomize.py").write_text(hook)
            try:
                with McpClient(
                    binary, execution.serve(), environment(venv / "bin"), workspace
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(python=restrict if stage != "startup failure" else "42")
                    if stage == "restart":
                        client.send(python="temporary.chmod(0)")
                        client.send(
                            control="restart", python="print('replacement ran')"
                        )
                    if stage != "shutdown":
                        assert client.transcript[-1]["result"]["isError"], (
                            client.transcript[-1]
                        )
                        assert (
                            "cannot remove worker temporary directory"
                            in last_result_text(client)
                        ), client.transcript[-1]
                        assert "replacement ran\n" not in last_result_text(client)
                    client.stdin.close()
                    client.process.wait(timeout=15)
                    stderr = client.stderr.read()
                    if stage == "startup failure":
                        # Startup already delivered its retirement error over MCP.
                        assert client.process.returncode == 0 and stderr == "", stderr
                    else:
                        assert client.process.returncode != 0, (stage, stderr)
                        assert "cannot remove worker temporary directory" in stderr, (
                            stderr
                        )
                    temporary = Path((workspace / "worker-temporary").read_text())
                    assert temporary.exists()
                    assert (venv / "bin/python3").exists()
                    records.append({"stage": stage})
                    records.extend(client.transcript)
                    records.append({"stderr": stderr})
                    # Normalize only this owned, run-specific path.
                    for record in records:
                        for content in record.get("result", {}).get("content", []):
                            if content["type"] == "text":
                                content["text"] = content["text"].replace(
                                    str(temporary), "<worker temporary>"
                                )
                        if "stderr" in record:
                            record["stderr"] = record["stderr"].replace(
                                str(temporary), "<worker temporary>"
                            )
            finally:
                marker = workspace / "worker-temporary"
                if marker.exists():
                    temporary = Path(marker.read_text())
                    if temporary.exists():
                        temporary.chmod(0o700)
                        (temporary / "restricted").chmod(0o700)
                        shutil.rmtree(temporary)
    return records
