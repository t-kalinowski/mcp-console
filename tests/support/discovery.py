"""Gate public runtime discovery and observe its descendant's retirement."""

import os
import select
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from support.resolvers import bare_runtime_environment

if os.name == "nt":
    import _winapi


class DiscoveryGate:
    def __init__(self, root: Path, listener: socket.socket | None) -> None:
        self.root = root
        self.executable = root / ("R.exe" if os.name == "nt" else "R")
        self.listener = listener
        self.connection: socket.socket | None = None
        self.process: int | None = None
        self.released = False
        self.lifetime: int | None = None
        self.proceed: int | None = None
        if os.name != "nt":
            # The sandbox has its own loopback namespace. Filesystem FIFOs keep
            # this control gate independent of its network permissions.
            os.mkfifo(root / "lifetime")
            os.mkfifo(root / "release")
            self.lifetime = os.open(root / "lifetime", os.O_RDONLY | os.O_NONBLOCK)
            self.proceed = os.open(root / "release", os.O_RDWR | os.O_NONBLOCK)

    def wait(self, description: str) -> None:
        assert self.connection is None, description
        if os.name != "nt":
            readable, _, _ = select.select([self.lifetime], [], [], 10)
            assert readable, description
            assert len(os.read(self.lifetime, 4)) == 4, description
            return
        assert self.listener is not None
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(5)
        identity = b""
        while len(identity) < 4:
            chunk = self.connection.recv(4 - len(identity))
            assert chunk, description
            identity += chunk
        if os.name == "nt":
            # Pin the blocked descendant before release or cancellation. A killed
            # Windows process resets TCP, so socket EOF is not its exit receipt.
            self.process = _winapi.OpenProcess(
                _winapi.SYNCHRONIZE, False, int.from_bytes(identity, "big")
            )

    def release(self) -> None:
        if not self.released:
            if os.name == "nt":
                assert self.connection is not None
                self.connection.sendall(b"1")
            else:
                assert os.write(self.proceed, b"1") == 1
            self.released = True

    def assert_retired(self) -> None:
        if os.name == "nt":
            assert self.process is not None
            assert (
                _winapi.WaitForSingleObject(self.process, 5000) == _winapi.WAIT_OBJECT_0
            ), "discovery survived MCP closure"
        else:
            readable, _, _ = select.select([self.lifetime], [], [], 5)
            assert readable, "discovery survived MCP closure"
            assert os.read(self.lifetime, 1) == b"", "discovery survived MCP closure"

    def close(self) -> None:
        for descriptor in (self.lifetime, self.proceed):
            if descriptor is not None:
                os.close(descriptor)
        if self.connection is not None:
            self.connection.close()
        if self.process is not None:
            _winapi.CloseHandle(self.process)


@contextmanager
def discovery_environment(
    *, r_home: Path | None = None
) -> Iterator[tuple[dict[str, str], DiscoveryGate]]:
    with (
        tempfile.TemporaryDirectory(prefix="console discovery ") as temporary,
        closing(socket.socket()) if os.name == "nt" else nullcontext() as listener,
    ):
        root = Path(temporary)
        if listener is not None:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(10)
            (root / "address").write_text(f"127.0.0.1:{listener.getsockname()[1]}")
        (root / "failure").write_text("fixture R discovery failed")
        if r_home is not None:
            (root / "r-home").write_text(str(r_home), encoding="utf-8")
        with closing(DiscoveryGate(root, listener)) as gate:
            subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "fixtures/resolver/gated_r.rs"
                    ),
                    "-o",
                    str(gate.executable),
                ],
                check=True,
            )
            # Relocatable Unix Rscript launchers need these host utilities.
            if os.name != "nt":
                for name in ("sh", "dirname", "readlink"):
                    (root / name).symlink_to(shutil.which(name))
            environment = bare_runtime_environment(os.environ.copy(), root / "library")
            environment["R_PROFILE_USER"] = os.devnull
            environment.pop("R_HOME", None)
            environment["PATH"] = str(root)
            yield environment, gate
            gate.assert_retired()
