#!/usr/bin/env -S uv run --script

import errno
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import LANDLOCK, MACOS_SANDBOX, SANDBOX, requires
from support.suites import run_this_suite


def configuration() -> dict:
    return {
        "version": 2,
        "filesystem": {
            "kind": "restricted",
            "entries": [
                {
                    "path": {"type": "special", "value": {"kind": "root"}},
                    "access": "read",
                }
            ],
        },
        "network": "restricted",
    }


def invoke(binary: Path, config: dict | str, *command: str, **kwargs):
    return subprocess.run(
        [binary, "sandbox", "--config-env", "TEST_POLICY", "--", *command],
        env={
            **os.environ,
            "TEST_POLICY": config if isinstance(config, str) else json.dumps(config),
            "MCP_CONSOLE_SANDBOX_CONFIG": "unselected ambient value",
            "INHERITED": "from launcher",
        },
        capture_output=True,
        text=True,
        **kwargs,
    )


@requires(SANDBOX)
def test_explicit_policy_controls_filesystem_and_network(binary: Path) -> Transcript:
    script = code(r"""
        import errno
        import pathlib
        import socket
        import sys

        try:
            pathlib.Path("created").write_text("allowed", encoding="utf-8")
            print("write allowed")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            print("write denied")
        try:
            with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2):
                print("network allowed")
        except OSError:
            print("network denied")
        """)
    transcript = []
    with TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        config = configuration()
        for allowed in (False, True):
            if allowed:
                config["filesystem"]["entries"].append(
                    {
                        "path": {
                            "type": "path",
                            "path": str(Path(directory).resolve()),
                        },
                        "access": "write",
                    }
                )
                config["network"] = "enabled"
            result = invoke(
                binary,
                config,
                sys.executable,
                "-c",
                script,
                str(listener.getsockname()[1]),
                cwd=directory,
            )
            assert result.returncode == 0, result.stderr
            assert result.stderr == ""
            permission = "allowed" if allowed else "denied"
            assert result.stdout == f"write {permission}\nnetwork {permission}\n"
            assert (Path(directory) / "created").exists() == allowed
            transcript.append({"scenario": permission, "stdout": result.stdout})
    return transcript


@requires(SANDBOX)
def test_environment_overrides_and_arguments_are_literal(binary: Path) -> Transcript:
    value = "雪, café; 'quoted' \"double\" $() `literal` \\ newline\nend"
    script = code(r"""
        import json
        import os
        import sys

        assert "TEST_POLICY" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        print(json.dumps({
            "inherited": os.environ.get("INHERITED"),
            "value": os.environ["VALUE"],
            "arguments": sys.argv[1:],
        }, ensure_ascii=False))
        """)
    transcript = []
    for inherit in (True, False):
        config = configuration()
        config["inherit_environment"] = inherit
        config["environment"] = {
            "VALUE": value,
            "TEST_POLICY": "attempted reintroduction",
            "MCP_CONSOLE_SANDBOX_CONFIG": "attempted reintroduction",
        }
        arguments = [value, "--config-env", "ANOTHER_POLICY", "--bootstrap-fd", "3"]
        result = invoke(binary, config, sys.executable, "-c", script, *arguments)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert json.loads(result.stdout) == {
            "inherited": "from launcher" if inherit else None,
            "value": value,
            "arguments": arguments,
        }
        transcript.append({"inherit_environment": inherit, "stdout": result.stdout})
    return transcript


@requires(SANDBOX)
def test_rejects_malformed_and_conflicting_configuration(binary: Path) -> Transcript:
    transcript = []
    for name, value in (
        ("malformed JSON", "{"),
        ("file reference", "@config.json"),
        ("missing policy", "{}"),
        ("duplicate field", '{"version":2,"version":2}'),
        ("duplicate command", {**configuration(), "command": ["/bin/true"]}),
        ("duplicate cwd", {**configuration(), "cwd": "/"}),
        ("file include", {**configuration(), "include": ["policy.json"]}),
        (
            "transport export",
            {
                **configuration(),
                "lifecycle": {"private_tmp": {"environment": ["TEST_POLICY"]}},
            },
        ),
        (
            "invalid lifecycle",
            {**configuration(), "lifecycle": {"cleanup_timeout_ms": 0}},
        ),
        (
            "invalid environment",
            {**configuration(), "environment": "PRIVATE_ENVIRONMENT_SENTINEL"},
        ),
    ):
        result = invoke(binary, value, "/bin/echo", "executed")
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr
        assert "PRIVATE_ENVIRONMENT_SENTINEL" not in result.stderr
        transcript.append(
            {
                "scenario": name,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    for options in (
        ["--config-env", "ABSENT_POLICY"],
        ["--config-env", "TEST_POLICY", "--config-env", "SECOND_POLICY"],
        ["--config-env", "TEST_POLICY", "--exit-with-parent", str(os.getpid())],
    ):
        result = subprocess.run(
            [binary, "sandbox", *options, "--", "/bin/echo", "executed"],
            env={
                **os.environ,
                "TEST_POLICY": json.dumps(configuration()),
                "NO_COLOR": "1",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert result.stdout == ""
        transcript.append(
            {
                "scenario": " ".join(options[:3]),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    return transcript


@requires(SANDBOX)
def test_options_before_command_are_launcher_options(binary: Path) -> Transcript:
    transcript = []
    for option in ("--bootstrap-fd", "--config-file"):
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--config-env",
                "TEST_POLICY",
                option,
                "3",
                "--",
                "/bin/echo",
                "executed",
            ],
            env={
                **os.environ,
                "TEST_POLICY": json.dumps(configuration()),
                "NO_COLOR": "1",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        assert f"unexpected argument '{option}'" in result.stderr
        transcript.append(
            {"option": option, "exit_code": result.returncode, "stderr": result.stderr}
        )
    return transcript


@requires(SANDBOX)
def test_large_environment_configuration_preserves_stdin(binary: Path) -> Transcript:
    config = json.dumps(configuration()) + " " * 65536
    result = invoke(binary, config, "/bin/cat", input="independent stdin\n\x00雪\n")
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == "independent stdin\n\x00雪\n"
    # The OS rejects an oversized launch before frontend diagnostics can run.
    oversized = " " * (os.sysconf("SC_ARG_MAX") * 2)
    try:
        invoke(binary, oversized, "/bin/echo", "executed")
    except OSError as error:
        assert error.errno == errno.E2BIG
    else:
        raise AssertionError("oversized exec environment was accepted")
    return [
        {"configuration_padding_bytes": 65536, "stdout": result.stdout},
        {"oversized_launch_errno": "E2BIG"},
    ]


@requires(SANDBOX)
def test_target_cannot_replace_active_policy(binary: Path) -> Transcript:
    script = code(r"""
        import errno
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys

        forbidden = Path(sys.argv[2])
        policy = json.loads(sys.argv[3])
        os.environ["TEST_POLICY"] = json.dumps(policy)
        os.environ["MCP_CONSOLE_SANDBOX_CONFIG"] = json.dumps(policy)
        temporary = Path(os.environ["TMPDIR"])
        for name in ("config.json", "sandbox.json", ".mcp-console.json"):
            (temporary / name).write_text(json.dumps(policy), encoding="utf-8")
        os.chdir(temporary)
        print(json.dumps(policy))  # stdout is target data, never host policy input.
        try:
            forbidden.write_text("escaped", encoding="utf-8")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            print("active policy still denies writes")
        else:
            raise AssertionError("target environment or files changed policy")
        child = subprocess.run(
            [sys.argv[1], "sandbox", "--config-env", "TEST_POLICY", "--",
             sys.executable, "-c",
             "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('escaped')",
             str(forbidden)],
            env={**os.environ, "TEST_POLICY": json.dumps(policy)},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert child.returncode != 0
        assert child.stdout == b""
        assert not forbidden.exists()
        print("nested sandbox cannot grant host writes")
        """)
    with TemporaryDirectory() as directory:
        forbidden = Path(directory).resolve() / "forbidden"
        permissive = configuration()
        permissive["filesystem"]["entries"].append(
            {"path": {"type": "path", "path": str(forbidden.parent)}, "access": "write"}
        )
        permissive["network"] = "enabled"
        restricted = configuration()
        restricted["lifecycle"] = {"private_tmp": {"environment": ["TMPDIR"]}}
        result = invoke(
            binary,
            restricted,
            sys.executable,
            "-c",
            script,
            str(binary),
            str(forbidden),
            json.dumps(permissive),
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        expected = (
            json.dumps(permissive)
            + "\nactive policy still denies writes\nnested sandbox cannot grant host writes\n"
        )
        assert result.stdout == expected
        assert not forbidden.exists()
        return [
            {
                "stdout": result.stdout.replace(
                    str(forbidden.parent), "<host directory>"
                ),
                "transcript_normalization": {"host_directory": "omitted"},
            }
        ]


@requires(SANDBOX)
def test_default_sandbox_ignores_ambient_configuration(binary: Path) -> Transcript:
    result = subprocess.run(
        [
            binary,
            "sandbox",
            "--",
            sys.executable,
            "-c",
            "import os; assert 'MCP_CONSOLE_SANDBOX_CONFIG' not in os.environ; print('default policy')",
        ],
        env={**os.environ, "MCP_CONSOLE_SANDBOX_CONFIG": "malformed ambient policy"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "default policy\n"
    assert result.stderr == ""
    return [{"stdout": result.stdout}]


@requires(SANDBOX)
def test_launcher_marker_cannot_replace_selected_configuration(
    binary: Path,
) -> Transcript:
    config = configuration()
    config["environment"] = {"MCP_CONSOLE_SANDBOX": "attempted reintroduction"}
    result = subprocess.run(
        [
            binary,
            "sandbox",
            "--config-env",
            "MCP_CONSOLE_SANDBOX",
            "--",
            sys.executable,
            "-c",
            "import os; assert 'MCP_CONSOLE_SANDBOX' not in os.environ; print('selected transport removed')",
        ],
        env={**os.environ, "MCP_CONSOLE_SANDBOX": json.dumps(config)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "selected transport removed\n"
    assert result.stderr == ""
    return [{"stdout": result.stdout}]


@requires(SANDBOX)
def test_child_specific_shell_python_and_processx_examples(binary: Path) -> Transcript:
    examples = Path(__file__).resolve().parents[4] / "examples"
    transcript = []
    for runtime, suffix in (
        ("/bin/sh", "sh"),
        (sys.executable, "py"),
        ("Rscript", "R"),
    ):
        result = subprocess.run(
            [runtime, str(examples / f"sandbox-config.{suffix}"), str(binary)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert (
            result.stdout
            == 'value: café 雪, "quotes", $() and spaces\nliteral argument: * ; $(echo no)\n'
        )
        transcript.append(
            {"example": f"sandbox-config.{suffix}", "stdout": result.stdout}
        )
    return transcript


@requires(LANDLOCK)
def test_explicit_landlock_preserves_policy_and_rejects_supervised_lifetime(
    binary: Path,
) -> Transcript:
    config = configuration() | {"linux_backend": "landlock"}
    result = invoke(binary, config, "/bin/echo", "explicit Landlock")
    assert result.returncode == 0, result
    assert result.stderr == ""
    assert result.stdout == "explicit Landlock\n"
    transcript = [{"stdout": result.stdout}]
    with TemporaryDirectory() as directory:
        sentinel = Path(directory) / "sentinel"
        sentinel.write_text("synthetic sentinel")
        result = invoke(
            binary,
            config,
            sys.executable,
            "-c",
            code(r"""
            import os
            import sys
            try:
                os.truncate(sys.argv[1], 0)
            except PermissionError:
                print("truncate denied")
            else:
                raise AssertionError("host truncation succeeded")
            """),
            str(sentinel),
        )
        assert result.returncode == 0 and not result.stderr, result
        assert result.stdout == "truncate denied\n"
        assert sentinel.read_text() == "synthetic sentinel"
        transcript.append({"stdout": result.stdout})
    config["lifecycle"] = {"private_tmp": {"environment": ["TMPDIR"]}}
    result = invoke(binary, config, "/bin/echo", "must not run")
    assert result.returncode == 1 and not result.stdout, result
    assert "landlock does not provide supervised lifetime" in result.stderr
    transcript.append({"stderr": result.stderr, "exit_code": result.returncode})
    return transcript


@requires(MACOS_SANDBOX)
def test_linux_backend_is_rejected_on_macos(binary: Path) -> Transcript:
    result = invoke(
        binary,
        configuration() | {"linux_backend": "landlock"},
        "/bin/echo",
        "must not run",
    )
    assert result.returncode == 1 and not result.stdout, result
    assert "linux_backend is supported only on Linux" in result.stderr
    return [{"stderr": result.stderr, "exit_code": result.returncode}]


if __name__ == "__main__":
    run_this_suite(__file__)
