"""Install a transferred checkout privately, retaining build data between runs."""

import fcntl
import json
import os
import platform
import shutil
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
            for parent in path.parents:
                if parent == source:
                    break
                if parent.is_file():
                    parent.unlink()
            if path.is_dir():
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
            path.chmod(entry.mode)
            names.append(entry.name)
    for name in set(previous) - set(names):
        path = source / name
        if path.is_file():
            path.unlink()
    manifest.write_text(json.dumps(names))
    workspace = Path(tempfile.mkdtemp(prefix="mcp-console-ssh-"))
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(workspace / "tools"),
        "UV_TOOL_BIN_DIR": str(workspace / "bin"),
    }
    try:
        subprocess.run(
            ["uv", "tool", "install", str(source)],
            env=environment,
            stdout=sys.stderr,
            check=True,
        )
    except BaseException:
        shutil.rmtree(workspace)
        raise

for name in ("results", "cli", "denied"):
    (workspace / name).mkdir()
print(
    json.dumps(
        {
            "target": {
                "workspace": str(workspace),
            },
            "path": str(workspace / "bin") + os.pathsep + os.environ["PATH"],
            "environment": {"R_PROFILE_USER": os.devnull},
            "platform": platform.system(),
        }
    )
)
