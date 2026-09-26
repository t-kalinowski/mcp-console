"""Public MCP coverage with no R executable visible to the local server."""

import json
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import UNPRIVILEGED, requires
from support.normalization import code, normalize_python_resolution_error
from support.native import build_interposer


def environment(path: Path) -> dict[str, str]:
    env = dict(os.environ, PATH=str(path))
    for name in (
        "R_HOME",
        "R_LIBS",
        "R_LIBS_USER",
        "RETICULATE_PYTHON",
        "RETICULATE_UV",
    ):
        env.pop(name, None)
    return env


def selected_environment(path: Path) -> dict[str, str]:
    return dict(environment(path), RETICULATE_PYTHON=str(path / "python3"))


def preparation_environment(root: Path) -> dict[str, str]:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/sans_r_uv.py"
    uv = root / "uv"
    uv.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    uv.chmod(0o755)
    invalid = root / "invalid-python"
    # A resolver can succeed while its interpreter is not embeddable.
    # fmt: python
    inspection = code("""
        import json
        import os
        import signal
        import sys
        from pathlib import Path

        # Cache warmup has no output-file argument. Gate only the subsequent
        # native inspector, which supplies its private result path.
        if len(sys.argv) == 4:
            sys.exit(0)
        root = Path(__file__).parent
        if (root / "mode").read_text() == "inspection-interrupt":
            signal.signal(signal.SIGINT, lambda *_: sys.exit("fixture Python inspection interrupted"))
            (root / "resolver-pid").write_text(str(os.getpid()))
            with (root / "started").open("wb", buffering=0) as stream:
                stream.write(b"1")
            signal.pause()
            raise AssertionError("cancelled inspection continued")
        result = dict(executable=str(Path(__file__)), libpython=str(root / "missing-libpython"))
        for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix"):
            result[name] = str(root)
        Path(sys.argv[-1]).write_text(json.dumps(result))
        """)
    invalid.write_text(f"#!{sys.executable}\n" + inspection)
    invalid.chmod(0o755)
    env = environment(root)
    env["MCP_CONSOLE_TEST_PREPARATION"] = str(root)
    env["MCP_CONSOLE_TEST_REAL_UV"] = shutil.which("uv")
    return env


def preparation_records(records: Transcript, root: Path) -> Transcript:
    for record in records:
        for content in record.get("result", {}).get("content", []):
            if content.get("type") != "text":
                continue
            text = (
                content["text"]
                .replace(str(root.resolve()), "<preparation>")
                .replace(str(root), "<preparation>")
            )
            if text.startswith(
                (
                    "managed Python resolution failed:",
                    "[managed Python resolution failed:",
                )
            ):
                text = normalize_python_resolution_error(text)
            content["text"] = text
    return records


@executions(DIRECT, SANDBOXED)
def test_configured_python_bypasses_uv(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", workspace / ".venv"],
            check=True,
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("python: .venv/bin/python\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text("#!/bin/sh\nexit 87\n")
        uv.chmod(0o755)
        env = environment(bin_dir)
        # Explicit selection bypasses even unsupported resolver configuration.
        env["UV_ENV_FILE"] = str(workspace / "missing.env")
        env["RETICULATE_PYTHON"] = str(root / "missing-python")
        with McpClient(
            binary, execution.serve(), env, current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import subprocess, sys
                    from pathlib import Path

                    assert Path(sys.prefix) == Path.cwd() / ".venv"
                    assert (
                        subprocess.check_output(
                            [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                        ).strip()
                        == sys.prefix
                    )
                    identity = object()
                    identity_id = id(identity)
                    42
                    """)
            )
            assert last_result_text(client) == "42\n"
            result = client.send(
                control="restart",
                requirements={"python": ["six"]},
                python="identity = None",
            )
            assert result["isError"]
            client.send(python="assert id(identity) == identity_id; 42")
            assert last_result_text(client) == "42\n"
            config.write_text("python: missing-after-startup\n")
            client.send(
                control="restart",
                python="import sys; from pathlib import Path; assert Path(sys.prefix) == Path.cwd() / '.venv'; 42",
            )
            assert last_result_text(client).endswith("42\n[done]")
            records = client.finish()
        # The CLI uses the same config layer and overrides the now-invalid file.
        with McpClient(
            binary, execution.serve("-c", "python=.venv/bin/python"), env, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import sys; from pathlib import Path; assert Path(sys.prefix) == Path.cwd() / '.venv'; 42"
            )
            assert last_result_text(client) == "42\n"
            records.extend(client.finish())
        return records


@executions(DIRECT, SANDBOXED)
def test_rejects_unsupported_managed_inputs(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for name, value in (
        ("UV_ENV_FILE", "worker.env"),
        ("UV_PYTHON", "project-python"),
        ("UV_CONFIG_FILE", 'find-links = ["./packages"]'),
        ("UV_CONFIG_FILE", 'python-preference = "system"'),
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uv = root / "uv"
            uv.write_text("#!/bin/sh\necho unsafe-resolver-executed >&2\nexit 87\n")
            uv.chmod(0o755)
            env = environment(root)
            if name == "UV_CONFIG_FILE":
                config = root / "uv.toml"
                config.write_text(value)
                env[name] = str(config)
            else:
                env[name] = value
            with McpClient(binary, execution.serve(), env) as client:
                client.process.wait(timeout=30)
                diagnostic = client.stderr.read()
                assert client.process.returncode != 0
                assert "unsupported managed Python setting" in diagnostic, diagnostic
                assert "unsafe-resolver-executed" not in diagnostic
                records.append({"rejected": value})
    return records


@executions(DIRECT, SANDBOXED)
def test_captures_user_uv_configuration(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        config = root / "config/uv/uv.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            'index-url = "https://private.example/simple"\nindex-strategy = "first-index"\n'
        )
        (workspace / "uv.toml").write_text('find-links = ["./untrusted"]\n')
        uv = root / "uv"
        # This public resolver fixture checks the inherited values, then uses
        # the real registry so the package and embedding assertions stay real.
        # fmt: python
        adapter = code(f"""
            import os
            import sys

            assert os.environ["UV_INDEX_URL"] == "https://private.example/simple"
            assert os.environ["UV_INDEX_STRATEGY"] == "first-index"
            assert os.environ["UV_NO_CONFIG"] == "1"
            assert os.environ["UV_PYTHON_PREFERENCE"] == "only-managed"
            del os.environ["UV_INDEX_URL"]
            uv = {shutil.which("uv")!r}
            os.execv(uv, [uv, *sys.argv[1:]])
            """)
        uv.write_text(f"#!{sys.executable}\n" + adapter)
        uv.chmod(0o755)
        env = environment(root)
        env["XDG_CONFIG_HOME"] = str(root / "config")
        env["XDG_CONFIG_DIRS"] = str(root / "no-system-config")
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 42")
            config.write_text('find-links = ["./changed-after-capture"]\n')
            client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
                python="import yaml12; 42",
            )
            assert last_result_text(client).endswith("42\n[done]"), client.transcript[
                -1
            ]
            return client.finish()


@executions(SANDBOXED)
def test_rejects_full_write_policy(binary: Path, execution: Execution) -> Transcript:
    records = []
    for filesystem in (
        "kind: unrestricted",
        """kind: restricted
    entries:
      - path: {type: special, value: {kind: root}}
        access: write""",
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("sandbox:\n  filesystem:\n    " + filesystem + "\n")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "uv-executed"
            uv = bin_dir / "uv"
            uv.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                "raise SystemExit(87)\n"
            )
            uv.chmod(0o755)
            (bin_dir / "python3").symlink_to(sys.executable)
            result = subprocess.run(
                [binary, *execution.serve()],
                cwd=workspace,
                env=environment(bin_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode != 0
            assert (
                "worker-writable" in result.stderr
                or "no protected `uv` executable" in result.stderr
            ), result.stderr
            assert not marker.exists()
            records.append({filesystem.splitlines()[0]: "automatic selection rejected"})
    return records


@executions(SANDBOXED)
def test_rejects_worker_writable_uv_storage(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for label, variable in (
        ("UV_CACHE_DIR", "UV_CACHE_DIR"),
        ("UV_PYTHON_INSTALL_DIR", "UV_PYTHON_INSTALL_DIR"),
        ("UV_CACHE_DIR with parent components", "UV_CACHE_DIR"),
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text('extends: ":workspace"\n')
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "uv-executed"
            uv = bin_dir / "uv"
            uv.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
                "raise SystemExit(87)\n"
            )
            uv.chmod(0o755)
            (bin_dir / "python3").symlink_to(sys.executable)
            env = environment(bin_dir)
            if label.endswith("parent components"):
                (root / "protected").mkdir()
                env[variable] = str(
                    root / "protected/missing/../../workspace/uv-storage"
                )
            else:
                env[variable] = str(workspace / "uv-storage")
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.process.wait(timeout=30)
                diagnostic = client.stderr.read()
                assert "worker-writable" in diagnostic, diagnostic
                assert not marker.exists()
            records.append({label: "managed startup rejected"})
    return records


@executions(SANDBOXED)
def test_temporary_write_grants_exclude_host_uv(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for options, special, temporary_root in (
        ({"exclude_tmpdir_env_var": False}, None, "/var/tmp"),
        ({"exclude_slash_tmp": False}, None, "/tmp"),
        (None, None, "/var/tmp"),
        (None, None, "/tmp"),
        ({}, "tmpdir", "/var/tmp"),
        ({}, "slash_tmp", "/tmp"),
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory(dir=temporary_root) as inherited,
        ):
            workspace = Path(directory)
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            sandbox = {"workspace_options": options}
            if special:
                sandbox["filesystem"] = {
                    "entries": [
                        {
                            "path": {"type": "special", "value": {"kind": special}},
                            "access": "write",
                        }
                    ]
                }
            config.write_text(
                json.dumps(
                    {
                        "extends": ":workspace",
                        "sandbox": sandbox,
                    }
                )
            )
            uv = Path(inherited) / "uv"
            marker = workspace / "uv-executed"
            uv.write_text(
                f"#!{sys.executable}\n"
                # fmt: python
                + code(f"""
                    from pathlib import Path

                    Path({str(marker)!r}).touch()
                    raise SystemExit(87)
                    """)
            )
            uv.chmod(0o755)
            env = environment(Path(inherited))
            env.update(RETICULATE_UV=str(uv), TMPDIR=inherited)
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.process.wait(timeout=30)
                diagnostic = client.stderr.read()
                assert "project or a worker-writable path" in diagnostic, diagnostic
                assert not marker.exists()
                records.append(
                    {
                        "options": options,
                        "special": special,
                        "temporary_root": temporary_root,
                        "stderr": diagnostic.replace(str(uv), "<temporary>/uv"),
                    }
                )
    return records


@executions(SANDBOXED)
def test_worker_writable_candidate_preserves_running_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text('extends: ":workspace"\n')
        env = preparation_environment(root)
        candidate = workspace / "candidate-python"
        marker = workspace / "candidate-executed"
        env["MCP_CONSOLE_TEST_UNSAFE_PYTHON"] = str(candidate)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code(r"""
                    import os
                    from pathlib import Path

                    identity = object()
                    candidate = Path(os.environ["MCP_CONSOLE_TEST_UNSAFE_PYTHON"])
                    candidate.write_text("#!/bin/sh\nprintf touched > candidate-executed\nexit 87\n")
                    candidate.chmod(0o755)
                    """)
            )
            assert not client.transcript[-1]["result"]["isError"]
            assert candidate.is_file(), client.transcript[-1]
            (root / "mode").write_text("unsafe-candidate")
            client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
                # fmt: python
                python=code("""
                    open("replacement-ran", "w").close()
                    """),
            )
            assert client.transcript[-1]["result"]["isError"]
            assert "worker-writable" in last_result_text(client), client.transcript[-1]
            assert not marker.exists()
            assert not (workspace / "replacement-ran").exists()
            client.send(
                # fmt: python
                python=code("""
                    assert identity is not None
                    print("old worker retained")
                    """)
            )
            assert last_result_text(client) == "old worker retained\n"
            return preparation_records(client.finish(), root)


@executions(SANDBOXED)
def test_skips_project_uv_left_by_a_writable_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text('extends: ":workspace"\n')
        safe_bin = root / "safe-bin"
        safe_bin.mkdir()
        (safe_bin / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            binary, execution.serve(), environment(safe_bin), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code(r"""
                    from pathlib import Path

                    candidate = Path(".venv/bin/uv")
                    candidate.parent.mkdir(parents=True)
                    candidate.write_text("#!/bin/sh\nprintf touched > project-uv-executed\nexit 87\n")
                    candidate.chmod(0o755)
                    """)
            )
            assert not client.transcript[-1]["result"]["isError"], client.transcript[-1]
            client.finish()
        project_uv = workspace / ".venv/bin/uv"
        assert project_uv.is_file()
        env = environment(project_uv.parent)
        env["PATH"] = os.pathsep.join((str(project_uv.parent), str(safe_bin)))
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
                # fmt: python
                python=code("""
                    import yaml12

                    print("safe uv prepared Python")
                    """),
            )
            assert last_result_text(client).endswith(
                "safe uv prepared Python\n[done]"
            ), client.transcript[-1]
            assert not (workspace / "project-uv-executed").exists()
            records = client.finish()
        explicit = dict(env, RETICULATE_UV=str(project_uv))
        rejected = subprocess.run(
            [binary, *execution.serve()],
            cwd=workspace,
            env=explicit,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert rejected.returncode != 0
        assert "selected uv" in rejected.stderr
        assert "project or a worker-writable path" in rejected.stderr
        assert not (workspace / "project-uv-executed").exists()
        records.append({"explicit_project_uv": "rejected without execution"})
        explicit_python = dict(explicit, RETICULATE_PYTHON=sys.executable)
        with McpClient(binary, execution.serve(), explicit_python, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    print("explicit Python retained")
                    """)
            )
            assert "explicit Python retained\n" in last_result_text(client)
            client.finish()
        assert not (workspace / "project-uv-executed").exists()
        records.append({"explicit_python_selection": "kept without uv execution"})
        return records


@executions(DIRECT, SANDBOXED)
def test_prepares_managed_python_at_startup_and_restart(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        env = environment(root)
        env["PYTHONPATH"] = str(workspace)
        uv = root / "uv"
        shutil.copy2(shutil.which("uv"), uv)
        arguments = (
            execution.serve("--writable-root", str(workspace))
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(binary, arguments, env, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={"python": ["py-yaml12"]},
                # fmt: python
                python=code("""
                    import yaml12

                    print("startup package available")
                    """),
            )
            assert not client.transcript[-1]["result"]["isError"], client.transcript[-1]
            schema = client.transcript[2]["result"]["tools"][0]["inputSchema"]
            assert {"r", "sql"}.isdisjoint(schema["properties"])
            requirement_schema = schema["properties"]["requirements"]
            assert set(requirement_schema["properties"]) == {"python"}
            client.send(
                # fmt: python
                python=code("""
                    import os, subprocess, sys, numpy, pandas, yaml12

                    assert "RETICULATE_PYTHON" not in os.environ
                    subprocess.run([sys.executable, "-c", "import numpy, pandas, yaml12"], check=True)
                    identity = object()
                    identity_id = id(identity)
                    print("startup packages available")
                    """)
            )
            assert last_result_text(client) == "startup packages available\n"
            # A no-op must not invoke uv or replace the running interpreter.
            uv.unlink()
            client.send(requirements={"python": ["py-yaml12", "numpy"]})
            assert last_result_text(client) == "[prepared]"
            client.send(
                # fmt: python
                python=code("""
                    assert id(identity) == identity_id
                    print("same worker")
                    """)
            )
            assert last_result_text(client) == "same worker\n"
            client.send(
                requirements={"python": ["py-yaml12"]},
                # fmt: python
                python=code("""
                    assert id(identity) == identity_id
                    print("no-op cell")
                    """),
            )
            assert last_result_text(client) == "no-op cell\n"
            client.send(
                # fmt: python
                python=code("""
                    from pathlib import Path

                    Path("sitecustomize.py").write_text(
                        "import os; from pathlib import Path; "
                        "'MCP_CONSOLE_LOCAL_RUNTIME' in os.environ or Path('../host-hook-ran').touch()"
                    )
                    input("old worker> ")
                    open("old-worker-consumed-input", "w").close()
                    """)
            )
            assert "[waiting for stdin]" in last_result_text(client)
            shutil.copy2(shutil.which("uv"), uv)
            client.send(
                control="restart",
                requirements={"python": ["more-itertools"]},
                stdin="replacement input\n",
                # fmt: python
                python=code("""
                    assert "identity" not in globals()
                    print(input())
                    """),
            )
            assert not client.transcript[-1]["result"]["isError"], client.transcript[-1]
            assert "replacement input\n" in last_result_text(client)
            assert not (workspace / "old-worker-consumed-input").exists()
            assert not (root / "host-hook-ran").exists(), (
                "resolver executed worker startup hook"
            )
            (workspace / "sitecustomize.py").unlink()
            # Plain restart and crash replacement retain the accepted result.
            uv.unlink()
            for control in ({}, {"control": "restart"}, {"crash": True}):
                if control.pop("crash", False):
                    client.send(
                        # fmt: python
                        python=code("""
                            import os

                            os._exit(47)
                            """)
                    )
                    assert "status 47" in last_result_text(client), client.transcript[
                        -1
                    ]
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os, subprocess, sys, numpy, pandas, yaml12, more_itertools

                        assert "RETICULATE_PYTHON" not in os.environ
                        probe = "import numpy, pandas, yaml12, more_itertools"
                        subprocess.run([sys.executable, "-c", probe], check=True)
                        subprocess.run(["python", "-c", probe], check=True)
                        print("cumulative packages retained")
                        """),
                )
                assert "cumulative packages retained\n" in last_result_text(client), (
                    client.transcript[-1]
                )
            client.send(requirements={"python": ["more-itertools", "py-yaml12"]})
            assert last_result_text(client) == "[prepared]"
            client.send(
                control="restart",
                requirements={"python": ["more-itertools", "py-yaml12"]},
                # fmt: python
                python=code("""
                    import yaml12, more_itertools

                    print("retained restart")
                    """),
            )
            assert "retained restart\n" in last_result_text(client), client.transcript[
                -1
            ]
            # Restart without code prepares immediately and retains cumulative requirements.
            shutil.copy2(shutil.which("uv"), uv)
            client.send(control="restart", requirements={"python": ["six"]})
            assert not client.transcript[-1]["result"]["isError"]
            client.send(
                # fmt: python
                python=code("""
                    import six, yaml12, more_itertools

                    42
                    """)
            )
            assert last_result_text(client) == "42\n"
            records = client.finish()
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        quarto = (session / "transcript.qmd").read_text()
        assert "  packages: []\n" in quarto
        for package in ("numpy", "pandas", "py-yaml12", "more-itertools"):
            assert f"    - {package}\n" in quarto
    return TranscriptWithCompanions(
        records, {"qmd": quarto.replace(str(workspace.resolve()), "<workspace>")}
    )


@executions(DIRECT, SANDBOXED)
def test_failed_managed_preparation_preserves_worker_and_input(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        env = preparation_environment(root)
        started = FifoCheckpoint.create(root / "started")
        try:
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                client.send(
                    # fmt: python
                    python=code("""
                        identity = object()
                        identity_id = id(identity)
                        42
                        """),
                    stdin="retained input\n",
                )
                assert last_result_text(client) == "42\n"
                for mode, expected in (
                    ("failure", "fixture Python resolution failed"),
                    ("inspection", "selected Python embedding library is missing"),
                    ("interrupt", "fixture Python resolution interrupted"),
                    (
                        "inspection-interrupt",
                        "fixture Python inspection interrupted",
                    ),
                ):
                    (root / "mode").write_text(mode)
                    arguments = dict(
                        control="restart",
                        requirements={"python": ["py-yaml12"]},
                        stdin="must not reach old worker\n",
                        # fmt: python
                        python=code("""
                            raise AssertionError("failed restart ran code")
                            """),
                    )
                    if mode in ("interrupt", "inspection-interrupt"):
                        pending = client.start_send(**arguments)
                        started.wait("candidate resolver entered")
                        interrupt = client.start_send(control="interrupt")
                        client.receive_many([pending, interrupt])
                        response = pending["result"]
                        pid = int((root / "resolver-pid").read_text())
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            raise AssertionError("resolver child survived interruption")
                    else:
                        response = client.send(**arguments)
                    assert response["isError"], response
                    text = "".join(item.get("text", "") for item in response["content"])
                    assert expected in text, response
                    client.send(
                        # fmt: python
                        python=code("""
                            assert id(identity) == identity_id
                            print("objects intact")
                            """)
                    )
                    assert last_result_text(client) == "objects intact\n"
                before = (root / "resolutions.jsonl").read_text()
                for request in (
                    {"requirements": {"python": ["py-yaml12"]}},
                    {
                        "requirements": {"python": ["py-yaml12"]},
                        # fmt: python
                        "python": code("""
                            identity = None
                            """),
                        "stdin": "rejected live input\n",
                    },
                    {
                        "control": "restart",
                        "requirements": {"python": ["./local-package"]},
                        "stdin": "invalid input\n",
                    },
                    {"requirements": {"r": ["cli"]}},
                    {"requirements": {"duckdb": ["json"]}},
                ):
                    response = client.send(**request)
                    assert response.get("isError", True), response
                assert (root / "resolutions.jsonl").read_text() == before
                client.send(
                    # fmt: python
                    python=code("""
                        assert id(identity) == identity_id
                        print(input())
                        """)
                )
                assert (
                    last_result_text(client)
                    == '[input requested: ""]\nretained input\n'
                ), client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        print(input("remaining> "))
                        """)
                )
                assert "[waiting for stdin]" in last_result_text(client)
                client.send(
                    control="interrupt",
                    requirements={"python": ["numpy"]},
                    # fmt: python
                    python=code("""
                        identity = None
                        """),
                    stdin="rejected interrupt input\n",
                )
                assert client.transcript[-1]["result"]["isError"], client.transcript[-1]
                client.send()
                assert "[waiting for stdin]" in last_result_text(client), (
                    client.transcript[-1]
                )
                wait_for_evaluation_output(
                    client,
                    "fresh input\n",
                    "queue remained empty",
                    stdin="fresh input\n",
                )
                # Failed candidates were never retained: the addition still needs resolution.
                (root / "mode").write_text("success")
                client.send(
                    control="restart",
                    requirements={"python": ["py-yaml12"]},
                    # fmt: python
                    python=code("""
                        import yaml12

                        print("accepted")
                        """),
                )
                assert "accepted\n" in last_result_text(client), client.transcript[-1]
                assert (root / "resolutions.jsonl").read_text() != before
                records = preparation_records(client.finish(), root)
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            events = [
                json.loads(line)
                for line in (session / "internal/events.jsonl").read_text().splitlines()
            ]
            accepted = [
                event
                for event in events
                if event["event"] == "python_environment_accepted"
            ]
            assert len(accepted) == 1, accepted
            assert set(accepted[0]["packages"]) == {"numpy", "pandas", "py-yaml12"}
            return records
        finally:
            started.close()


@executions(DIRECT, SANDBOXED)
def test_retries_failed_prestart_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        env = preparation_environment(root)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for mode in ("failure", "inspection"):
                (root / "mode").write_text(mode)
                client.send(
                    requirements={"python": ["py-yaml12"]},
                    # fmt: python
                    python=code("""
                        raise AssertionError("failed preparation ran code")
                        """),
                    stdin="rejected input\n",
                )
                assert client.transcript[-1]["result"]["isError"], client.transcript[-1]
            (root / "mode").write_text("success")
            client.send(requirements={"python": ["py-yaml12"]})
            assert last_result_text(client) == "[prepared]"
            client.send(
                # fmt: python
                python=code("""
                    import yaml12

                    print(input("prepared> "))
                    """)
            )
            assert "[waiting for stdin]" in last_result_text(client), client.transcript[
                -1
            ]
            wait_for_evaluation_output(
                client,
                "fresh input\n",
                "failed prestart did not queue input",
                stdin="fresh input\n",
            )
            return preparation_records(client.finish(), root)


@executions(DIRECT, SANDBOXED)
def test_shutdown_cancels_sans_r_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for restart, mode in (
        (False, "interrupt"),
        (True, "interrupt"),
        (False, "inspection-interrupt"),
        (True, "inspection-interrupt"),
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            env = preparation_environment(root)
            (root / "mode").write_text(mode)
            started = FifoCheckpoint.create(root / "started")
            try:
                with McpClient(binary, execution.serve(), env, workspace) as client:
                    client.initialize_and_list_tools()
                    if restart:
                        client.send(
                            # fmt: python
                            python=code("""
                                retained = 42
                                retained
                                """)
                        )
                        assert last_result_text(client) == "42\n"
                    client.start_send(
                        requirements={"python": ["py-yaml12"]},
                        # fmt: python
                        python=code("""
                            raise AssertionError("cancelled preparation ran code")
                            """),
                        **({"control": "restart"} if restart else {}),
                    )
                    started.wait("resolver entered before input closure")
                    pid = int((root / "resolver-pid").read_text())
                    client.stdin.close()
                    assert client.process.wait(timeout=10) == 0
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise AssertionError("cancelled resolver survived shutdown")
                    records.append(
                        {
                            "restart": restart,
                            "phase": mode,
                            "stdout": client.stdout.read(),
                            "stderr": client.stderr.read(),
                        }
                    )
                (session,) = (workspace / ".agents/console/sessions").iterdir()
                events = [
                    json.loads(line)
                    for line in (session / "internal/events.jsonl")
                    .read_text()
                    .splitlines()
                ]
                assert all(
                    event["event"] != "python_environment_accepted" for event in events
                )
            finally:
                started.close()
    return records


@executions(DIRECT, SANDBOXED)
def test_resolves_default_python_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        uv = shutil.which("uv")
        assert uv is not None
        shutil.copy2(uv, path / "uv")
        env = environment(path)
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            schema = client.transcript[-1]["result"]["tools"][0]
            assert {"r", "sql"}.isdisjoint(schema["inputSchema"]["properties"])
            assert (
                "without R" in schema["description"]
                or "R and SQL" in schema["description"]
            )
            client.send(
                # fmt: python
                python=code("""
                    import json
                    import os
                    import subprocess
                    import sys
                    import numpy
                    import pandas

                    assert "RETICULATE_PYTHON" not in os.environ
                    assert sys.prefix != sys.base_prefix
                    base = os.path.join(sys.base_prefix, "bin", "python3")
                    probe = "import importlib.util; assert importlib.util.find_spec('pandas') is None"
                    subprocess.run([base, "-I", "-c", probe], check=True)
                    probe = "import json, sys, pandas; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix]))"
                    child = json.loads(subprocess.check_output([sys.executable, "-c", probe], text=True))
                    assert child == [sys.executable, sys.prefix, sys.exec_prefix]
                    child = json.loads(subprocess.check_output(["python", "-c", probe], text=True))
                    assert child == [sys.executable, sys.prefix, sys.exec_prefix]
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    import multiprocessing

                    with multiprocessing.get_context("spawn").Pool(1) as pool:
                        child = pool.apply(eval, ("__import__('sys').executable",))
                    assert child == sys.executable
                    # Release the pool's semaphores before restarting a worker
                    # whose interpreter is intentionally never finalized.
                    del pool
                    import gc

                    gc.collect()
                    selected = sys.executable
                    retained = 41
                    identity = object()
                    identity_id = id(identity)
                    retained + 1
                    """)
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            client.send(python="assert id(identity) == identity_id; retained + 2")
            assert last_result_text(client) == "43\n", client.transcript[-1]
            for request in (
                {"r": "1"},
                {"sql": "SELECT 1"},
                {"requirements": {"python": ["six"]}},
                {"requirements": {"r": ["cli"]}},
                {"requirements": {"duckdb": ["json"]}},
            ):
                result = client.send(**request)
                assert result.get("isError", True), result
                client.send(python="assert id(identity) == identity_id; retained + 2")
                assert last_result_text(client) == "43\n"
            client.send(python="import mcp_console_package_that_does_not_exist")
            assert "automatic package installation is unavailable" in last_result_text(
                client
            )
            assert "user-selected" not in last_result_text(client)
            client.send(python="assert id(identity) == identity_id; retained + 2")
            assert last_result_text(client) == "43\n"
            client.send(python="print(selected)")
            selected = last_result_text(client).strip()
            # Resolution cannot run again: remove uv after successful selection.
            (path / "uv").unlink()
            discovered_r = path / "R"
            discovered_r.write_text(
                code("""
                #!/bin/sh
                exit 48
                """)
            )
            discovered_r.chmod(0o755)
            client.send(
                control="restart", python="import sys, pandas; print(sys.executable)"
            )
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n"
                + selected
                + "\n[done]"
            ), client.transcript[-1]
            records = client.finish()
            for record in records:
                if "result" in record:
                    for content in record["result"].get("content", []):
                        if content.get("type") == "text":
                            content["text"] = content["text"].replace(
                                selected, "<selected Python>"
                            )
            return records


@executions(DIRECT, SANDBOXED)
def test_uses_selected_virtualenv(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        subprocess.run(
            ["uv", "pip", "install", "--python", selected, "matplotlib"],
            check=True,
            capture_output=True,
        )
        env = selected_environment(venv / "bin")
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys
                    import tempfile
                    from pathlib import Path
                    import matplotlib.pyplot as plt

                    assert os.environ["RETICULATE_PYTHON"] == sys.executable
                    assert sys.prefix != sys.base_prefix
                    value = 40
                    temporary = Path(tempfile.gettempdir())
                    (temporary / "owned.txt").write_text("owned")
                    print(temporary)
                    """)
            )
            temporary = Path(last_result_text(client).strip())
            assert temporary.is_dir(), client.transcript[-1]
            client.send(
                python="value += 1; print('before error'); raise ValueError('recoverable')"
            )
            assert "ValueError: recoverable" in last_result_text(client)
            client.send(python="value + 1")
            assert last_result_text(client) == "42\n"
            client.send(python="answer = input('value> '); answer")
            assert "[waiting for stdin]" in last_result_text(client)
            wait_for_evaluation_output(
                client, "'hello'\n", "sans-R input", stdin="hello\n"
            )
            client.send(
                # fmt: python
                python=code("""
                    _ = plt.plot([1, 2], [3, 4])
                    plt.gcf().savefig(temporary / "expected.png")
                    """)
            )
            assert_result_content(
                client,
                [(temporary / "expected.png").read_bytes()],
                image_reference="selected environment savefig {page}",
            )
            client.send(python="input('interrupt> ')")
            client.send(control="interrupt")
            assert "KeyboardInterrupt" in last_result_text(client)
            client.send(python="value + 1")
            assert last_result_text(client) == "42\n"
            client.send(control="restart", python="'value' in globals()")
            assert (
                last_result_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\nFalse\n[done]"
            ), client.transcript[-1]
            assert not temporary.exists(), "retired worker storage remains"
            client.send(python="import tempfile; print(tempfile.gettempdir())")
            replacement = Path(last_result_text(client).strip())
            assert replacement.is_dir() and replacement != temporary
            records = client.finish()
            assert not replacement.exists(), "shutdown worker storage remains"
            assert selected.exists(), "retirement deleted the selected environment"
            for record in records:
                if "result" in record:
                    for content in record["result"].get("content", []):
                        if content.get("type") == "text":
                            content["text"] = (
                                content["text"]
                                .replace(str(temporary), "<worker temporary>")
                                .replace(str(replacement), "<replacement temporary>")
                            )
            return records


@executions(DIRECT, SANDBOXED)
def test_reports_missing_interpreters(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "python3").symlink_to(sys.executable)
        result = subprocess.run(
            [binary, *execution.serve()],
            env=environment(Path(directory)),
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "no protected `uv` executable" in result.stderr, result.stderr
        return [{"stderr": result.stderr}]


@executions(DIRECT, SANDBOXED)
def test_resolver_failure_does_not_fall_back(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "uv").symlink_to(shutil.which("uv"))
        (path / "python3").symlink_to(sys.executable)
        env = environment(path)
        env["UV_PYTHON_PREFERENCE"] = "invalid-console-acceptance-preference"
        # Keep MCP input open: the resolver failure, rather than input-owner
        # cancellation, must determine the outcome.
        with McpClient(binary, execution.serve(), env) as client:
            client.process.wait(timeout=30)
            diagnostic = client.stderr.read()
            assert client.process.returncode != 0
            assert (
                "unsupported managed Python setting `UV_PYTHON_PREFERENCE`"
                in diagnostic
            ), diagnostic
            return [{"stderr": diagnostic}]


@executions(DIRECT, SANDBOXED)
def test_rejects_broken_r_instead_of_selecting_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        env = environment(path)
        env["R_HOME"] = str(path / "missing-r")
        result = subprocess.run(
            [binary, *execution.serve()],
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0 and "Rscript" in result.stderr, result.stderr
        explicit = result.stderr.replace(str(path), "<fixture>")
        env.pop("R_HOME")
        broken = path / "R"
        broken.write_text(
            code("""
            #!/bin/sh
            echo 'broken discovered R' >&2
            exit 41
            """)
        )
        broken.chmod(0o755)
        result = subprocess.run(
            [binary, *execution.serve()],
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0 and "broken discovered R" in result.stderr, (
            result.stderr
        )
        return [{"invalid_R_HOME": explicit}, {"broken_R": result.stderr}]


@executions(DIRECT, SANDBOXED)
def test_cleans_temporary_storage_after_startup_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        site = Path(
            subprocess.check_output(
                [selected, "-c", "import site; print(site.getsitepackages()[0])"],
                text=True,
            ).strip()
        )
        # This is a real selected interpreter's startup hook, not an evaluator.
        # Inspection succeeds; only the subsequently embedded worker exits.
        # fmt: python
        hook = code("""
            import os
            from pathlib import Path

            if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                Path("startup-temporary").write_text(os.environ["TMPDIR"])
                Path(os.environ["TMPDIR"], "owned-before-failure").touch()
                os._exit(47)
            """)
        (site / "sitecustomize.py").write_text(hook)
        arguments = (
            execution.serve("--writable-root", str(root))
            if execution == SANDBOXED
            else execution.serve()
        )
        env = selected_environment(venv / "bin")
        env["RETICULATE_PYTHON"] = str(selected)
        with McpClient(binary, arguments, env, current_directory=root) as client:
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('startup failure ran the cell')"
            )
            assert result["isError"] and "status 47" in last_result_text(client), result
            temporary = Path((root / "startup-temporary").read_text())
            assert not temporary.exists(), "failed worker storage remains"
            assert selected.exists(), "startup failure deleted the environment"
            return client.finish()


@executions(DIRECT)
def test_describes_direct_python_session(
    binary: Path, execution: Execution
) -> Transcript:
    return describe_session(binary, execution)


@executions(SANDBOXED)
def test_describes_sandboxed_python_session(
    binary: Path, execution: Execution
) -> Transcript:
    return describe_session(binary, execution)


def describe_session(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), selected_environment(path)) as client:
            client.initialize_and_list_tools()
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_python_execution(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        env = selected_environment(workspace)
        env["RETICULATE_PYTHON"] = sys.executable
        with McpClient(
            binary,
            execution.serve(),
            env,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="recorded_value = 41; recorded_value + 1")
            assert last_result_text(client) == "42\n"
            client.send(python="raise ValueError('recorded failure')")
            assert "ValueError: recorded failure" in last_result_text(client)
            client.send(control="restart", python="'recorded_value' in globals()")
            assert (
                last_result_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\nFalse\n[done]"
            )
            records = client.finish()
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            markdown = (session / "transcript.md").read_text()
            quarto = (session / "transcript.qmd").read_text()
            assert "recorded_value = 41" in markdown and "recorded_value = 41" in quarto
            assert "  python-packages: []\n" in quarto
            assert "ValueError: recorded failure" in markdown
            assert (session / "outputs/call-000001.log").read_text() == "42\n"
            events = [
                json.loads(line)
                for line in (session / "internal/events.jsonl").read_text().splitlines()
            ]
            assert events[0]["event"] == "session_started"
            assert sum(event["event"] == "tool_call" for event in events) == 3
            records.append(
                {
                    "recording": {
                        "events": [event["event"] for event in events],
                        "markdown and quarto": "Python source retained",
                        "output log": "42\n",
                    }
                }
            )
            return records


@executions(DIRECT, SANDBOXED)
def test_preserves_explicit_python_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        env = environment(Path(directory))
        env["RETICULATE_PYTHON"] = sys.executable
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import os, sys; assert sys.executable == os.environ['RETICULATE_PYTHON']; identity = object(); identity_id = id(identity); 42"
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            for control in ({}, {"control": "restart"}):
                result = client.send(**control, requirements={"python": ["py-yaml12"]})
                assert result["isError"], result
                assert "non-managed Python session" in last_result_text(client)
                client.send(python="assert id(identity) == identity_id; 42")
                assert last_result_text(client) == "42\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_and_replaces_a_failed_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), selected_environment(path)) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 41")
            wait_for_evaluation_output(
                client,
                "loop entered\n\n[running; poll with an empty send]",
                "Python loop entry",
                # fmt: python
                python=code("""
                    print("loop entered")
                    # Keep interrupt locations stable across Python bytecode versions.
                    while True: pass  # fmt: skip
                    """),
                timeout_ms=100,
            )
            client.send(control="interrupt")
            assert "KeyboardInterrupt" in last_result_text(client), client.transcript[
                -1
            ]
            client.send(python="retained + 1")
            assert last_result_text(client) == "42\n"
            client.send(python="import tempfile; print(tempfile.gettempdir())")
            temporary = Path(last_result_text(client).strip())
            client.send(python="import os; os._exit(23)")
            assert "status 23" in last_result_text(client), client.transcript[-1]
            client.send(python="assert 'retained' not in globals(); 42")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            assert not temporary.exists(), "failed worker storage remains"
            records = client.finish()
            for record in records:
                for content in record.get("result", {}).get("content", []):
                    if content["type"] == "text":
                        content["text"] = content["text"].replace(
                            str(temporary), "<worker temporary>"
                        )
            return records


@executions(SANDBOXED)
def test_preserves_explicit_selection_in_sandbox_environment(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for inherit in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "inherit_environment": inherit,
                            "environment": {
                                "RETICULATE_PYTHON": "/invalid/project/python"
                            },
                        }
                    }
                )
            )
            env = environment(workspace)
            env["RETICULATE_PYTHON"] = sys.executable
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                for control in ({}, {"control": "restart"}):
                    client.send(
                        **control,
                        # fmt: python
                        python=code("""
                            import os
                            import sys

                            assert os.environ["RETICULATE_PYTHON"] == sys.executable
                            print("explicit selection retained")
                            """),
                    )
                    assert "explicit selection retained\n" in last_result_text(client)
                records.extend(client.finish())
    return records


@executions(DIRECT, SANDBOXED)
def test_inspection_excludes_workspace_and_pythonpath(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "python3").symlink_to(sys.executable)
        poisoned_path = workspace / "pythonpath"
        poisoned_path.mkdir()
        # fmt: python
        payload = code("""
            from pathlib import Path

            Path("host-import-executed").touch()
            raise RuntimeError("inspection imported workspace code")
            """)
        (workspace / "ctypes.py").write_text(payload)
        (poisoned_path / "sitecustomize.py").write_text(payload)
        env = selected_environment(workspace)
        env["RETICULATE_PYTHON"] = sys.executable
        env["PYTHONPATH"] = str(poisoned_path)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            assert not (workspace / "host-import-executed").exists()
            # Workload imports keep their ordinary semantics inside the worker.
            (workspace / "ctypes.py").unlink()
            (poisoned_path / "sitecustomize.py").unlink()
            shutil.rmtree(poisoned_path / "__pycache__", ignore_errors=True)
            client.send(python="41 + 1")
            assert last_result_text(client) == "42\n"
            return client.finish()


def failed_native_startup(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        probe = build_interposer(workspace, "python_exit_state")
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        site = Path(
            subprocess.check_output(
                [selected, "-c", "import site; print(site.getsitepackages()[0])"],
                text=True,
            ).strip()
        )
        # A real installed startup hook changes only the embedded worker.
        # fmt: python
        hook = code(f"""
            import os
            import sys

            if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                import ctypes
                from pathlib import Path

                probe = ctypes.CDLL({str(probe)!r})
                Path("startup-temporary").write_text(os.environ["TMPDIR"])
                sys.prefix = "changed-by-startup-hook"
            """)
        (site / "sitecustomize.py").write_text(hook)
        arguments = (
            execution.serve("--writable-root", str(workspace))
            if execution == SANDBOXED
            else execution.serve()
        )
        env = selected_environment(venv / "bin")
        env["RETICULATE_PYTHON"] = str(selected)
        with McpClient(binary, arguments, env, workspace) as client:
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('failed startup ran cell')"
            )
            assert result["isError"], result
            records = client.finish()
            temporary = Path((workspace / "startup-temporary").read_text())
            assert not temporary.exists(), "failed worker storage remains"
            assert selected.exists(), "failed startup removed selected environment"
            for record in records:
                for content in record.get("result", {}).get("content", []):
                    if content["type"] == "text":
                        content["text"] = content["text"].replace(
                            str(venv), "<selected environment>"
                        )
            return records


@executions(DIRECT, SANDBOXED)
def test_startup_failure_restores_python_thread(
    binary: Path, execution: Execution
) -> Transcript:
    records = failed_native_startup(binary, execution)
    diagnostic = records[-1]["result"]["content"][0]["text"]
    assert "Python exit thread attached\n" in diagnostic, diagnostic
    assert "Python exit thread detached" not in diagnostic, diagnostic
    return records


@executions(DIRECT, SANDBOXED)
def test_startup_failure_preserves_python_exception(
    binary: Path, execution: Execution
) -> Transcript:
    records = failed_native_startup(binary, execution)
    diagnostic = records[-1]["result"]["content"][0]["text"]
    assert (
        "RuntimeError: embedded Python prefix differs from the selected environment"
        in diagnostic
    ), diagnostic
    assert "'changed-by-startup-hook' != '<selected environment>'" in diagnostic, (
        diagnostic
    )
    return records


@executions(DIRECT, SANDBOXED)
def test_uses_environment_through_directory_alias(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        original = workspace / "original"
        original.mkdir()
        venv = original / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        alias = workspace / "alias"
        alias.symlink_to(original, target_is_directory=True)
        env = selected_environment(alias / "environment/bin")
        env["MCP_CONSOLE_TEST_ENVIRONMENT"] = str(venv)
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys
                    import subprocess

                    assert os.environ["RETICULATE_PYTHON"] == sys.executable
                    assert os.path.samefile(sys.prefix, os.environ["MCP_CONSOLE_TEST_ENVIRONMENT"])
                    assert os.path.samefile(sys.exec_prefix, sys.prefix)
                    child = subprocess.check_output(
                        [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                    ).strip()
                    assert os.path.samefile(child, sys.prefix)
                    print("selected environment retained through directory alias")
                    """),
            )
            assert (
                last_result_text(client)
                == "selected environment retained through directory alias\n"
            ), client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_consumes_idle_interrupt_before_next_python_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), selected_environment(path)) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 40")
            client.send(control="interrupt")
            client.send(python="retained += 1; retained")
            assert last_result_text(client) == "41\n", client.transcript[-1]
            client.send(control="interrupt", python="retained += 1; retained")
            assert last_result_text(client) == "42\n[done]", client.transcript[-1]
            client.send(python="retained")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_imports_workspace_modules_without_pythonpath(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (root / "python3").symlink_to(sys.executable)
        (workspace / "workspace_module.py").write_text("value = 20\n")
        package = workspace / "workspace_package"
        package.mkdir()
        (package / "__init__.py").write_text("value = 22\n")
        subdirectory = workspace / "subdirectory"
        subdirectory.mkdir()
        (subdirectory / "after_chdir.py").write_text("value = 43\n")
        env = selected_environment(root)
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import sys
                        import workspace_module
                        import workspace_package

                        assert "PYTHONPATH" not in os.environ
                        assert sys.path[0] == ""
                        assert workspace_module.value + workspace_package.value == 42
                        os.chdir("subdirectory")
                        import after_chdir

                        after_chdir.value
                        """),
                )
                assert "43\n" in last_result_text(client), client.transcript[-1]
                assert not client.transcript[-1]["result"]["isError"]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_ignores_pythonhome_for_selected_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return ignores_python_layout_override(binary, execution, "PYTHONHOME")


@executions(DIRECT, SANDBOXED)
def test_ignores_pythonplatlibdir_for_selected_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return ignores_python_layout_override(binary, execution, "PYTHONPLATLIBDIR")


def ignores_python_layout_override(
    binary: Path, execution: Execution, variable: str
) -> Transcript:
    records = []
    for inherit in (True, False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            venv = root / "environment"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", venv],
                check=True,
                capture_output=True,
            )
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "inherit_environment": inherit,
                            "environment": {variable: "unavailable-configured-layout"},
                        }
                    }
                )
            )
            env = selected_environment(venv / "bin")
            env[variable] = "unavailable-inherited-layout"
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                for control in ({}, {"control": "restart"}):
                    client.send(
                        **control,
                        # fmt: python
                        python=code(f"""
                            import os
                            import sys
                            import subprocess

                            assert "{variable}" not in os.environ
                            assert os.environ["RETICULATE_PYTHON"] == sys.executable
                            assert os.path.samefile(sys.prefix, "../environment")
                            assert sys.prefix != sys.base_prefix
                            child = subprocess.check_output(
                                [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                            ).strip()
                            assert os.path.samefile(child, sys.prefix)
                            print("selected environment retained")
                            """),
                    )
                    assert "selected environment retained\n" in last_result_text(
                        client
                    ), client.transcript[-1]
                records.extend(client.finish())
    return records


@executions(DIRECT, SANDBOXED)
def test_accepts_parent_components_in_selected_executable(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        (workspace / "alias").mkdir()
        selected = workspace / "alias/../environment/bin/python3"
        env = environment(workspace)
        env["RETICULATE_PYTHON"] = str(selected)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import subprocess
                        import sys

                        selected = os.environ["RETICULATE_PYTHON"]
                        assert "/../" in selected
                        assert sys.executable == selected
                        assert os.path.samefile(sys.prefix, "environment")
                        assert sys.prefix != sys.base_prefix
                        child = subprocess.check_output(
                            [sys.executable, "-c", "import sys; print(sys.executable); print(sys.prefix)"],
                            text=True,
                        ).splitlines()
                        assert os.path.samefile(child[0], selected)
                        assert os.path.samefile(child[1], sys.prefix)
                        print("selected executable and environment retained")
                        """),
                )
                assert (
                    "selected executable and environment retained\n"
                    in last_result_text(client)
                ), client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_excludes_executable_directory_from_imports(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--copies", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = venv / "bin/python3"
        site = Path(
            subprocess.check_output(
                [selected, "-I", "-c", "import site; print(site.getsitepackages()[0])"],
                text=True,
            ).strip()
        )
        (site / "selected_package.py").write_text("value = 42\n")
        (venv / "bin/json.py").write_text(
            "raise RuntimeError('imported executable directory')\n"
        )
        (venv / "bin/selected_package.py").write_text("value = -1\n")
        env = selected_environment(venv / "bin")
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import json
                        import os
                        import sys
                        import selected_package

                        assert sys.path[0] == ""
                        assert os.path.dirname(sys.executable) not in sys.path
                        assert selected_package.value == 42
                        json.dumps({"selected package": selected_package.value})
                        """),
                )
                assert not client.transcript[-1]["result"]["isError"], (
                    client.transcript[-1]
                )
                assert "'{\"selected package\": 42}'\n" in last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_managed_python_defaults(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        (workspace / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            binary, execution.serve(), environment(workspace), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import numpy, pandas

                    retained = 42
                    retained
                    """)
            )
            assert last_result_text(client) == "42\n"
            client.send(requirements={"python": ["six"]})
            assert client.transcript[-1]["result"]["isError"]
            client.send(python="retained")
            assert last_result_text(client) == "42\n"
            records = client.finish()
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        quarto = (session / "transcript.qmd").read_text()
        assert "  python-packages:\n    - numpy\n    - pandas\n" in quarto, quarto
        assert "  packages: []\n" in quarto, quarto
        assert "six" not in quarto, quarto
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        assert events[0]["dynamic_resolution"] is False
        assert events[0]["python_preparation"] is True
        return TranscriptWithCompanions(
            records, {"qmd": quarto.replace(str(workspace.resolve()), "<workspace>")}
        )


@requires(UNPRIVILEGED)
@executions(DIRECT)
def test_reports_direct_storage_retirement_failure(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for stage in ("restart", "shutdown", "startup failure"):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            venv = workspace / "environment"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", venv],
                check=True,
                capture_output=True,
            )
            site = Path(
                subprocess.check_output(
                    [
                        venv / "bin/python3",
                        "-I",
                        "-c",
                        "import site; print(site.getsitepackages()[0])",
                    ],
                    text=True,
                ).strip()
            )
            # fmt: python
            restrict = code("""
                import os
                from pathlib import Path

                temporary = Path(os.environ["TMPDIR"])
                Path("worker-temporary").write_text(str(temporary))
                restricted = temporary / "restricted"
                restricted.mkdir()
                (restricted / "retained.txt").write_text("private contents")
                restricted.chmod(0)
                """)
            if stage == "startup failure":
                # fmt: python
                hook = code("""
                    import os
                    from pathlib import Path

                    if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                        temporary = Path(os.environ["TMPDIR"])
                        Path("worker-temporary").write_text(str(temporary))
                        restricted = temporary / "restricted"
                        restricted.mkdir()
                        (restricted / "retained.txt").write_text("private contents")
                        restricted.chmod(0)
                        os._exit(47)
                    """)
                (site / "sitecustomize.py").write_text(hook)
            try:
                with McpClient(
                    binary,
                    execution.serve(),
                    selected_environment(venv / "bin"),
                    workspace,
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(python=restrict if stage != "startup failure" else "42")
                    if stage == "restart":
                        client.send(python="temporary.chmod(0)")
                        client.send(
                            control="restart", python="print('replacement ran')"
                        )
                    if stage != "shutdown":
                        assert client.transcript[-1]["result"]["isError"], (
                            client.transcript[-1]
                        )
                        assert (
                            "cannot remove worker temporary directory"
                            in last_result_text(client)
                        ), client.transcript[-1]
                        assert "replacement ran\n" not in last_result_text(client)
                    client.stdin.close()
                    client.process.wait(timeout=15)
                    stderr = client.stderr.read()
                    if stage == "startup failure":
                        # Startup already delivered its retirement error over MCP.
                        assert client.process.returncode == 0 and stderr == "", stderr
                    else:
                        assert client.process.returncode != 0, (stage, stderr)
                        assert "cannot remove worker temporary directory" in stderr, (
                            stderr
                        )
                    temporary = Path((workspace / "worker-temporary").read_text())
                    assert temporary.exists()
                    assert (venv / "bin/python3").exists()
                    records.append({"stage": stage})
                    records.extend(client.transcript)
                    records.append({"stderr": stderr})
                    # Normalize only this owned, run-specific path.
                    for record in records:
                        for content in record.get("result", {}).get("content", []):
                            if content["type"] == "text":
                                content["text"] = content["text"].replace(
                                    str(temporary), "<worker temporary>"
                                )
                        if "stderr" in record:
                            record["stderr"] = record["stderr"].replace(
                                str(temporary), "<worker temporary>"
                            )
            finally:
                marker = workspace / "worker-temporary"
                if marker.exists():
                    temporary = Path(marker.read_text())
                    if temporary.exists():
                        temporary.chmod(0o700)
                        (temporary / "restricted").chmod(0o700)
                        shutil.rmtree(temporary)
    return records
