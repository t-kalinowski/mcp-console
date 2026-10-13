"""Discover user-installed uv when a launcher supplies a limited PATH."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import r_test_environment
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, USER_UV, command, requires
from support.resolvers import expose_uv
from support.suites import run_this_suite


def configure_python(root: Path) -> None:
    config = root / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "python": {"managed": {"version": "3.13", "packages": []}},
            }
        )
    )


def without_r(root: Path) -> dict[str, str]:
    tools = root / "empty-path"
    tools.mkdir()
    retain_system_bwrap(tools)
    environment = os.environ | {"PATH": str(tools)}
    for name in ("R_HOME", "RHOME", "RETICULATE_PYTHON", "RETICULATE_UV"):
        environment.pop(name, None)
    return environment


@requires(USER_UV)
@executions(DIRECT, SANDBOXED)
def test_discovers_default_user_uv(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        configure_python(root)
        environment = without_r(root)
        for name in ("XDG_BIN_HOME", "XDG_DATA_HOME"):
            environment.pop(name, None)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="print(6 * 7)")
            client.finish()
        return [{"uv_outside_path": "default user directory", "python_result": 42}]


@requires(command("uv"))
@executions(DIRECT, SANDBOXED)
def test_discovers_xdg_user_uv(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        configure_python(root)
        user_bin = root / "user-bin"
        user_bin.mkdir()
        expose_uv(user_bin)
        environment = without_r(root) | {"XDG_BIN_HOME": str(user_bin)}
        # The selected executable directory wins over XDG_DATA_HOME.
        environment["XDG_DATA_HOME"] = str(root / "other-data")
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="print(6 * 7)")
            client.finish()
        return [{"uv_outside_path": "XDG_BIN_HOME", "python_result": 42}]


@requires(command("uv"))
@executions(DIRECT, SANDBOXED)
def test_discovers_uv_beside_xdg_data(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        configure_python(root)
        data = root / "user/data"
        data.mkdir(parents=True)
        user_bin = data.parent / "bin"
        user_bin.mkdir()
        expose_uv(user_bin)
        environment = without_r(root) | {"XDG_DATA_HOME": str(data)}
        environment["XDG_BIN_HOME"] = "relative-user-bin"
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="print(6 * 7)")
            client.finish()
        return [{"uv_outside_path": "XDG_DATA_HOME sibling", "python_result": 42}]


@requires(command("uv"))
@executions(DIRECT, SANDBOXED)
def test_prefers_path_uv_to_user_uv(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        configure_python(root)
        user_bin = root / "user-bin"
        user_bin.mkdir()
        (user_bin / ("uv.exe" if os.name == "nt" else "uv")).write_text(
            "this user installation must not be selected"
        )
        environment = without_r(root) | {"XDG_BIN_HOME": str(user_bin)}
        expose_uv(Path(environment["PATH"]))
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="print(6 * 7)")
            client.finish()
        return [{"uv_selected_from": "PATH", "python_result": 42}]


@requires(POSIX, R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_uses_user_uv_with_r(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        user_bin = root / "user-bin"
        user_bin.mkdir()
        uv = expose_uv(user_bin)
        real_uv = uv.resolve()
        uv.unlink()
        cache = root / "cache"
        marker = cache / "mcp-console/dependencies/user-uv-used"
        marker.parent.mkdir(parents=True)
        uv.write_text(
            # fmt: python
            code(f"""
                #!{sys.executable}
                import os
                import sys
                from pathlib import Path

                Path({str(marker)!r}).touch()
                os.execv({str(real_uv)!r}, [{str(real_uv)!r}, *sys.argv[1:]])
                """)
        )
        uv.chmod(0o755)
        environment, _ = r_test_environment()
        environment["PATH"] = os.pathsep.join(
            entry
            for entry in environment["PATH"].split(os.pathsep)
            if not any(
                (Path(entry) / (name + (".exe" if os.name == "nt" else ""))).exists()
                for name in ("uv", "ir")
            )
        )
        environment["XDG_BIN_HOME"] = str(user_bin)
        environment["XDG_CACHE_HOME"] = str(cache)
        environment.pop("RETICULATE_UV", None)
        environment.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("[1] 42\n", r="6 * 7")
            assert marker.exists(), "preparation did not use the user-installed uv"
            client.finish()
        return [{"uv_outside_path": "XDG_BIN_HOME", "r_result": 42}]


if __name__ == "__main__":
    run_this_suite(__file__)
