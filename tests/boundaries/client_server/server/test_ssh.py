#!/usr/bin/env -S uv run --script

import base64
import json
import os
import shlex
import subprocess
import sys
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
from support.execution import DIRECT, SANDBOXED, Execution
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.r import r_test_environment
from support.requirements import SANDBOX, WORKER, requires
from support.ssh import SSH, configure, localhost, peer_environment
from support.suites import run_this_suite


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
                ]["enum"] == ["get"], send
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


def test_unexpected_stdout(binary: Path) -> Transcript:
    return _peer(binary, "stdout")


def test_incompatible_remote_build(binary: Path) -> Transcript:
    return _peer(binary, "incompatible")


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
