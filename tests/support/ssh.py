"""A private localhost OpenSSH server with pinned test keys and a host alias."""

import getpass
import json
import os
import shlex
import shutil
import socket
import subprocess
import struct
from contextlib import contextmanager
from pathlib import Path
import select
import time

from support.client import TextReader
from support.normalization import code
from support.requirements import Requirement


SSHD = shutil.which("sshd") or (
    "/usr/sbin/sshd" if Path("/usr/sbin/sshd").is_file() else None
)
SSH = Requirement(
    "localhost OpenSSH",
    SSHD is not None and shutil.which("ssh-keygen") is not None,
    "requires sshd and ssh-keygen with permission to run a localhost SSH server",
)
CONFIG = ".agents/console/config.yaml"
EXTERNAL_SSH = Requirement(
    "configured external SSH target",
    bool(os.environ.get("MCP_CONSOLE_TEST_SSH_EXTERNAL")),
    "set MCP_CONSOLE_TEST_SSH_EXTERNAL to a provisioned test target JSON object",
)


def bootstrap(
    binary: Path, workspace: Path, *, policy=None, no_sandbox=False, **values
) -> bytes:
    version = subprocess.check_output([binary, "--version"], text=True).split()[1]
    body = json.dumps(
        {
            "version": 2,
            "build": version,
            "workspace": str(workspace),
            "policy": policy or {},
            "no_sandbox": no_sandbox,
            "writable_roots": [],
            **values,
        }
    ).encode()
    return struct.pack(">I", len(body)) + body


def read_exact(stream, length: int, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < length:
        remaining = deadline - time.monotonic()
        assert remaining > 0 and select.select([stream], [], [], remaining)[0], (
            "SSH frame timed out"
        )
        chunk = os.read(stream.fileno(), length - len(result))
        assert chunk, "truncated SSH frame"
        result.extend(chunk)
    return bytes(result)


def read_frame(stream, timeout: float = 15) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    tag, length = struct.unpack(">BI", read_exact(stream, 5, deadline))
    assert length <= 65536, length
    return tag, read_exact(stream, length, deadline)


def configure(
    workspace: Path, remote: Path, prefix: list[str], **policy: object
) -> Path:
    config = workspace / CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "target": {
                    "transport": {"kind": "ssh", "host": "console-test"},
                    "workspace": str(remote),
                    "command": prefix,
                },
                **policy,
            }
        )
    )
    return config


@contextmanager
def localhost(root: Path):
    assert SSHD is not None
    root.mkdir()
    for name in ("host", "client"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)],
            check=True,
            capture_output=True,
        )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    (root / "authorized_keys").write_bytes((root / "client.pub").read_bytes())
    (root / "known_hosts").write_text(
        f"[127.0.0.1]:{port} " + (root / "host.pub").read_text()
    )
    server_config = root / "sshd_config"
    server_config.write_text(
        f"""Port {port}
ListenAddress 127.0.0.1
HostKey {root}/host
PidFile {root}/pid
AuthorizedKeysFile {root}/authorized_keys
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
LogLevel VERBOSE
"""
    )
    client_config = root / "ssh_config"
    client_config.write_text(
        f"""Host console-test
  HostName 127.0.0.1
  Port {port}
  User {getpass.getuser()}
  IdentityFile {root}/client
  IdentitiesOnly yes
  UserKnownHostsFile {root}/known_hosts
  StrictHostKeyChecking yes
  ControlPath {root}/control
"""
    )
    # Only supply a test configuration file. All transport and quoting are real
    # OpenSSH, including the production-selected options and destination alias.
    executable = shutil.which("ssh")
    assert executable is not None
    launcher = root / "ssh"
    launcher.write_text(
        code(r"""
            #!/bin/sh
            exec COMMAND "$@"
            """).replace("COMMAND", shlex.join([executable, "-F", str(client_config)]))
    )
    launcher.chmod(0o755)
    process = subprocess.Popen(
        [SSHD, "-D", "-e", "-f", str(server_config)],
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stderr is not None
    reader = TextReader(process.stderr)
    try:
        line = reader.readline(timeout=10)
        assert "Server listening on" in line, line
        yield {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
    finally:
        process.terminate()
        process.wait(timeout=10)
        reader.close()


def remote_command(root: Path, binary: Path, environment: dict[str, str]) -> list[str]:
    prefix = root / "remote-console"
    prefix.write_text(
        "#!/bin/sh\nexec "
        + shlex.join(
            [
                "/usr/bin/env",
                "-i",
                *(f"{name}={value}" for name, value in environment.items()),
                str(binary),
            ]
        )
        + ' "$@"\n'
    )
    prefix.chmod(0o755)
    return [str(prefix)]


def poison_controller(root: Path, environment: dict[str, str]) -> Path:
    trap = root / "controller-discovery"
    for name in ("R", "Rscript", "uv", "uvx", "ir", "python", "python3"):
        executable = root / name
        executable.write_text(
            code("""
                #!/bin/sh
                printf called >> TRAP
                exit 93
                """).replace("TRAP", shlex.quote(str(trap)))
        )
        executable.chmod(0o755)
    environment.update(
        {
            "PATH": str(root),
            "R_HOME": "/controller-must-not-discover-R",
            "RETICULATE_PYTHON": "/controller-must-not-select-python",
            "IR_CACHE_DIR": str(root / "controller-ir"),
            "UV_CACHE_DIR": str(root / "controller-uv"),
        }
    )
    return trap
