"""Install a transferred checkout privately, retaining build data between runs."""

import fcntl
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

cache = Path.home() / ".cache/mcp-console-tests" / sys.argv[1]
source = cache / "source"
source.mkdir(parents=True, exist_ok=True)
with (cache / "install.lock").open("w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    manifest = cache / "files.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else []
    names = []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
        for entry in archive:
            path = source / entry.name
            assert entry.isfile() and path.is_relative_to(source)
            data = archive.extractfile(entry).read()
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
            path.chmod(entry.mode)
            names.append(entry.name)
    for name in set(previous) - set(names):
        (source / name).unlink()
    manifest.write_text(json.dumps(names))
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(cache / "tools"),
        "UV_TOOL_BIN_DIR": str(cache / "bin"),
    }
    subprocess.run(
        ["uv", "tool", "install", "--reinstall", str(source)],
        env=environment,
        stdout=sys.stderr,
        check=True,
    )

workspace = Path(tempfile.mkdtemp(prefix="mcp-console-ssh-"))
for name in ("results", "cli", "denied"):
    (workspace / name).mkdir()
print(
    json.dumps(
        {
            "target": {
                "workspace": str(workspace),
            },
            "path": str(cache / "bin") + os.pathsep + os.environ["PATH"],
            "environment": {"R_PROFILE_USER": os.devnull},
            "platform": platform.system(),
        }
    )
)
