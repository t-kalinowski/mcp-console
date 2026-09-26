"""Isolated configuration and proxy inputs for native sandbox cases."""

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from support.normalization import code
from support.r import r_test_environment

NATIVE_PROXY = {
    "enabled": True,
    "enableSocks5": True,
    "enableSocks5Udp": False,
    "allowUpstreamProxy": False,
    "dangerouslyAllowAllUnixSockets": False,
    "mode": "full",
    "allowLocalBinding": False,
}


def isolated_home_environment(home: str) -> dict[str, str]:
    environment = os.environ.copy()
    # Match the server's R selection; Python-only hosts must not invoke R.
    if "R_HOME" in environment or shutil.which("R") is not None:
        environment, rscript = r_test_environment()
        # Preserve the host libraries before changing HOME for config discovery.
        environment["R_LIBS"] = subprocess.run(
            [
                rscript,
                "--vanilla",
                "-e",
                # fmt: r
                code("""
                    cat(paste(.libPaths(), collapse = .Platform$path.sep))
                    """),
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    environment["HOME"] = home
    return environment


@contextmanager
def host_tcp_ports() -> Iterator[list[int]]:
    # The Linux sandbox has up to two loopback proxy listeners in its own network
    # namespace. Three host listeners leave a port free of either proxy endpoint.
    with ExitStack() as stack:
        listeners = [stack.enter_context(socket.socket()) for _ in range(3)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
        yield [listener.getsockname()[1] for listener in listeners]
