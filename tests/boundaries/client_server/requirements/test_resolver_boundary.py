"""Preparation crosses the real native resolver boundary, including inspection."""

import os
import json
import shutil
import functools
import http.server
import tarfile
import threading
import select
import signal
from contextlib import contextmanager, closing
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import last_result_text
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    SANDBOX,
    command,
    requires,
)
from support.native import LOADER_VARIABLE, build_interposer
from support.checkpoints import FifoCheckpoint
from support.execution import SANDBOXED
from support.r import r_test_environment
from support.resolvers import (
    fake_ir_environment,
    resolver_fixture_arguments,
    resolver_fixture_directory,
)
from support.processes import capture_process_identity, child_process_identities
from boundaries.client_server._harness import wait_for_stopped_process
from support.ssh import SSH, configure, localhost, poison_controller, remote_command


@contextmanager
def source_repository(root: Path, backend: str):
    source = root / "source"
    source.mkdir()
    (source / "backend.py").write_text(backend)
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n[project]\nname = "resolver-probe"\nversion = "1.0"\n'
    )
    downloads = root / "downloads"
    downloads.mkdir()
    with tarfile.open(downloads / "resolver_probe-1.0.tar.gz", "w:gz") as archive:
        archive.add(source, arcname="resolver_probe-1.0")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(downloads))
    )
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    try:
        # Use the proxy even though renv's local IPC allowance sets NO_PROXY
        # for bare localhost. On Linux the server is outside the private netns.
        yield f"http://localhost.:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join()


def managed_python_environment(root: Path) -> dict[str, str]:
    path = root / "path"
    path.mkdir()
    uv = shutil.which("uv")
    assert uv is not None
    (path / "uv").symlink_to(uv)
    environment = dict(os.environ, PATH=str(path), XDG_CACHE_HOME=str(root / "cache"))
    for name in ["R_HOME", "RETICULATE_PYTHON", "RETICULATE_UV", "UV_NO_BUILD"]:
        environment.pop(name, None)
    return environment


@requires(SANDBOX)
def test_selected_python_startup_hook_is_confined(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        venv = root / "python"
        subprocess.run(
            [str(Path(sys.executable).resolve()), "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        marker = root / "outside-resolver-storage"
        site = next((venv / "lib").glob("python*/site-packages"))
        hook = (
            # fmt: python
            code("""
                import errno
                from pathlib import Path

                try:
                    Path(MARKER).write_text("startup hook escaped")
                except OSError as error:
                    assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
                """)
        ).replace("MARKER", repr(str(marker)))
        (site / "sitecustomize.py").write_text(hook)
        path = root / "path"
        path.mkdir()
        environment = dict(
            os.environ,
            PATH=str(path),
            RETICULATE_PYTHON=str(venv / "bin/python"),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        environment.pop("R_HOME", None)
        with McpClient(binary, ("serve",), environment) as client:
            client.initialize_and_list_tools()
            assert not marker.exists(), "Python inspection executed outside the sandbox"
            client.send(python="print(42)")
            assert not marker.exists(), "worker startup escaped its write policy"
            return client.finish()


@requires(SANDBOX, NATIVE_FIXTURES)
def test_loader_environment_is_applied_only_after_enforcement(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        library = build_interposer(root, "resolver_loader_probe")
        outside = root / "outside-loader-hook"
        inside = root / "cache/mcp-console/resolver/payload/loader-hook"
        path = root / "path"
        path.mkdir()
        selected = path / "python3"
        selected.symlink_to(Path(sys.executable).resolve())
        secret = root / "private-secret"
        secret.write_text("must not be readable through an executable alias")
        environment = dict(
            os.environ,
            PATH=str(path),
            RETICULATE_PYTHON=str(selected),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        environment.pop("R_HOME", None)
        workspace = root / "workspace"
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "resolver": {
                        "readable_roots": [str(library)],
                        "environment": {
                            LOADER_VARIABLE: str(library),
                            "RESOLVER_PROBE_OUTSIDE": str(outside),
                            "RESOLVER_PROBE_INSIDE": str(inside),
                            "RESOLVER_PROBE_SECRET": str(secret),
                        },
                    }
                }
            )
        )
        with McpClient(
            binary, ("serve",), environment, current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            assert inside.read_text().splitlines(), (
                "resolver workload did not load the startup hook"
            )
            assert "secret-readable" not in inside.read_text(), (
                "Python executable alias exposed its parent directory"
            )
            assert not outside.exists(), (
                "broker or native launcher loaded the hostile hook before enforcement"
            )
            client.send(python="print('prepared with confined startup hooks')")
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            return client.finish()


@requires(MACOS_SANDBOX, command("xcode-select"))
def test_resolver_uses_selected_developer_tools(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        developer = Path(
            subprocess.check_output(["xcode-select", "-p"], text=True).strip()
        )
        # Full Xcode has required frameworks beside Contents/Developer. Exercise
        # that layout even when the host normally selects CommandLineTools.
        bundles = sorted(Path("/Applications").glob("Xcode*.app/Contents/Developer"))
        if bundles:
            developer = bundles[0]
        venv = root / "python"
        subprocess.run(
            [str(Path(sys.executable).resolve()), "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        site = next((venv / "lib").glob("python*/site-packages"))
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code("""
                import json
                import os
                from pathlib import Path
                import subprocess

                if payload := os.environ.get("MCP_CONSOLE_RESOLVER_PAYLOAD"):
                    make = subprocess.run(
                        ["/usr/bin/make", "--version"], capture_output=True, text=True
                    )
                    (Path(payload) / "developer-tools.json").write_text(
                        json.dumps(
                            {
                                "developer": os.environ.get("DEVELOPER_DIR"),
                                "status": make.returncode,
                                "stdout": make.stdout,
                                "stderr": make.stderr,
                            }
                        )
                    )
                """)
        )
        path = root / "path"
        path.mkdir()
        environment = dict(
            os.environ,
            PATH=str(path),
            RETICULATE_PYTHON=str(venv / "bin/python"),
            DEVELOPER_DIR=str(developer),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        environment.pop("R_HOME", None)
        with McpClient(binary, ("serve",), environment) as client:
            client.initialize_and_list_tools()
            result = json.loads(
                (
                    root / "cache/mcp-console/resolver/payload/developer-tools.json"
                ).read_text()
            )
            assert result["developer"] == str(developer), result
            assert result["status"] == 0, result
            assert "GNU Make" in result["stdout"], result
            client.send(python="print('selected developer tools are available')")
            return client.finish()


@requires(SANDBOX)
def test_cold_python_storage_and_restart(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        path = root / "path"
        path.mkdir()
        uv = shutil.which("uv")
        assert uv is not None
        (path / "uv").symlink_to(uv)
        host_cache = root / "host-uv"
        host_cache.mkdir()
        (host_cache / "sentinel").write_text("host cache")
        environment = dict(
            os.environ,
            PATH=str(path),
            XDG_CACHE_HOME=str(root / "cache"),
            UV_CACHE_DIR=str(host_cache),
        )
        for name in ["R_HOME", "RETICULATE_PYTHON", "RETICULATE_UV"]:
            environment.pop(name, None)
        payload = root / "cache/mcp-console/resolver/payload"
        assert not payload.exists()
        with McpClient(binary, ("serve",), environment) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={"python": ["six"]},
                python="import sys, six; print(sys.executable)",
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            selected = Path(last_result_text(client).strip())
            assert selected.is_relative_to(payload), selected
            client.transcript[-1]["result"]["content"][0]["text"] = "<managed-python>\n"
            client.send(python="retained = object(); retained_id = id(retained)")
            client.send(
                control="restart",
                requirements={"python": ["mcp-console-no-such-resolver-package==0"]},
                stdin="queued\n",
                python="raise AssertionError('must not run')",
            )
            assert "resolution failed" in last_result_text(client), client.transcript[
                -1
            ]
            client.send(python="assert id(retained) == retained_id; print('preserved')")
            assert last_result_text(client) == "preserved\n", client.transcript[-1]
            client.send(
                control="restart",
                requirements={"python": ["packaging"]},
                python="import packaging, six; print('prepared')",
            )
            assert "prepared\n" in last_result_text(client), client.transcript[-1]
            assert sorted(p.name for p in host_cache.iterdir()) == ["sentinel"]
            transcript = client.finish()
        metadata = json.loads((payload.parent / "control/metadata.json").read_text())
        assert metadata["leases"] == 0, metadata
        return transcript


@requires(SANDBOX, command("R"), command("uv"))
def test_cold_r_python_and_duckdb_storage(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["XDG_CACHE_HOME"] = str(root / "cache")
        # Exercise ir's cold uv-tool bootstrap, not an installed host ir.
        path = root / "path"
        path.mkdir()
        uv = shutil.which("uv")
        assert uv is not None
        (path / "uv").symlink_to(uv)
        environment["PATH"] = os.pathsep.join(
            [str(path), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        )
        environment["RETICULATE_UV"] = "managed"
        host_caches = []
        for variable in [
            "UV_CACHE_DIR",
            "IR_CACHE_DIR",
            "RENV_PATHS_CACHE",
            "R_USER_CACHE_DIR",
        ]:
            host_cache = root / variable.lower()
            host_cache.mkdir()
            (host_cache / "sentinel").write_text("host cache must remain untouched")
            environment[variable] = str(host_cache)
            host_caches.append(host_cache)
        payload = root / "cache/mcp-console/resolver/payload"
        assert not payload.exists()
        with McpClient(binary, ("serve",), environment) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={"r": ["praise"], "python": ["six"], "duckdb": ["httpfs"]}
            )
            assert last_result_text(client) == "[prepared]", client.transcript[-1]
            assert list(payload.glob("**/uv/bin/uv")), (
                "explicit managed uv bootstrap did not run inside resolver storage"
            )
            client.send(r="cat(normalizePath(find.package('praise')))")
            library = Path(last_result_text(client))
            assert library.is_relative_to(payload), library
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<resolver-payload>/r-library/praise"
            )
            client.send(
                python="import six; retained = object(); retained_id = id(retained)"
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            client.send(sql="LOAD httpfs; SELECT 42 AS answer")
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            assert list((payload / "extensions").glob("**/httpfs.duckdb_extension")), (
                "cold extension download did not use resolver storage"
            )
            client.send(
                control="restart",
                requirements={"r": ["mcpconsolenosuchpackage"]},
                python="raise AssertionError('failed preparation ran code')",
                stdin="must not be queued\n",
            )
            assert client.transcript[-1]["result"].get("isError"), client.transcript[-1]
            client.send(python="assert id(retained) == retained_id; print('preserved')")
            assert last_result_text(client) == "preserved\n", client.transcript[-1]
            for host_cache in host_caches:
                assert sorted(path.name for path in host_cache.iterdir()) == [
                    "sentinel"
                ], host_cache
            return client.finish()


@requires(SANDBOX, SSH)
def test_ssh_uses_execution_host_broker(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        remote = root / "remote"
        remote.mkdir()
        remote_environment = managed_python_environment(remote)
        command = remote_command(remote, binary, remote_environment)
        local = root / "local"
        local.mkdir()
        configure(local, remote, command)
        # Execution-host project settings must not replace captured policy.
        configure(
            remote,
            remote,
            ["must-not-run"],
            sandbox={"filesystem": {"kind": "unrestricted"}},
        )
        with localhost(root / "sshd") as environment:
            trap = poison_controller(root / "sshd", environment)
            with McpClient(
                binary, ("serve",), environment, current_directory=local
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    requirements={"python": ["six"]},
                    python="import sys, six; print(sys.executable)",
                )
                selected = Path(last_result_text(client).strip())
                assert selected.is_relative_to(
                    remote / "cache/mcp-console/resolver/payload"
                ), client.transcript[-1]
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    str(selected.parent.parent.parent / "<environment>" / "bin/python")
                    + "\n"
                )
                client.send(
                    control="restart",
                    requirements={"python": ["packaging"]},
                    python="import six, packaging; print('remote prepared')",
                )
                assert "remote prepared\n" in last_result_text(client), (
                    client.transcript[-1]
                )
                assert not trap.exists(), "controller executed a resolve"
                transcript = client.finish()
        metadata = json.loads(
            (remote / "cache/mcp-console/resolver/control/metadata.json").read_text()
        )
        assert metadata["leases"] == 0, metadata
        return json.loads(json.dumps(transcript).replace(str(root), "<ssh-test>"))


@requires(SANDBOX)
def test_rejects_console_installed_in_resolver_storage(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        prefix = root / "cache/mcp-console/resolver/payload/installation"
        (prefix / "bin").mkdir(parents=True)
        relocated = prefix / "bin/mcp-console"
        shutil.copy2(binary, relocated)
        for relative in ["libexec", "share/licenses/mcp-console"]:
            shutil.copytree(binary.parent.parent / relative, prefix / relative)
        environment = dict(os.environ, XDG_CACHE_HOME=str(root / "cache"))
        worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
        with McpClient(
            relocated, ("serve", "--worker", str(worker)), environment
        ) as client:
            try:
                client.initialize_and_list_tools()
            except AssertionError as error:
                diagnostic = str(error)
                assert "outside resolver storage" in diagnostic, diagnostic
            else:
                raise AssertionError(
                    "server accepted a sandbox-writable Console installation"
                )
        assert relocated.exists(), "cleanup removed the running installation"
        return [{"error": diagnostic}]


@requires(SANDBOX)
def test_rejected_worker_policy_releases_unused_storage(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        path = root / "path"
        path.mkdir()
        (path / "python3").symlink_to(sys.executable)
        environment = dict(
            os.environ, PATH=str(path), XDG_CACHE_HOME=str(root / "cache")
        )
        for name in ["R_HOME", "RETICULATE_PYTHON", "RETICULATE_UV"]:
            environment.pop(name, None)
        with McpClient(
            binary, ("serve", "--writable-root", str(root)), environment
        ) as client:
            assert client.process.wait(timeout=30) != 0
            diagnostic = client.stderr.read()
            assert "without custom filesystem rules" in diagnostic, diagnostic
        metadata = json.loads(
            (root / "cache/mcp-console/resolver/control/metadata.json").read_text()
        )
        assert metadata["leases"] == 0, metadata
        return [{"stderr": diagnostic}]


@requires(SANDBOX)
def test_direct_execution_keeps_host_cache_namespace(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        environment = managed_python_environment(root)
        payload = root / "cache/mcp-console/resolver/payload"
        records = []
        for arguments, confined in [
            (("serve",), True),
            (("serve", "--no-sandbox"), False),
        ]:
            with McpClient(binary, arguments, environment) as client:
                client.initialize_and_list_tools()
                client.send(python="import sys; print(sys.executable)")
                selected = Path(last_result_text(client).strip())
                assert selected.is_relative_to(payload) == confined, client.transcript[
                    -1
                ]
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<resolver-payload-python>\n" if confined else "<host-python>\n"
                )
                records.extend(client.finish())
        return records


@requires(SANDBOX)
def test_python_manifest_reset_and_recording(binary: Path) -> TranscriptWithCompanions:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        with McpClient(
            binary,
            ("serve",),
            managed_python_environment(root),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "get"})
            defaults = json.loads(last_result_text(client))["requirements"]
            client.send(
                requirements={"python": ["six"]}, python="import six; retained = 42"
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            quarto = session / "transcript.qmd"
            assert "    - six\n" in quarto.read_text(), (
                "accepted Python requirements were not recorded"
            )
            client.send(stdin="preserved input\n")
            client.send(
                control="restart",
                requirements={"python": ["mcp-console-no-such-resolver-package==0"]},
                stdin="rejected input\n",
                python="raise AssertionError('failed candidate ran')",
            )
            assert client.transcript[-1]["result"].get("isError"), client.transcript[-1]
            assert "mcp-console-no-such-resolver-package" not in quarto.read_text()
            client.send(
                python="assert retained == 42; assert input() == 'preserved input'; print('preserved')"
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            assert last_result_text(client).endswith("preserved\n"), client.transcript[
                -1
            ]
            client.send(
                control="restart",
                requirements={"action": "reset"},
                python="assert 'retained' not in globals(); print('reset')",
            )
            assert "reset\n" in last_result_text(client), client.transcript[-1]
            client.send(requirements={"action": "get"})
            assert json.loads(last_result_text(client))["requirements"] == defaults
            final_quarto = quarto.read_text()
            assert '"six"' not in final_quarto and "- six" not in final_quarto
            return TranscriptWithCompanions(
                client.finish(),
                # Keep the generated document verbatim when formatting the
                # repository, including submitted code and JSON layout.
                {
                    "qmd": "<!-- fmt: off -->\n"
                    + final_quarto.replace(str(workspace), "<workspace>")
                    + "<!-- fmt: on -->\n"
                },
            )


@requires(SANDBOX, command("cc"))
def test_real_build_hook_and_result_substitution(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        outside = root / "escaped"
        host_cache = root / "host-cache"
        host_cache.mkdir()
        secret = root / "secret"
        secret.write_text("fixture secret must stay private")
        backend = (
            Path(__file__).resolve().parents[3] / "fixtures/resolver_build_backend.py"
        )
        program = (
            backend.read_text()
            .replace("@OUTSIDE@", str(outside))
            .replace("@HOST_CACHE@", str(host_cache / "escaped"))
            .replace("@SECRET@", str(secret))
            .replace("@CC@", shutil.which("cc"))
            .replace(
                '"@RELEASE_FILES@"',
                repr(
                    {
                        path: Path(path).read_text()
                        for path in ["/etc/os-release", "/etc/redhat-release"]
                        if Path(path).is_file()
                    }
                ),
            )
        )
        with source_repository(root, program) as url:
            environment = dict(
                managed_python_environment(root),
                UV_FIND_LINKS=url,
                UV_CACHE_DIR=str(host_cache),
            )
            workspace = root / "workspace"
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"resolver": {"allowed_hosts": ["localhost"]}})
            )
            with McpClient(
                binary, ("serve",), environment, current_directory=workspace
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    requirements={"python": ["resolver-probe==1.0"]},
                    python="import json, resolver_probe; print(json.dumps(resolver_probe.evidence))",
                )
                evidence = json.loads(last_result_text(client))
                assert (
                    evidence["outside"]
                    == evidence["host_cache"]
                    == evidence["secret"]
                    == "denied"
                ), evidence
                assert "403" in evidence["network"], evidence
                assert evidence["compiler"] == "native build succeeded", evidence
                assert Path(evidence["working_directory"]).is_relative_to(
                    root / "cache/mcp-console/resolver/payload"
                ), evidence
                assert not outside.exists()
                assert not list(host_cache.iterdir())
                assert secret.read_text() == "fixture secret must stay private"
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    last_result_text(client).replace(
                        evidence["working_directory"], "<source-build-directory>"
                    )
                )
                transcript = client.finish()
        return transcript


@requires(SANDBOX)
def test_interrupt_and_server_death_retire_build_descendants(
    binary: Path,
) -> Transcript:
    transcripts = []
    for stop in ["interrupt", "input_closed", "server_death"]:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            payload = root / "cache/mcp-console/resolver/payload"
            lifetime = payload / "descendant-lifetime"
            gate = payload / "descendant-gate"
            # fmt: python
            child = code("""
                import os, signal, sys

                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                with open(sys.argv[1], "wb", buffering=0) as lifetime:
                    lifetime.write(b"1")
                    with open(sys.argv[2], "rb", buffering=0) as gate:
                        gate.read(1)
                """)
            # fmt: python
            backend = (
                code("""
                import subprocess, sys


                def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
                    child = subprocess.Popen(
                        [sys.executable, "-c", CHILD, LIFETIME, GATE], start_new_session=True
                    )
                    child.wait()
                    raise AssertionError("blocked build must be retired")
                """)
                .replace("CHILD", repr(child))
                .replace("LIFETIME", repr(str(lifetime)))
                .replace("GATE", repr(str(gate)))
            )
            with source_repository(root, backend) as url:
                environment = dict(managed_python_environment(root), UV_FIND_LINKS=url)
                with McpClient(
                    binary,
                    ("serve", "-c", 'resolver.allowed_hosts=["localhost"]'),
                    environment,
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(python="retained = 42")
                    os.mkfifo(lifetime)
                    os.mkfifo(gate)
                    reader = os.open(lifetime, os.O_RDONLY | os.O_NONBLOCK)
                    keeper = os.open(lifetime, os.O_WRONLY | os.O_NONBLOCK)
                    try:
                        preparing = client.start_send(
                            control="restart",
                            requirements={"python": ["resolver-probe==1.0"]},
                            python="raise AssertionError('candidate cell must not run')",
                        )
                        assert select.select([reader], [], [], 60)[0], (
                            "source build did not start"
                        )
                        assert os.read(reader, 1) == b"1"
                        os.close(keeper)
                        keeper = None
                        if stop == "interrupt":
                            interrupt = client.start_send(control="interrupt")
                            client.receive_many([preparing, interrupt])
                            assert preparing["result"]["isError"], preparing
                            assert (
                                "interrupted"
                                in preparing["result"]["content"][0]["text"]
                            ), preparing
                        elif stop == "input_closed":
                            client.stdin.close()
                        else:
                            client.process.kill()
                        assert select.select([reader], [], [], 10)[0], (
                            "native cleanup left detached build descendant alive"
                        )
                        assert os.read(reader, 1) == b"", "descendant did not retire"
                        if stop == "interrupt":
                            client.send(python="print(retained)")
                            assert last_result_text(client) == "42\n"
                            transcripts.extend(client.finish())
                        elif stop == "input_closed":
                            client.close()
                            assert client.process.returncode == 0
                        else:
                            client.close()
                            assert client.process.returncode == -9
                        if stop != "interrupt":
                            transcripts.append(
                                {
                                    "shutdown": stop,
                                    "detached_source_build_descendant": "retired",
                                }
                            )
                    finally:
                        if keeper is not None:
                            os.close(keeper)
                        os.close(reader)
    return transcripts


@requires(SANDBOX)
def test_interrupt_acknowledges_before_native_retirement(binary: Path) -> Transcript:
    with resolver_fixture_directory(binary, SANDBOXED) as root:
        library = root / "library"
        library.mkdir()
        environment = fake_ir_environment(root, [library])
        with (
            closing(FifoCheckpoint.create(root / "started")) as started,
            closing(FifoCheckpoint.create(root / "release")) as release,
        ):
            environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(started.path)
            environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(release.path)
            worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
            with McpClient(
                binary,
                SANDBOXED.serve(
                    *resolver_fixture_arguments(environment), "--worker", str(worker)
                ),
                environment,
            ) as client:
                client.initialize_and_list_tools()
                brokers = child_process_identities(
                    capture_process_identity(client.process.pid)
                )
                assert len(brokers) == 1, brokers
                preparing = client.start_send(requirements={"r": ["blocked"]})
                started.wait("resolver workload is running")
                launchers = child_process_identities(brokers[0])
                assert len(launchers) == 1, launchers
                launcher = launchers[0][0]
                os.kill(launcher, signal.SIGSTOP)
                try:
                    wait_for_stopped_process(
                        launcher, os.getpgid(launcher), client, "resolver launcher"
                    )
                    interrupt = client.start_send(control="interrupt", timeout_ms=0)
                    assert select.select([client.stdout], [], [], 3)[0], (
                        "interrupt acknowledgment waited for native retirement"
                    )
                    client.receive(interrupt)
                    assert interrupt["result"] == {
                        "content": [
                            {
                                "type": "text",
                                "text": "\n[running; poll with an empty send]",
                            }
                        ],
                        "isError": False,
                    }, interrupt
                    assert "result" not in preparing
                finally:
                    os.kill(launcher, signal.SIGCONT)
                client.receive(preparing)
                assert preparing["result"] == {
                    "content": [
                        {"type": "text", "text": "dependency resolution interrupted"}
                    ],
                    "isError": True,
                }, preparing
                client.send(r="echo worker usable")
                assert last_result_text(client) == "zod: worker usable\n"
                return client.finish()


@requires(SANDBOX)
def test_weekly_cleanup_waits_for_idle_sessions_and_restart(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        path = root / "path"
        path.mkdir()
        environment = dict(
            os.environ,
            PATH=str(path),
            RETICULATE_PYTHON=str(Path(sys.executable).resolve()),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        environment.pop("R_HOME", None)
        storage = root / "cache/mcp-console/resolver"
        payload = storage / "payload"
        metadata_path = storage / "control/metadata.json"
        transcripts = []
        with McpClient(binary, ("serve",), environment) as first:
            first.initialize_and_list_tools()
            first.send(python="retained = 42")
            assert not first.transcript[-1]["result"].get("isError"), first.transcript[
                -1
            ]
            marker = payload / "retained-payload"
            marker.write_text("live")
            metadata = json.loads(metadata_path.read_text())
            metadata["cleaned_at"] = 1
            metadata_path.write_text(json.dumps(metadata))
            with McpClient(binary, ("serve",), environment) as second:
                second.initialize_and_list_tools()
                assert marker.read_text() == "live"
                assert json.loads(metadata_path.read_text())["leases"] == 2
                first.send(control="restart", python="print('restarted')")
                assert (
                    last_result_text(first)
                    == "[worker stopped: in-memory state lost]\n[starting new worker]\nrestarted\n[done]"
                ), first.transcript[-1]
                assert marker.read_text() == "live"
                assert json.loads(metadata_path.read_text())["cleaned_at"] == 1
                # The worker can read the payload but cannot change artifacts,
                # broker metadata, or the executable used for the next broker.
                # fmt: python
                program = code("""
                    import errno
                    from pathlib import Path
                    for path in PROTECTED:
                        try:
                            with Path(path).open("r+b"):
                                pass
                        except OSError as error:
                            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
                            print("write denied")
                        else:
                            raise AssertionError("protected file was writable")
                    """).replace(
                    "PROTECTED", repr([str(marker), str(metadata_path), str(binary)])
                )
                second.send(python=program)
                assert last_result_text(second) == "write denied\n" * 3
                second.transcript[-1]["send"]["python"] = program.replace(
                    str(root), "<fixture>"
                ).replace(str(binary), "<console-executable>")
                transcripts.extend(second.finish())
            assert json.loads(metadata_path.read_text())["leases"] == 1
            assert marker.exists()
            transcripts.extend(first.finish())
        assert json.loads(metadata_path.read_text())["leases"] == 0
        external = root / "outside"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("must survive")
        (payload / "malicious-link").symlink_to(external, target_is_directory=True)
        with McpClient(binary, ("serve",), environment) as third:
            third.initialize_and_list_tools()
            assert not marker.exists()
            assert not (payload / "malicious-link").exists()
            assert sentinel.read_text() == "must survive"
            assert json.loads(metadata_path.read_text())["cleaned_at"] > 1
            third.send(python="print('fresh payload')")
            transcripts.extend(third.finish())
        return transcripts


@requires(SANDBOX)
def test_weekly_cleanup_after_broker_loss_waits_for_worker(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        path = root / "path"
        path.mkdir()
        environment = dict(
            os.environ,
            PATH=str(path),
            RETICULATE_PYTHON=str(Path(sys.executable).resolve()),
            XDG_CACHE_HOME=str(root / "cache"),
        )
        environment.pop("R_HOME", None)
        storage = root / "cache/mcp-console/resolver"
        metadata_path = storage / "control/metadata.json"
        with McpClient(binary, ("serve",), environment) as first:
            first.initialize_and_list_tools()
            brokers = child_process_identities(
                capture_process_identity(first.process.pid)
            )
            assert len(brokers) == 1, brokers
            # A workload must not inherit the launcher's lock descriptor: an
            # inherited duplicate could unlock that shared open description.
            # fmt: python
            first.send(
                python=code("""
                import fcntl, os
                for descriptor in os.listdir('/dev/fd'):
                    try:
                        fcntl.flock(int(descriptor), fcntl.LOCK_UN)
                    except OSError:
                        pass
                retained = 42
                """)
            )
            assert not first.transcript[-1]["result"].get("isError"), first.transcript[
                -1
            ]
            marker = storage / "payload/live-environment"
            marker.write_text("live")
            metadata = json.loads(metadata_path.read_text())
            metadata["cleaned_at"] = 1
            metadata_path.write_text(json.dumps(metadata))
            os.kill(brokers[0][0], signal.SIGKILL)
            with McpClient(binary, ("serve",), environment) as second:
                second.initialize_and_list_tools()
                assert marker.read_text() == "live", (
                    "cleanup removed a retained environment"
                )
                first.send(python="print(retained)")
                assert last_result_text(first) == "42\n"
                second.finish()
            first.close()
        with McpClient(binary, ("serve",), environment) as third:
            third.initialize_and_list_tools()
            assert not marker.exists(), "abandoned broker lease prevented idle cleanup"
            assert json.loads(metadata_path.read_text())["cleaned_at"] > 1
            third.finish()
        return [
            {
                "broker_loss": "live worker retained payload",
                "worker_retired": "weekly cleanup completed",
            }
        ]
