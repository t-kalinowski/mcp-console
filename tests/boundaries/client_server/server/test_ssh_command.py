#!/usr/bin/env -S uv run --script

import shlex
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import last_result_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.r import r_test_environment
from support.requirements import requires
from support.ssh import SSH, configure, localhost
from support.suites import run_this_suite


@requires(SSH)
def test_command_selection_and_ordinary_failures(
    binary: Path,
) -> TranscriptWithCompanions:
    records = []
    missing = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        commands = root / "commands"
        commands.mkdir()
        (commands / "sh").symlink_to("/bin/sh")
        log = root / "calls"
        for name in ("mcp-console", "uvx", "command space ' ; $()"):
            executable = commands / name
            executable.write_text(
                code(r"""
                    #!/bin/sh
                    printf '%s\n' NAME "$@" >> LOG
                    printf '%s\n' 'selected command failed' >&2
                    exit 42
                    """)
                .replace("NAME", shlex.quote(name))
                .replace("LOG", shlex.quote(str(log)))
            )
            executable.chmod(0o755)
        cases = (
            (
                "configured",
                [str(commands / "command space ' ; $()"), "literal ' ; $HOME"],
                ["command space ' ; $()", "literal ' ; $HOME", "ssh-prepare"],
            ),
            ("configured missing", [str(commands / "absent")], []),
            ("PATH", None, ["mcp-console", "ssh-prepare"]),
            ("fallback", None, ["uvx", "mcp-console", "ssh-prepare"]),
            ("missing", None, []),
        )
        with localhost(root / "sshd", remote_path=commands) as environment:
            for label, prefix, expected in cases:
                if label == "fallback":
                    (commands / "mcp-console").unlink()
                if label == "missing":
                    (commands / "uvx").unlink()
                configure(root, root, prefix)
                log.write_text("")
                with McpClient(binary, ("serve",), environment, root) as client:
                    assert client.process.wait(timeout=12) != 0
                    assert not client.stdout.read()
                    errors = client.stderr.read()
                    if expected:
                        assert "selected command failed" in errors, errors
                    else:
                        assert (
                            "not found" in errors
                            or "No such file or directory" in errors
                        ), errors
                    assert log.read_text().splitlines() == expected, log.read_text()
                    entry = {
                        "selection": label,
                        "arguments": expected,
                        "standard_error": errors.replace(str(root), "<ssh-test>"),
                    }
                    (records if expected else missing).append(entry)
    return TranscriptWithCompanions(records, {f"{sys.platform}.yaml": missing})


@requires(SSH)
@executions(DIRECT, SANDBOXED)
def test_default_command_launches_remote_worker(
    binary: Path, execution: Execution
) -> Transcript:
    remote_environment, _ = r_test_environment()
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        commands = root / "commands"
        commands.mkdir()
        (commands / "sh").symlink_to("/bin/sh")
        log = root / "calls"
        installed = commands / "mcp-console"
        installed.write_text(
            code(r"""
                #!/bin/sh
                printf '%s\n' "$@" >> LOG
                exec /usr/bin/env -i PATH=/usr/bin:/bin R_HOME=RHOME R_LIBS_USER=/unavailable R_LIBS_SITE=/unavailable EXECUTABLE "$@"
                """)
            .replace("LOG", shlex.quote(str(log)))
            .replace("RHOME", shlex.quote(remote_environment["R_HOME"]))
            .replace("EXECUTABLE", shlex.quote(str(binary)))
        )
        installed.chmod(0o755)
        uvx = commands / "uvx"
        uvx.write_text(
            code(r"""
                #!/bin/sh
                [ "$1" = mcp-console ] || exit 23
                shift
                exec EXECUTABLE "$@"
                """).replace("EXECUTABLE", shlex.quote(str(commands / "installed")))
        )
        uvx.chmod(0o755)
        configure(root, root, None)
        with localhost(root / "sshd", remote_path=commands) as environment:
            for selection in ("PATH", "fallback"):
                if selection == "fallback":
                    installed.rename(commands / "installed")
                log.write_text("")
                with McpClient(binary, execution.serve(), environment, root) as client:
                    client.initialize_and_list_tools()
                    client.send(r="6 * 7")
                    assert last_result_text(client) == "[1] 42\n", last_result_text(
                        client
                    )
                    records.append({"selection": selection})
                    records.extend(client.finish()[3:])
                assert log.read_text().splitlines() == ["ssh-prepare", "ssh-launch"], (
                    log.read_text()
                )
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
