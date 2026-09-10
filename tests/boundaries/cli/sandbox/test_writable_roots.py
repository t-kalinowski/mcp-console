#!/usr/bin/env -S uv run --script

import os
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_writable_roots_augment_default_permissions(binary: Path) -> Transcript:
    script = code(r"""
        import errno
        import os
        from pathlib import Path
        import socket
        import subprocess
        import sys

        host = Path.cwd()
        temporary = Path(os.environ["TMPDIR"])
        assert not host.is_relative_to(temporary)
        assert not temporary.is_relative_to(host)
        (temporary / "private").write_text("still writable")
        print("private storage remains writable")

        def denied(path: Path) -> None:
            try:
                path.write_text("must not be written")
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                print(f"write denied: {path.relative_to(host)}")
            else:
                raise AssertionError(f"unexpected write: {path}")

        for name in ("output café 雪", "cache"):
            root = host / name
            if sys.argv[1] == "denied":
                denied(root / "created")
                continue
            created = root / "created"
            created.write_text("created")
            assert created.read_text() == "created"
            existing = root / "existing"
            assert existing.read_text() == "original"
            existing.write_text("modified")
            assert existing.read_text() == "modified"
            created.unlink()
            assert not created.exists()
            subprocess.run(["touch", str(root / "child")], check=True)
            assert (root / "child").is_file()
            print(f"create, modify, delete, subprocess write allowed: {name}")

        denied(host / "parent-write")
        denied(host / "neighbor" / "created")
        denied(host / "output café 雪" / "escape" / "created")
        try:
            socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=2)
        except OSError:
            print("network remains denied")
        else:
            raise AssertionError("network was allowed")
        """)
    transcript = []
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory).resolve()
        roots = [host / "output café 雪", host / "cache"]
        for root in [*roots, host / "neighbor"]:
            root.mkdir()
            (root / "existing").write_text("original")
        (roots[0] / "escape").symlink_to(host / "neighbor", target_is_directory=True)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        for allowed in (False, True):
            # Exercise one relative and one absolute argument, each kept whole.
            options = (
                ["--writable-root", roots[0].name, "--writable-root", str(roots[1])]
                if allowed
                else []
            )
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    *options,
                    "--",
                    sys.executable,
                    "-c",
                    script,
                    "allowed" if allowed else "denied",
                    str(listener.getsockname()[1]),
                ],
                cwd=host,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
            assert result.stderr == ""
            for root in roots:
                assert root.is_dir(), "retirement removed a user directory"
                assert (root / "existing").read_text() == (
                    "modified" if allowed else "original"
                )
                assert (root / "child").exists() == allowed
            assert (host / "neighbor" / "existing").read_text() == "original"
            assert not (host / "neighbor" / "created").exists()
            assert not (host / "parent-write").exists()
            transcript.append(
                {
                    "writable_roots": [root.name for root in roots] if allowed else [],
                    "stdout": result.stdout,
                }
            )
    return transcript


@requires(SANDBOX)
def test_rejects_invalid_writable_roots_before_starting(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory)
        (host / "file").write_text("not a directory")
        for command in ("serve", "sandbox"):
            for root in ("missing", "file"):
                arguments = [command, "--writable-root", ".", "--writable-root", root]
                if command == "sandbox":
                    arguments += ["--", "/bin/echo", "workload started"]
                result = subprocess.run(
                    [binary, *arguments],
                    cwd=host,
                    input="",
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 1, result.stderr
                assert result.stdout == ""
                assert f"writable root '{root}'" in result.stderr
                assert not (host / "missing").exists()
                transcript.append(
                    {
                        "arguments": arguments,
                        "exit_code": result.returncode,
                        "stderr": result.stderr,
                    }
                )
    return transcript


@requires(SANDBOX)
def test_rejects_non_utf8_writable_roots_before_starting(binary: Path) -> Transcript:
    transcript = []
    for command in (b"serve", b"sandbox"):
        arguments = [command, b"--writable-root", b"non-utf8-\xff"]
        if command == b"sandbox":
            arguments += [b"--", b"/bin/echo", b"workload started"]
        result = subprocess.run(
            [os.fsencode(binary), *arguments],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, result.stderr
        assert result.stdout == ""
        assert result.stderr == "writable root 'non-utf8-�' is not valid UTF-8\n", (
            result.stderr
        )
        transcript.append(
            {
                "arguments_bytes": repr(arguments),
                "exit_code": result.returncode,
                "stderr": result.stderr,
            }
        )
    return transcript


def test_rejects_conflicting_writable_root_options(binary: Path) -> Transcript:
    transcript = []
    for arguments in (
        ["serve", "--no-sandbox", "--writable-root", "."],
        [
            "sandbox",
            "--config-env",
            "POLICY",
            "--writable-root",
            ".",
            "--",
            "/bin/echo",
            "workload started",
        ],
    ):
        result = subprocess.run(
            [binary, *arguments],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        assert "cannot be used with" in result.stderr
        assert "--writable-root" in result.stderr
        transcript.append(
            {
                "arguments": arguments,
                "exit_code": result.returncode,
                "stderr": result.stderr,
            }
        )
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
