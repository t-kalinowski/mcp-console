"""Configured declarations, policy admission, and captured Python selection."""

import json
import os
import shutil
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.python import virtualenv_python
from support.resolvers import (
    expose_uv,
    ir_run_records,
    recording_ir_environment,
    checkpoint_uv_environment,
)
from support.linux_sandbox import retain_system_bwrap
from support.records import Transcript
from support.normalization import code
from support.requirements import POSIX, R, SANDBOX, requires, command
from support.r import r_test_environment
from support.suites import run_this_suite
from support.snapshots import execution_snapshots


def configure(root: Path, document: dict) -> None:
    path = root / ".agents/console/config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def without_r(tools: Path) -> dict[str, str]:
    retain_system_bwrap(tools)
    env = {**os.environ, "PATH": str(tools)}
    for name in ("R_HOME", "RHOME", "RETICULATE_PYTHON", "VIRTUAL_ENV"):
        env.pop(name, None)
    return env


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_early_locked_request_preserves_startup_baseline(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        tools = root / "tools"
        tools.mkdir()
        environment, started, release = checkpoint_uv_environment(root, "six")
        (tools / "uv").symlink_to(environment["RETICULATE_UV"])
        (tools / "python3").symlink_to(sys.executable)
        environment.update(without_r(tools))
        for name in ("R_HOME", "RHOME", "RETICULATE_PYTHON", "VIRTUAL_ENV"):
            environment.pop(name, None)
        configure(
            root,
            {
                "languages": ["python"],
                "python": {
                    "managed": {"packages": ["six"], "resolution": "startup_only"}
                },
            },
        )
        try:
            with McpClient(
                binary, execution.serve("-c", "cache=host"), environment, root
            ) as client:
                client.initialize_and_list_tools()
                started.wait("configured startup preparation is blocked")
                pending = client.start_send(
                    control="restart",
                    requirements={"action": "set", "python": []},
                    python="early_cell_ran = True",
                )
                client.request("ping")
                release.release()
                client.receive(pending)
                result = pending["result"]
                assert (
                    result.get("isError")
                    and "configuration" in result["content"][0]["text"]
                ), result
                client.expect(
                    "baseline retained\n",
                    python="import six; assert 'early_cell_ran' not in globals(); print('baseline retained')",
                )
                inspected = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]
                assert inspected["requirements"] == inspected[
                    "startup_requirements"
                ] and inspected["requirements"]["python"] == ["six"], inspected
                return client.finish()
        finally:
            started.close()
            release.close()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_configured_r_packages_require_available_r(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        tools = root / "tools"
        tools.mkdir()
        configure(
            root,
            {
                "languages": ["python"],
                "r": {"packages": ["dplyr"]},
                "python": sys.executable,
            },
        )
        with McpClient(binary, execution.serve(), without_r(tools), root) as client:
            client.initialize_and_list_tools()
            result = client.send(python="print('must not run')")
            assert (
                result.get("isError") and "r.packages" in result["content"][0]["text"]
            ), result
            transcript, stderr = client.finish_with_standard_error(
                expected_exit_status=1
            )
            assert "r.packages" in stderr, stderr
            return transcript + [{"stderr": stderr}]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_configured_python_defaults_and_policy(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for policy in ("automatic", "explicit", "startup_only"):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / "tools"
            tools.mkdir()
            expose_uv(tools)
            configure(
                root,
                {
                    "languages": ["python"],
                    "python": {"managed": {"packages": [], "resolution": policy}},
                },
            )
            with McpClient(binary, execution.serve(), without_r(tools), root) as client:
                client.initialize_and_list_tools()
                result = client.send(requirements={"action": "get"})
                assert not result.get("isError"), result
                initial = result["structuredContent"]["requirements"]
                assert initial["python"] == [] and initial["duckdb"] == [], initial
                client.expect("ready\n", python="print('ready')")
                extension = client.send(
                    requirements={"duckdb": ["sqlite"]}, python="print('must not run')"
                )
                assert extension.get("isError"), extension
                expected_extension = (
                    "configuration" if policy == "startup_only" else "duckdb"
                )
                assert expected_extension in extension["content"][0]["text"].lower(), (
                    extension
                )
                assert "must not run\n" not in extension["content"][0]["text"], (
                    extension
                )
                missing = client.send(
                    python="try:\n    import six\n    print('available')\nexcept ModuleNotFoundError:\n    print('missing')"
                )
                expected = "available\n" if policy == "automatic" else "missing\n"
                assert missing["content"][0]["text"].endswith(expected), missing
                retained = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                assert retained["python"] == (
                    ["six"] if policy == "automatic" else []
                ), retained
                changed = client.send(
                    requirements={"python": ["six"]}, python="print('changed')"
                )
                if policy == "startup_only":
                    assert (
                        changed.get("isError")
                        and "configuration" in changed["content"][0]["text"]
                    ), changed
                    assert "changed\n" not in changed["content"][0]["text"], changed
                    unchanged = client.send(
                        control="restart", requirements={"action": "set", **initial}
                    )
                    assert not unchanged.get("isError"), unchanged
                else:
                    assert not changed.get("isError"), changed
                reset = client.send(control="restart", requirements={"action": "reset"})
                assert not reset.get("isError"), reset
                after = client.send(requirements={"action": "get"})
                assert after["structuredContent"]["requirements"] == initial, after
                records.append({"policy": policy})
                records.extend(client.finish())
    return records


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_existing_fallback_captured_across_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        venv = root / "active venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        tools = root / "tools"
        tools.mkdir()
        configure(
            root,
            {
                "languages": ["python"],
                "python": {
                    "first_available": [
                        {"existing": "missing"},
                        "active_venv",
                        {"managed": {"packages": ["impossible-unused-package"]}},
                    ]
                },
            },
        )
        env = {
            **without_r(tools),
            "VIRTUAL_ENV": str(venv),
            "RETICULATE_PYTHON": str(root / "invalid-legacy"),
        }
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            # fmt: python
            source = code("""
                import os, sys
                from pathlib import Path

                assert Path(sys.prefix) == Path.cwd() / "active venv"
                assert sys.prefix != sys.base_prefix
                os.environ["VIRTUAL_ENV"] = "missing-after-startup"
                os.environ["RETICULATE_PYTHON"] = "missing-after-startup"
                print("captured")
                """)
            client.expect("captured\n", python=source)
            (root / "missing").symlink_to(virtualenv_python(venv))
            client.send(control="restart")
            client.expect("captured\n", python=source)
            result = client.send(requirements={"action": "get"})
            assert result["structuredContent"]["requirements"]["python"] == [], result
            return client.finish()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_selected_r_disabled_without_path_discovery(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        tools = root / "tools"
        tools.mkdir()
        for utility in ("sh", "sed", "uname"):
            (tools / utility).symlink_to(shutil.which(utility))
        r = shutil.which("R")
        assert r is not None
        (root / "chosen R").symlink_to(r)
        env, _ = r_test_environment()
        env.update(
            {
                "PATH": str(tools),
                "R_HOME": str(root / "invalid inherited R"),
                "RETICULATE_PYTHON": sys.executable,
            }
        )
        configure(root, {"r": "./chosen R", "languages": ["r"]})
        with McpClient(
            binary, execution.serve("-c", "r.resolution=disabled"), env, root
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "selected R retained\n",
                r='cat("selected R retained\\n"); Sys.setenv(R_HOME = "changed", PATH = "changed")',
            )
            result = client.send(requirements={"action": "get"})
            assert result["structuredContent"]["requirements"]["r"] == [], result
            rejected = client.send(
                control="restart",
                requirements={"r": ["six"]},
                r='cat("must not run\\n")',
            )
            assert (
                rejected.get("isError")
                and "configuration" in rejected["content"][0]["text"]
            ), rejected
            client.send(control="restart")
            client.expect("selected R retained\n", r='cat("selected R retained\\n")')
            return client.finish()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_locked_r_rejects_mixed_changes_before_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        configure(
            root,
            {
                "r": {"packages": ["dplyr"], "resolution": "startup_only"},
                "python": {"managed": {"packages": ["six"], "resolution": "explicit"}},
            },
        )
        env, _ = r_test_environment()
        env.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("retained\n", r='retained <- 42; cat("retained\\n")')
            initial = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert initial["r"] == ["dplyr"] and initial["python"] == ["six"], initial
            for requested in (
                {"action": "set", "python": ["six", "packaging"]},
                {"r": ["ggplot2"], "python": ["packaging"]},
                {"duckdb": ["fts"], "python": ["packaging"]},
            ):
                result = client.send(
                    control="restart",
                    requirements=requested,
                    r='retained <- NULL; cat("must not run\\n")',
                )
                assert (
                    result.get("isError")
                    and "configuration" in result["content"][0]["text"]
                ), result
                client.expect("42\n", r="cat(retained, '\\n', sep = '')")
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
            unchanged = client.send(requirements={"action": "set", **initial})
            assert not unchanged.get("isError"), unchanged
            client.send(control="restart", requirements={"action": "reset"})
            assert (
                client.send(requirements={"action": "get"})["structuredContent"][
                    "requirements"
                ]
                == initial
            )
            return client.finish()


@requires(POSIX, SANDBOX)
def test_existing_venv_inspection_uses_worker_permissions(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        venv = workspace / ".venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        marker = root / "ungranted-marker"
        site = next((venv / "lib").glob("python*/site-packages"))
        site.joinpath("sitecustomize.py").write_text(
            "from pathlib import Path\n"
            + "try:\n"
            + f"    Path({str(marker)!r}).write_text('inspection escaped')\n"
            + "except PermissionError:\n    pass\n"
        )
        tools = root / "tools"
        tools.mkdir()
        configure(
            workspace,
            {
                "python": ".venv",
                "languages": ["python"],
                "sandbox": {"filesystem": {"read_write": [str(workspace)]}},
            },
        )
        with McpClient(
            binary, SANDBOXED.serve(), without_r(tools), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect("selected\n", python="print('selected')")
            assert not marker.exists(), (
                "existing-environment inspection escaped worker permissions"
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<fixture>")
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_selection_rejects_invalid_reached_candidates(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    cases = [
        (
            {"first_available": ["active_venv", {"managed": {}}]},
            {"VIRTUAL_ENV": sys.executable},
            "standard venv",
        ),
        (
            {"managed": {"packages": []}},
            {"UV_PYTHON_PREFERENCE": "system"},
            "configuration",
        ),
        (
            {"managed": {"packages": []}},
            {"RETICULATE_PYTHON": sys.executable},
            "configuration",
        ),
    ]
    for python, variables, expected in cases:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / "tools"
            tools.mkdir()
            expose_uv(tools)
            config = {"languages": ["python"], "python": python}
            env = without_r(tools)
            if "VIRTUAL_ENV" in variables:
                activated = root / "activated-file"
                activated.symlink_to(sys.executable)
                env["VIRTUAL_ENV"] = str(activated)
            else:
                config["resolver"] = {"environment": variables}
            configure(root, config)
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                result = client.send(python="print('must not run')")
                assert (
                    result.get("isError") and expected in result["content"][0]["text"]
                ), result
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert expected in stderr, stderr
                records.extend(
                    json.loads(
                        json.dumps([*transcript, {"stderr": stderr}]).replace(
                            str(root), "<fixture>"
                        )
                    )
                )
    return records


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_disabled_r_coexists_with_managed_python(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        env, record = recording_ir_environment(root)
        configure(
            root,
            {
                "r": {"resolution": "disabled"},
                "python": {"managed": {"packages": [], "resolution": "explicit"}},
            },
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.initialize_and_list_tools()
            client.expect("R available\n", r='cat("R available\\n")')
            changed = client.send(
                requirements={"python": ["six"]},
                python="import six; print(six.__version__)",
            )
            assert not changed.get("isError"), changed
            initial = client.send(requirements={"action": "get"})["structuredContent"]
            assert initial["requirements"]["r"] == [] and initial["requirements"][
                "python"
            ] == ["six"], initial
            assert initial["runtime_requirements"]["r"] == [], initial
            assert not ir_run_records(record), "disabled R invoked IR"
            return client.finish()


@requires(POSIX, R, SANDBOX)
def test_explicit_r_probe_uses_worker_permissions(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        launcher = workspace / "R"
        marker = root / "ungranted-marker"
        actual = shutil.which("R")
        assert actual is not None
        launcher.write_text(
            "#!/bin/sh\n"
            + "printf escaped > "
            + shlex.quote(str(marker))
            + " 2>/dev/null\n"
            + "exec "
            + shlex.quote(actual)
            + ' "$@"\n'
        )
        launcher.chmod(0o755)
        env, _ = r_test_environment()
        env["RETICULATE_PYTHON"] = sys.executable
        configure(
            workspace,
            {
                "r": {"executable": "R", "resolution": "disabled"},
                "languages": ["r"],
                "sandbox": {"filesystem": {"read_write": [str(workspace)]}},
            },
        )
        with McpClient(binary, SANDBOXED.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            client.expect("contained\n", r='cat("contained\\n")')
            assert not marker.exists(), (
                "explicit R inspection escaped worker permissions"
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<fixture>")
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_explicit_r_retry_and_installation_identity(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        launcher = root / "selected R"
        actual = shutil.which("R")
        assert actual is not None
        env, _ = r_test_environment()
        env["RETICULATE_PYTHON"] = sys.executable
        configure(
            root,
            {
                "r": {"executable": "selected R", "resolution": "disabled"},
                "languages": ["r"],
            },
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            failed = client.send(r='cat("must not run\\n")')
            assert (
                failed.get("isError") and "r.executable" in failed["content"][0]["text"]
            ), failed
            launcher.write_text("#!/bin/sh\nexec " + shlex.quote(actual) + ' "$@"\n')
            launcher.chmod(0o755)
            restarted = client.send(control="restart")
            assert not restarted.get("isError"), restarted
            client.expect("repaired\n", r='retained <- TRUE; cat("repaired\\n")')
            launcher.chmod(0o644)
            changed = client.send(control="restart")
            assert (
                changed.get("isError")
                and "installation changed" in changed["content"][0]["text"]
            ), changed
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<fixture>")
            )


if __name__ == "__main__":
    run_this_suite(__file__)
