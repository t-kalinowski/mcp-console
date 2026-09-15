"""A private localhost OpenSSH server with pinned test keys and a host alias."""

import getpass
import json
import os
import shlex
import shutil
import socket
import subprocess
import struct
import sys
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


def bootstrap(
    binary: Path, workspace: Path, *, policy=None, no_sandbox=False, **values
) -> bytes:
    version = subprocess.check_output([binary, "--version"], text=True).split()[1]
    body = json.dumps(
        {
            "version": 4,
            "provider": "native",
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
    workspace: Path, remote: Path, prefix: list[str] | None, **policy: object
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


def peer_environment(root: Path, mode: str) -> dict[str, str]:
    peer = Path(__file__).resolve().parents[1] / "fixtures/ssh_peer.py"
    ssh = root / "ssh"
    ssh.write_text(
        code(r"""
            #!/bin/sh
            exec COMMAND "$@"
            """).replace("COMMAND", shlex.join([sys.executable, str(peer)]))
    )
    ssh.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(root) + os.pathsep + os.environ["PATH"],
        "CONSOLE_SSH_PEER": mode,
        "CONSOLE_SSH_PEER_LOG": str(root / "calls"),
    }


@contextmanager
def localhost(root: Path, *, remote_path: Path | None = None):
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
    environment = client_environment(
        root, config=str(client_config), remote_path=remote_path
    )
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
        yield environment
    finally:
        process.terminate()
        process.wait(timeout=10)
        reader.close()


def client_environment(
    root: Path, *, config: str | None = None, remote_path: str | Path | None = None
) -> dict[str, str]:
    # Preserve real OpenSSH transport, options, and remote-shell quoting.
    executable = shutil.which("ssh")
    assert executable is not None
    command = [executable, *(["-F", config] if config else [])]
    launcher = root / "ssh"
    launcher.write_text(
        code(r"""
            #!/bin/sh
            exec COMMAND "$@"
            """).replace("COMMAND", shlex.join(command))
    )
    launcher.chmod(0o755)
    if remote_path is not None:
        # Keep the real SSH connection and remote shell, but give command
        # discovery a controlled PATH independent of account startup files.
        # fmt: python
        launcher.write_text(
            f"#!{sys.executable}\n"
            + code("""
                import os
                import shlex
                import sys

                command, remote_path = CONFIGURATION
                arguments = sys.argv[1:]
                arguments[-1] = shlex.join([
                    "/usr/bin/env", "PATH=" + remote_path,
                    "/bin/sh", "-c", arguments[-1],
                ])
                os.execv(command[0], [*command, *arguments])
                """).replace("CONFIGURATION", repr((command, str(remote_path))))
        )
    return {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}


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
