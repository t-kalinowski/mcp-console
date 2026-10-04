"""Resolver permissions through the public MCP preparation API."""

import json
import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SANDBOX, command, requires
from support.r import r_test_environment
from support.ssh import SSH, configure, localhost
from boundaries.client_server.python.test_without_r import environment


@requires(SANDBOX)
def test_default_resolver_permissions(binary: Path) -> Transcript:
    return permissions(binary, tailored=False)


@requires(SANDBOX)
def test_retains_tailored_resolver_policy(binary: Path) -> Transcript:
    return permissions(binary, tailored=True)


@requires(SANDBOX, SSH)
def test_ssh_resolver_permissions(binary: Path) -> Transcript:
    return permissions(binary, tailored=False, remote=True)


@requires(SANDBOX, R, command("ir"))
def test_cold_r_cache_resolution(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env, _ = r_test_environment()
        env.update(
            IR_CACHE_DIR=str(root / "ir"),
            R_USER_CACHE_DIR=str(root / "r-cache"),
            MCP_CONSOLE_LANGUAGES="r",
        )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={"action": "set", "r": ["utf8"]},
                r='cat(utf8::utf8_valid("resolved"), "\\n", sep = "")',
            )
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            assert last_tool_text(client).endswith("TRUE\n"), last_tool_text(client)
            client.finish()
        assert list((root / "ir").rglob("utf8/DESCRIPTION"))
        return [{"cold_ir_cache": True, "resolved_r_package": "utf8"}]


def permissions(binary: Path, *, tailored: bool, remote: bool = False) -> Transcript:
    from contextlib import ExitStack

    with ExitStack() as stack:
        directory = stack.enter_context(TemporaryDirectory())
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        cache = root / "uv-cache"
        cache.mkdir()
        protected = root / "protected"
        protected.mkdir()
        real_uv = shutil.which("uv")
        assert real_uv is not None
        # This is the selected resolver executable, before it delegates to uv.
        # Assert actual enforcement rather than relying on the sandbox marker.
        # fmt: python
        probe = code(r"""
            import errno
            import json
            import os
            from pathlib import Path
            import sys
            import urllib.request

            cache = Path(os.environ["UV_CACHE_DIR"])
            protected = Path(os.environ["RESOLVER_TEST_PROTECTED"])
            assert protected.joinpath("readable").read_text() == "host read"
            try:
                protected.joinpath("denied").write_text("host write")
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            else:
                raise AssertionError("resolver wrote outside its cache grants")
            assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
            try:
                urllib.request.urlopen("https://example.com", timeout=10)
            except urllib.error.URLError as error:
                assert "403" in str(error), error
            else:
                raise AssertionError("resolver reached a disallowed download host")
            approved = os.environ.get("RESOLVER_TEST_APPROVED")
            if approved:
                Path(approved).joinpath("created").write_text("custom grant")
                try:
                    Path(os.environ["IR_CACHE_DIR"]).joinpath("denied").write_text("excluded cache")
                except OSError as error:
                    assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                else:
                    raise AssertionError("explicit entries retained a default cache grant")
                with urllib.request.urlopen("https://www.python.org", timeout=10) as response:
                    assert response.status == 200
            cache.joinpath("probe.json").write_text(json.dumps({
                "host_read": True, "cache_write": True,
                "host_write_denied": True, "disallowed_host_denied": True,
            }))
            os.execv(os.environ["RESOLVER_TEST_UV"], ["uv", *sys.argv[1:]])
            """)
        (tools / "uv").write_text(f"#!{sys.executable}\n" + probe, encoding="utf-8")
        (tools / "uv").chmod(0o755)
        (protected / "readable").write_text("host read")
        env = dict(
            environment(tools),
            UV_CACHE_DIR=str(cache),
            MCP_CONSOLE_LANGUAGES="python",
            MCP_CONSOLE_HOME=str(root / "console-home"),
            RESOLVER_TEST_PROTECTED=str(protected),
            RESOLVER_TEST_UV=real_uv,
        )
        config = root / ".agents/console/config.yaml"
        if tailored:
            approved = root / "approved"
            approved.mkdir()
            excluded = root / "excluded-cache"
            excluded.mkdir()
            env["IR_CACHE_DIR"] = str(excluded)
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "resolver": {
                            "filesystem": {
                                "entries": [
                                    {
                                        "path": {
                                            "type": "special",
                                            "value": {"kind": "root"},
                                        },
                                        "access": "read",
                                    },
                                    {
                                        "path": {"type": "path", "path": "uv-cache"},
                                        "access": "write",
                                    },
                                    {
                                        "path": {"type": "path", "path": "approved"},
                                        "access": "write",
                                    },
                                ]
                            },
                            "proxy": {
                                "domains": {
                                    "pypi.org": "allow",
                                    "files.pythonhosted.org": "allow",
                                    "www.python.org": "allow",
                                }
                            },
                            "environment": {"RESOLVER_TEST_APPROVED": str(approved)},
                        }
                    }
                )
            )
        if remote:
            ssh_env = stack.enter_context(localhost(root / "ssh", remote_path=tools))
            ssh_env.update({key: value for key, value in env.items() if key != "PATH"})
            env = ssh_env
            configure(
                root,
                root,
                [str(binary)],
                resolver={
                    "environment": {
                        name: env[name]
                        for name in (
                            "UV_CACHE_DIR",
                            "RESOLVER_TEST_UV",
                            "RESOLVER_TEST_PROTECTED",
                        )
                    }
                },
            )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            if tailored:
                config.write_text(
                    "resolver: {proxy: {domains: {example.com: allow}}}\n"
                )
            client.send(requirements={"action": "set", "python": ["six"]})
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            client.send(python="import six; print(six.__name__)")
            assert last_tool_text(client).endswith("six\n"), last_tool_text(client)
            if tailored:
                client.send(control="restart")
                client.send(python="import six; print(six.__name__)")
                assert last_tool_text(client).endswith("six\n"), last_tool_text(client)
            client.finish()
        assert not (protected / "denied").exists()
        result = json.loads((cache / "probe.json").read_text())
        if tailored:
            assert (approved / "created").read_text() == "custom grant"
            assert not (excluded / "denied").exists()
            result.update(
                custom_write=True,
                replaced_cache_grants=True,
                custom_download_host=True,
                captured_policy=True,
            )
        if remote:
            result["execution_host"] = "ssh"
        return [result]
