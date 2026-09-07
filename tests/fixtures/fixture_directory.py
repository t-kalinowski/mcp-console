"""Give deterministic fixtures generation-local checkpoints without a sandbox."""

import os
import shutil
import tempfile
from pathlib import Path


def configure() -> Path:
    root = Path(os.environ["TMPDIR"])
    if os.environ.get("MCP_CONSOLE_TEST_FIXTURE_DIRECTORY") == "1":
        # The harness supplies one root per test. A replacement starts only after
        # its predecessor retires; discard that generation's stale checkpoints.
        for previous in root.glob("mcp-console-tmp-fixture-*"):
            shutil.rmtree(previous)
        root = Path(tempfile.mkdtemp(prefix="mcp-console-tmp-fixture-", dir=root))
        os.environ["TMPDIR"] = str(root)
        # Tests can observe this fixture's exit independently of the server's
        # inherited process group. The relay still signals only its direct child.
        os.setpgrp()
    return root
