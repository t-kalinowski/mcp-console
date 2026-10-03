#!/usr/bin/env -S uv run --script

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.r import install_r_startup, r_test_environment
from support.requirements import (
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    SANDBOX,
    WORKER,
    requires,
)
from support.ssh import SSH, configure, localhost, peer_environment
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
@requires(SSH, WORKER)
def test_controller_languages_override_remote_ambient_selection(
    binary: Path, execution: Execution
) -> list:
    check_ssh_optional_python_absence(binary, execution)
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        remote_environment, _ = r_test_environment()
        modules = remote / "modules"
        modules.mkdir()
        hook = remote / "python-hook"
        (modules / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import sys
                from pathlib import Path

                if "_mcp_console_services" in sys.modules:
                    Path({str(hook)!r}).touch()
                """),
        )
        prefix = remote / "launch"
        prefix.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(
                [
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    "MCP_CONSOLE_LANGUAGES=invalid-remote-selection",
                    "HOME=" + str(remote),
                    "R_HOME=" + remote_environment["R_HOME"],
                    str(binary),
                ]
            )
            + ' "$@"\n'
        )
        prefix.chmod(0o755)
        configure(
            local,
            remote,
            [str(prefix)],
            python=sys.executable,
            sandbox={
                "environment": {
                    "R_HOME": remote_environment["R_HOME"],
                    "RETICULATE_PYTHONPATH": str(modules),
                    "R_LIBS": "/unavailable",
                    "R_LIBS_USER": "/unavailable",
                    "R_LIBS_SITE": "/unavailable",
                }
            },
        )
        with localhost(root / "sshd") as environment:
            environment["MCP_CONSOLE_LANGUAGES"] = "r"
            with McpClient(binary, execution.serve(), environment, local) as client:
                client.initialize_and_list_tools()
                properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
                    "properties"
                ]
                assert (
                    "r" in properties
                    and "python" not in properties
                    and "sql" not in properties
                )
                for arguments in ({}, {"control": "restart"}):
                    client.send(
                        r='stopifnot(Sys.getenv("MCP_CONSOLE_LANGUAGES") == "r"); 42L',
                        **arguments,
                    )
                    expected = (
                        "[worker stopped: in-memory state lost]\n[starting new worker]\n[1] 42\n[done]"
                        if arguments
                        else "[1] 42\n"
                    )
                    assert last_result_text(client) == expected, last_result_text(
                        client
                    )
                    assert not hook.exists(), "disabled Python startup hook ran"
                client.finish()
        return [
            {"controller_r_only_selection_survives_remote_ambient_and_restart": True}
        ]


def check_ssh_optional_python_absence(binary: Path, execution: Execution) -> None:
    # Exercise real absent-interpreter discovery through MCP, alongside the
    # existing configured-language case, without changing its snapshot.
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        environment, _ = r_test_environment()
        library = install_r_startup(
            root,
            environment,
            # fmt: r
            code("""
                stopifnot(requireNamespace("reticulate", quietly = TRUE))
                startup_value <- 41L
                readline("R before Python discovery> ")
                """),
        )
        commands, home = remote / "commands", remote / "home"
        commands.mkdir()
        home.mkdir()
        (commands / "sh").symlink_to(shutil.which("sh"))
        workload = {
            "R_HOME": environment["R_HOME"],
            "PATH": str(commands),
            "HOME": str(home),
            "R_LIBS": str(library),
            "R_LIBS_USER": str(library),
            "R_LIBS_SITE": str(library),
            "R_DEFAULT_PACKAGES": environment["R_DEFAULT_PACKAGES"],
            "MCP_CONSOLE_TEST_BOOTSTRAP_SCRIPT": environment[
                "MCP_CONSOLE_TEST_BOOTSTRAP_SCRIPT"
            ],
            "RETICULATE_USE_MANAGED_VENV": "false",
        }
        prefix = remote / "launch"
        prefix.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(
                [
                    "/usr/bin/env",
                    "-i",
                    *[f"{key}={value}" for key, value in workload.items()],
                    str(binary),
                ]
            )
            + ' "$@"\n'
        )
        prefix.chmod(0o755)
        configure(local, remote, [str(prefix)], sandbox={"environment": workload})
        with localhost(root / "sshd") as controller:
            controller["MCP_CONSOLE_LANGUAGES"] = "r,python"
            with McpClient(binary, execution.serve(), controller, local) as client:
                client.initialize_and_list_tools()
                properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
                    "properties"
                ]
                assert {"r", "python"} <= properties.keys(), properties
                client.expect(
                    '[input requested: "R before Python discovery> "]\n[waiting for stdin]',
                    r="startup_value + 1L",
                    timeout_ms=0,
                )
                client.expect(
                    "[1] 42\n",
                    stdin="continue\n",
                )
                client.send(
                    # fmt: r
                    r=code("""
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        missing <- tryCatch(reticulate::py_config(), error = conditionMessage)
                        stopifnot(
                          is.character(missing),
                          grepl("Installation of Python not found", missing, fixed = TRUE)
                        )
                        startup_value + 1L
                        """),
                )
                assert last_result_text(client) == "[1] 42\n", last_result_text(client)
                client.finish()


def _preinstalled_remote_runtime(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local = root / "controller"
        remote = root / "remote workspace"
        local.mkdir()
        remote.mkdir()
        value = "remote environment value"
        # OpenSSH passes the executable prefix through the remote shell.
        argument = "literal space ' \" ; $(echo unexpected) $HOME"
        remote_environment, rscript = r_test_environment()
        preinstalled_libraries = subprocess.check_output(
            [
                rscript,
                "--vanilla",
                "-e",
                # fmt: r
                code("""
                    cat(paste(.libPaths(), collapse = .Platform$path.sep))
                    """),
            ],
            env=remote_environment,
            text=True,
        )
        prefix = root / "command space ' ; $()"
        prefix.write_text(
            code(r"""
                #!/bin/sh
                [ "$1" = VALUE ] || exit 23
                shift
                exec /usr/bin/env -i PATH=/usr/bin:/bin R_HOME=RHOME R_LIBS_USER=/unavailable R_LIBS_SITE=/unavailable EXECUTABLE "$@"
                """)
            .replace("VALUE", shlex.quote(argument))
            .replace("EXECUTABLE", shlex.quote(str(binary)))
            .replace("RHOME", shlex.quote(remote_environment["R_HOME"]))
        )
        prefix.chmod(0o755)
        config = configure(
            local,
            remote,
            [str(prefix), argument],
            extends=":workspace",
            sandbox={
                "environment": {
                    "CONSOLE_SSH_LITERAL": value,
                    "R_HOME": remote_environment["R_HOME"],
                    "R_LIBS": os.environ.get(
                        "MCP_CONSOLE_TEST_SSH_R_LIBS",
                        preinstalled_libraries,
                    ),
                    "R_PROFILE_USER": os.devnull,
                    "RETICULATE_PYTHON": sys.executable,
                    "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "1",
                    "MCP_CONSOLE_MANAGED_PYTHON": "must-not-activate",
                }
            },
        )
        # The remote project cannot supply policy, including during restart.
        configure(remote, remote, ["must-not-launch"], sandbox={"network": "invalid"})
        with localhost(root / "sshd") as environment:
            trap = root / "discovery-called"
            for name in ("R", "Rscript", "uv", "uvx", "ir", "python", "python3"):
                executable = root / "sshd" / name
                executable.write_text(
                    code("""
                        #!/bin/sh
                        printf called >> TRAP
                        exit 93
                        """).replace("TRAP", shlex.quote(str(trap)))
                )
                executable.chmod(0o755)
            environment["PATH"] = str(root / "sshd")
            environment.update(
                {
                    "R_HOME": "/controller-must-not-discover-R",
                    "RETICULATE_PYTHON": "/controller-must-not-select-python",
                }
            )
            with McpClient(binary, execution.serve(), environment, local) as client:
                client.initialize_and_list_tools()
                send = client.transcript[-1]["result"]["tools"][0]
                assert send["inputSchema"]["properties"]["requirements"]["properties"][
                    "action"
                ]["enum"] == ["get", "add", "set", "reset"], send
                assert "console-test" in send["description"], send
                client.send(
                    # fmt: r
                    r=code("""
                        x <- 41
                        x + 1
                        """)
                )
                assert last_result_text(client) == "[1] 42\n", last_result_text(client)
                session = next((local / ".agents/console/sessions").iterdir())
                initial_quarto = (session / "transcript.qmd").read_text()
                client.send(
                    # fmt: r
                    r=code("""
                        stopifnot(
                          getwd() == REMOTE_WORKSPACE,
                          Sys.getenv("CONSOLE_SSH_LITERAL") == "remote environment value"
                        )
                        x
                        """).replace("REMOTE_WORKSPACE", json.dumps(str(remote)))
                )
                assert last_result_text(client) == "[1] 41\n", last_result_text(client)
                client.send(
                    r="x <- -1",
                    control="restart",
                    requirements={"r": ["praise"]},
                    stdin="unwanted\n",
                )
                assert (
                    "dynamic environment resolution is unavailable"
                    in last_result_text(client)
                )
                client.send(r="x")
                assert last_result_text(client) == "[1] 41\n", last_result_text(client)
                client.send(
                    # fmt: python
                    python=code("""
                        import os
                        import sys

                        answer = 42
                        print(answer)
                        print(sys.executable == os.environ["RETICULATE_PYTHON"])
                        """)
                )
                assert "42\nTrue\n" in last_result_text(client), last_result_text(
                    client
                )
                with closing(
                    FifoCheckpoint.create(remote / "raw-output-release")
                ) as release:
                    # Raw stdout and completion use independent transports. Keep
                    # the cell running until the MCP response proves receipt.
                    wait_for_evaluation_output(
                        client,
                        "raw output\n\n[running; poll with an empty send]",
                        "remote raw stdout",
                        # fmt: python
                        python=code(r"""
                            _ = os.write(1, b"raw output\n")
                            with open("raw-output-release", "rb", buffering=0) as gate:
                                assert gate.read(1) == b"1"
                            """),
                        timeout_ms=1,
                    )
                    release.release()
                    wait_for_evaluation_output(
                        client, "[done]", "remote raw output completion"
                    )
                client.send(
                    # fmt: python
                    python=code("""
                        try:
                            import mcpConsoleDefinitelyMissingPackage
                        except ModuleNotFoundError as error:
                            print(error)
                        """).rstrip()
                )
                assert (
                    "dynamic environment resolution is unavailable"
                    in last_result_text(client)
                ), last_result_text(client)
                client.send(sql="SELECT 6 * 7 AS answer")
                assert "42" in last_result_text(client), last_result_text(client)
                client.send(python="print(input('remote prompt: '))")
                assert "[waiting for stdin]" in last_result_text(client), (
                    last_result_text(client)
                )
                wait_for_evaluation_output(
                    client,
                    "interactive value\n",
                    "remote interactive input",
                    stdin="interactive value\n",
                )
                plotted = client.send(r="plot(1:3)")
                images = [
                    part for part in plotted["content"] if part["type"] == "image"
                ]
                assert len(images) == 1, plotted
                image_bytes = base64.b64decode(images[0]["data"])
                session = next((local / ".agents/console/sessions").iterdir())
                artifact = next((session / "artifacts").iterdir())
                assert_result_content(
                    client,
                    [artifact.read_bytes()],
                    image_reference="local recording artifact",
                )
                with closing(FifoCheckpoint.create(remote / "loop-started")) as started:
                    client.send(
                        # fmt: r
                        r=code(r"""
                            local({
                              checkpoint <- fifo("loop-started", open = "wb", blocking = TRUE)
                              writeBin(charToRaw("1"), checkpoint)
                              close(checkpoint)
                              repeat {
                                Sys.sleep(60)
                              }
                            })
                            """),
                        timeout_ms=1,
                    )
                    assert "[running;" in last_result_text(client), last_result_text(
                        client
                    )
                    # Running acknowledges admission; the FIFO proves execution
                    # has reached the remote worker before the interrupt.
                    started.wait("remote R loop started")
                    wait_for_evaluation_output(
                        client, "\n", "remote R interrupt", control="interrupt"
                    )
                client.send(r="x")
                assert last_result_text(client).endswith("[1] 41\n"), last_result_text(
                    client
                )
                config.write_text("invalid: [")
                client.send(control="restart")
                client.send(r="exists('x')")
                assert last_result_text(client) == "[1] FALSE\n", last_result_text(
                    client
                )
                client.send(r="quit(save='no', status=23)")
                client.send(r="exists('x')")
                assert last_result_text(client).endswith("[1] FALSE\n"), (
                    last_result_text(client)
                )
                transcript = client.finish()
        assert not trap.exists(), "controller runtime discovery was invoked"
        assert not (remote / ".agents/console/sessions").exists()
        session = next((local / ".agents/console/sessions").iterdir())
        event = json.loads(
            (session / "internal/events.jsonl").read_text().splitlines()[0]
        )
        assert event["working_directory"] == str(local), event
        assert event["target"]["workspace"] == str(remote), event
        assert event["target"]["transport"] == {
            "kind": "ssh",
            "host": "console-test",
        }, event
        artifacts = list((session / "artifacts").iterdir())
        assert len(artifacts) == 1, artifacts
        assert artifacts[0].read_bytes() == image_bytes
        qmd = (session / "transcript.qmd").read_text()
        frontmatter = qmd.split("---", 2)[1]
        assert "root.dir" not in qmd, qmd
        assert "execute:" not in frontmatter, qmd
        assert "# Run `ir render transcript.qmd`" in frontmatter, qmd
        # Keep literal wire output; only the incidental temporary paths vary.
        return TranscriptWithCompanions(
            transcript=json.loads(
                json.dumps(transcript).replace(str(root), "<ssh-test>")
            ),
            companions={"qmd": initial_quarto.replace(str(root), "<ssh-test>")},
        )


@requires(SSH, WORKER)
def test_preinstalled_remote_runtime(binary: Path) -> TranscriptWithCompanions:
    return _preinstalled_remote_runtime(binary, DIRECT)


@requires(SSH, WORKER, SANDBOX)
def test_preinstalled_sandbox(binary: Path) -> TranscriptWithCompanions:
    return _preinstalled_remote_runtime(binary, SANDBOXED)


def _peer(binary: Path, mode: str, callback: str = "resolve_r") -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        configure(root, root, [str(binary)])
        log = root / "calls"
        environment = peer_environment(root, mode)
        environment["CONSOLE_SSH_CALLBACK"] = callback
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(r="one_cell_only <- TRUE")
            result = last_result_text(client)
            expected = {
                "auth": "unconfirmed",
                "stdout": "unexpected stdout",
                "incompatible": "incompatible SSH bootstrap",
                "prior-bootstrap-protocol": "expected protocol 10",
                "lost": "unconfirmed",
                "resolver": "dynamic environment resolution is unavailable",
            }[mode]
            assert expected in result, result
            if mode == "resolver":
                message = "dynamic environment resolution is unavailable"
                if callback == "resolve_r":
                    message += "; install `ir` or `uv` and restart MCP Console"
                assert result == message + "\n", result
            if mode != "resolver":
                client.send(r="must_not_replay <- TRUE")
                client.send(control="restart", r="must_not_replace <- TRUE")
                assert log.read_text().count("launched\n") == 1, log.read_text()
                assert "must_not_" not in log.read_text(), log.read_text()
            # Transport failure may make orderly session shutdown fail too.
            client.stdin.close()
            client.stdout.read(timeout=12)
            stderr = client.stderr.read(timeout=12)
            client.process.wait(timeout=12)
            if mode == "auth":
                assert "Permission denied (publickey)" in stderr, stderr
            if mode == "resolver":
                assert client.process.returncode == 0 and not stderr, stderr
            return client.transcript[3:] + [{"standard_error": stderr}]


def test_diagnostic_producers_keep_separate_utf8_decoders(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        configure(root, root, [str(binary)])
        with (
            closing(FifoCheckpoint.create(root / "discovery-release")) as discovery,
            closing(FifoCheckpoint.create(root / "diagnostic-release")) as diagnostic,
            closing(FifoCheckpoint.create(root / "evaluation-started")) as evaluating,
            McpClient(
                binary,
                DIRECT.serve(),
                peer_environment(root, "diagnostic-overlap"),
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                "producer prefix \n[running; poll with an empty send]",
                "partial diagnostic scalar ingested during discovery",
                r="hold_first_cell",
                timeout_ms=0,
            )
            discovery.release()
            evaluating.wait("first worker accepted the cell")
            client.send(control="restart", r="hold_replacement_cell", timeout_ms=0)
            evaluating.wait("old launcher retired before replacement evaluation")
            diagnostic.release()
            wait_for_evaluation_output(
                client,
                "α diagnostic complete\n\n[running; poll with an empty send]",
                "preparation scalar survived another diagnostic producer's exit",
                timeout_ms=0,
            )
            client.finish()
            (session,) = (root / ".agents/console/sessions").iterdir()
            # The first bytes precede evaluation; the remainder arrives while
            # the replacement cell owns output. Both raw files retain bytes.
            paths = [
                session / "outputs/session.log",
                *sorted((session / "outputs").glob("call-*.log")),
            ]
            raw = b"".join(path.read_bytes() for path in paths)
            assert raw == "producer prefix α diagnostic complete\n".encode(), raw
            return [{"diagnostic_utf8_survives_overlapping_producer_exit": True}]


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_diagnostic_close_preserves_another_producers_progress(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        configure(root, root, [str(binary)])
        environment = peer_environment(root, "diagnostic-terminal-overlap")
        environment[LOADER_VARIABLE] = str(
            build_interposer(root, "diagnostic_reader_exit")
        )
        environment["MCP_CONSOLE_TEST_DIAGNOSTIC_EXIT"] = str(
            root / "diagnostic-exited"
        )
        with (
            closing(FifoCheckpoint.create(root / "diagnostic-start")) as started,
            closing(FifoCheckpoint.create(root / "diagnostic-finish")) as finished,
            closing(FifoCheckpoint.create(root / "diagnostic-close")) as close,
            closing(FifoCheckpoint.create(root / "diagnostic-exited")) as exited,
            closing(FifoCheckpoint.create(root / "evaluation-finish")) as evaluation,
            McpClient(binary, DIRECT.serve(), environment, root) as client,
        ):
            try:
                client.initialize_and_list_tools()
                wait_for_evaluation_output(
                    client,
                    "diagnostic reader ready\n\n[running; poll with an empty send]",
                    "launcher diagnostic reader identified",
                    r="hold_cell",
                    timeout_ms=0,
                )
                (session,) = (root / ".agents/console/sessions").iterdir()
                raw = session / "outputs/call-000001.log"
                with Events() as events:
                    events.watch_file(raw)
                    for gate, expected in (
                        (started, b"diagnostic reader ready\nprogress 1\r"),
                        (
                            finished,
                            b"diagnostic reader ready\nprogress 1\rprogress 2\n",
                        ),
                    ):
                        gate.release()
                        deadline = time.monotonic() + 10
                        while raw.stat().st_size < len(expected):
                            remaining = deadline - time.monotonic()
                            assert remaining > 0 and events.wait(remaining), (
                                "diagnostic bytes not captured"
                            )
                        assert raw.read_bytes() == expected
                        if gate is started:
                            close.release()
                            # The thread's TLS destructor runs after its EOF
                            # publication, before the other producer continues.
                            exited.wait("launcher diagnostics closed")
                evaluation.release()
                client.expect("progress 2\n")
                client.finish()
                return [{"diagnostic_close_preserves_progress_replacement": True}]
            finally:
                started.release()
                finished.release()
                close.release()
                evaluation.release()


def test_unexpected_stdout(binary: Path) -> Transcript:
    return _peer(binary, "stdout")


def test_incompatible_remote_build(binary: Path) -> Transcript:
    return _peer(binary, "incompatible")


def test_rejects_remote_without_interpreter_bootstrap_protocol(
    binary: Path,
) -> Transcript:
    return _peer(binary, "prior-bootstrap-protocol")


def test_authentication_failure(binary: Path) -> Transcript:
    return _peer(binary, "auth")


def test_transport_loss_blocks_replacement(binary: Path) -> Transcript:
    return _peer(binary, "lost")


def test_remote_r_callback_cannot_run_local_resolvers(binary: Path) -> Transcript:
    return _peer(binary, "resolver")


def test_remote_python_callback_cannot_run_local_resolvers(binary: Path) -> Transcript:
    return _peer(binary, "resolver", "resolve_python")


def test_remote_python_version_callback_cannot_run_local_resolvers(
    binary: Path,
) -> Transcript:
    return _peer(binary, "resolver", "resolve_python_version")


if __name__ == "__main__":
    run_this_suite(__file__)
