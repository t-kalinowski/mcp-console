"""Optional cross-host tests using a private installation of the current source."""

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path

from support.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
QUICK = os.environ.get("MCP_CONSOLE_TEST_QUICK") == "1"
CONFIGURED = json.loads(os.environ.get("MCP_CONSOLE_TEST_SSH_EXTERNAL") or "null")
HOST = (
    CONFIGURED["target"]["transport"]["host"]
    if CONFIGURED is not None
    else os.environ.get("MCP_CONSOLE_TEST_SSH_HOST", "mule")
)
SSH_COMMAND = [
    "ssh",
    "-T",
    "-a",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=3",
    *(["-F", CONFIGURED["ssh_config"]] if CONFIGURED is not None else []),
    "--",
    HOST,
]


def reachable() -> bool:
    if not HOST or shutil.which("ssh") is None:
        return False
    try:
        return (
            subprocess.run(
                [*SSH_COMMAND, "true"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


EXTERNAL_SSH = Requirement(
    "external SSH target",
    not QUICK and reachable(),
    "omitted by --quick; run without --quick to include it"
    if QUICK
    else "no reachable test host; select another with MCP_CONSOLE_TEST_SSH_HOST",
)


@contextmanager
def external_target():
    if CONFIGURED is not None:
        yield CONFIGURED
        return
    # Transfer working-tree contents, including new files, so unreleased changes
    # are tested. The remote fixture owns installation and workspace creation.
    files = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        for name in files:
            if name and (ROOT / name).is_file():
                output.add(ROOT / name, arcname=name)
    installer = (ROOT / "tests/fixtures/ssh_install.py").read_text()
    cache_key = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    command = shlex.join(["python3", "-c", installer, cache_key])
    result = subprocess.run(
        [*SSH_COMMAND, shlex.join(["sh", "-lc", command])],
        input=archive.getvalue(),
        capture_output=True,
        timeout=540,
    )
    assert result.returncode == 0, result.stderr.decode()
    external = json.loads(result.stdout)
    external["target"]["transport"] = {"kind": "ssh", "host": HOST}
    try:
        yield external
    finally:
        subprocess.run(
            [
                *SSH_COMMAND,
                shlex.join(["rm", "-rf", "--", external["target"]["workspace"]]),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
