#!/usr/bin/env -S uv run --script

import fcntl
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, code, run_this_suite, stop_client

PLATFORMS = {"darwin"}


def test_builtin_worker_closes_unlisted_server_descriptors(
    binary: Path,
) -> Transcript:
    # fmt: python
    launcher = code(r"""
        import os
        import resource
        import sys

        _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, hard_limit))
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    # fmt: python
    source = code(r"""
        import errno
        import os

        descriptor = int(os.environ["MCP_CONSOLE_TEST_INHERITED_FD"])
        try:
            os.write(descriptor, b"escaped")
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise RuntimeError("unlisted server descriptor reached the worker")

        print("closed")
        """)

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        host_path = temporary / "host.txt"
        host_path.write_bytes(b"")
        environment = os.environ.copy()
        with host_path.open("ab", buffering=0) as stream:
            descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 64)
            os.set_inheritable(descriptor, True)
            environment["MCP_CONSOLE_TEST_INHERITED_FD"] = str(descriptor)
            client = McpClient(
                Path(sys.executable),
                ("-c", launcher, str(binary), "serve"),
                environment,
                current_directory=temporary,
                pass_fds=(descriptor,),
            )
            passed = False
            try:
                client._initialize_and_list_tools()
                result = client.send(python=source)
                assert result == {
                    "content": [{"type": "text", "text": "closed\n"}],
                    "isError": False,
                }, result
                transcript = client._finish()
                passed = True
            finally:
                if not passed:
                    stop_client(client)
                os.close(descriptor)

        assert host_path.read_bytes() == b""
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
