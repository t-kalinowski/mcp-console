"""Attempt effects outside a resolver policy before recording actual enforcement."""

import errno
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


def probe() -> None:
    cache = Path(os.environ["UV_CACHE_DIR"])
    protected = Path(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
    assert protected.joinpath("canary").read_text() == "preserved"
    alias = Path(os.environ["MCP_CONSOLE_TEST_ALIAS"])
    for destination in (protected / "escaped", alias / "escaped"):
        try:
            destination.write_text("escaped")
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
        else:
            raise AssertionError(f"resolver wrote outside its grants: {destination}")
    try:
        urllib.request.urlopen("https://example.com", timeout=10)
    except urllib.error.URLError as error:
        assert "403" in str(error), error
    else:
        raise AssertionError("resolver reached a disallowed download host")
    cache.joinpath("trust-probe.json").write_text(
        json.dumps(
            {
                "host_read": True,
                "direct_write_denied": True,
                "symlink_write_denied": True,
                "download_denied": True,
            }
        )
    )


if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ:
    probe()

if __name__ == "__main__":
    assert os.environ["UV_HTTP_TIMEOUT"] == "37"
    assert "UV_OFFLINE" not in os.environ
    os.execv(os.environ["MCP_CONSOLE_TEST_UV"], ["uv", *sys.argv[1:]])
