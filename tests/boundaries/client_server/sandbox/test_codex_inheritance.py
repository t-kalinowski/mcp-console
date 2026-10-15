#!/usr/bin/env -S uv run --script

import json
import copy
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
from support.client import McpClient
from support.normalization import code
from support.python import isolated_python
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.sandbox_configuration import host_tcp_ports
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)

STATE = "codex/sandbox-state"
LEGACY_STATE = "codex/sandbox-state-meta"


def sandbox_state(root: Path, network: str = "restricted") -> dict:
    return {
        "permissionProfile": {
            "type": "managed",
            "file_system": {
                "type": "restricted",
                "entries": [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    },
                    {
                        "path": {"type": "special", "value": {"kind": "project_roots"}},
                        "access": "write",
                    },
                    {
                        "path": {"type": "path", "path": str(root / "additional")},
                        "access": "write",
                    },
                    {
                        "path": {
                            "type": "path",
                            "path": (root / "additional/private").as_uri(),
                        },
                        "access": "deny",
                    },
                    {
                        "path": {
                            "type": "special",
                            "value": {"kind": "future_location"},
                        },
                        "access": "write",
                    },
                    {
                        "path": {
                            "type": "special",
                            "value": {"kind": "project_roots", "subpath": False},
                        },
                        "access": "deny",
                    },
                ],
            },
            "network": network,
        },
        "sandboxCwd": (root / "project café 雪").as_uri(),
        "codexLinuxSandboxExe": "/must-not-be-executed",
        "useLegacyLandlock": True,
        "useMxc": False,
        "unknownFutureField": True,
    }


def send_with_state(
    client: McpClient, state: object, *, key: str = STATE, **arguments: object
) -> dict:
    return client.request(
        "tools/call",
        name="send",
        arguments={"timeout_ms": 60000, **arguments},
        _meta={key: state},
    )


def finish(client: McpClient, root: Path) -> Transcript:
    transcript = client.finish()
    # Assert native/URI paths above; normalize only the random fixture prefix.
    return json.loads(
        json.dumps(transcript, ensure_ascii=False)
        .replace(root.as_uri(), "file:///<workspace>")
        .replace(str(root), "<workspace>")
    )


@requires(SANDBOX)
def test_inherits_filesystem_and_network_before_first_worker(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        for name in ("project café 雪", "additional/private", "local", "neighbor"):
            (root / name).mkdir(parents=True)
        (root / "additional/private/secret").write_text("secret")
        python, _ = isolated_python(root)
        environment = {
            **os.environ,
            "MCP_CONSOLE_TEST_ROOT": str(root),
            "MCP_CONSOLE_TEST_PORTS": json.dumps(ports),
        }
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.inherit_from_codex=true",
                "-c",
                "sandbox.filesystem.read_write=[local]",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            capabilities = client.transcript[0]["result"]["capabilities"]
            assert capabilities["experimental"][STATE] == {}, capabilities
            state = sandbox_state(root, "enabled")
            entry = send_with_state(
                client,
                state,
                # fmt: python
                python=code("""
                    import errno
                    import json
                    import os
                    import socket
                    from pathlib import Path

                    root = Path(os.environ["MCP_CONSOLE_TEST_ROOT"])
                    assert Path.cwd() == root
                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    for name in ("project café 雪", "additional", "local"):
                        _ = (root / name / "created").write_text("allowed")
                    for path, operation in (
                        (root / "neighbor/created", lambda path: path.write_text("denied")),
                        (root / "additional/private/secret", lambda path: path.read_text()),
                    ):
                        try:
                            operation(path)
                        except OSError as error:
                            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                        else:
                            raise AssertionError(path)
                    with socket.create_connection(
                        ("127.0.0.1", json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"])[0]), timeout=2
                    ):
                        pass
                    retained = 41
                    print("inherited grants and network; local grants retained")
                    """),
            )
            assert "error" not in entry, entry
            assert (
                last_tool_text(client)
                == "inherited grants and network; local grants retained\n"
            ), last_tool_text(client)
            # The legacy spelling carries the identical authority.
            reordered = copy.deepcopy(state)
            entries = reordered["permissionProfile"]["file_system"]["entries"]
            entries.reverse()
            entries.append(entries[-1])
            client.request(
                "tools/call",
                name="send",
                arguments={"python": "retained + 1"},
                _meta={LEGACY_STATE: reordered},
            )
            assert last_tool_text(client) == "42\n"
            changed = sandbox_state(root)
            rejected = send_with_state(client, changed, python="retained = -1")
            assert rejected["result"]["isError"] is True, rejected
            assert "reconnect" in last_result_text(client), rejected
            client.send(python="retained")
            assert last_tool_text(client) == "41\n"
            send_with_state(
                client, state, control="restart", python="'retained' in globals()"
            )
            assert (
                last_tool_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\nFalse\n[done]"
            ), last_tool_text(client)
            assert (root / "additional/created").read_text() == "allowed"
            assert not (root / "neighbor/created").exists()
            transcript = finish(client, root)
            projection = next(root.glob(".agents/console/sessions/*/transcript.qmd"))
            assert "retained = -1" in projection.read_text(), (
                "rejected call was not recorded"
            )
            return transcript


@requires(SANDBOX)
def test_default_ignores_client_permissions(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        (root / "local").mkdir()
        (root / "additional").mkdir()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "--writable-root",
                "local",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            {**os.environ, "MCP_CONSOLE_TEST_PORTS": json.dumps(ports)},
            root,
        ) as client:
            client.initialize_and_list_tools()
            assert "experimental" not in client.transcript[0]["result"]["capabilities"]
            send_with_state(
                client,
                {"permissionProfile": {"type": "disabled"}},
                # fmt: python
                python=code("""
                    import errno
                    import json
                    import os
                    import socket
                    from pathlib import Path

                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    _ = Path("local/created").write_text("allowed")
                    try:
                        _ = Path("additional/created").write_text("denied")
                    except OSError as error:
                        assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                    else:
                        raise AssertionError("client disabled the sandbox without opt-in")
                    for port in json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"]):
                        try:
                            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                        except OSError:
                            pass
                        else:
                            connection.close()
                            raise AssertionError("client enabled direct networking without opt-in")
                    print("local sandbox unchanged")
                    """),
            )
            assert last_tool_text(client) == "local sandbox unchanged\n", (
                last_tool_text(client)
            )
            assert not (root / "additional/created").exists()
            return finish(client, root)


def test_full_access_uses_host_permissions_and_caches(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        (root / "host-cache").mkdir()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.inherit_from_codex=true",
                "-c",
                "sandbox.filesystem.deny=[outside]",
                "-c",
                f"python.existing={python}",
            ),
            {
                **without_r_environment(root),
                "XDG_CACHE_HOME": str(root / "host-cache"),
                "MCP_CONSOLE_TEST_PORT": str(ports[0]),
            },
            root,
        ) as client:
            client.initialize_and_list_tools()
            send_with_state(
                client,
                {"permissionProfile": {"type": "disabled"}},
                key=LEGACY_STATE,
                # fmt: python
                python=code("""
                    import os
                    import socket
                    from pathlib import Path

                    assert "MCP_CONSOLE_SANDBOX" not in os.environ
                    assert Path(os.environ["XDG_CACHE_HOME"]) == Path.cwd() / "host-cache"
                    _ = Path("outside").write_text("full access")
                    with socket.create_connection(
                        ("127.0.0.1", int(os.environ["MCP_CONSOLE_TEST_PORT"])), timeout=2
                    ):
                        pass
                    print("full access with host caches")
                    """),
            )
            assert last_tool_text(client) == "full access with host caches\n", (
                last_tool_text(client)
            )
            assert (root / "outside").read_text() == "full access"
            return finish(client, root)


@requires(SANDBOX)
def test_missing_and_unsupported_metadata_retain_local_policy(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.inherit_from_codex=true",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "local sandbox\n",
                python="import os; assert os.environ['MCP_CONSOLE_SANDBOX'] == '1'; print('local sandbox')",
            )
            for state in (
                None,
                {},
                {"permissionProfile": {"type": "future"}},
                {"permissionProfile": {"type": "managed", "file_system": []}},
            ):
                send_with_state(
                    client,
                    state,
                    python="assert os.environ['MCP_CONSOLE_SANDBOX'] == '1'; print('local sandbox')",
                )
                assert last_tool_text(client) == "local sandbox\n", last_tool_text(
                    client
                )
            return finish(client, root)


def test_discovery_and_eof_do_not_prepare_runtimes_when_inheriting(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        marker = root / "inspected"
        (site / "sitecustomize.py").write_text(
            f"from pathlib import Path\n_ = Path({str(marker)!r}).write_text('inspected')\n"
        )
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.inherit_from_codex=true",
                "-c",
                "sandbox.filesystem.read_write=[.]",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.request("ping")
            transcript = finish(client, root)
            assert not marker.exists(), "runtime inspection preceded first send"
            return transcript


@requires(SANDBOX)
def test_unrestricted_filesystem_retains_network_enforcement(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary, host_tcp_ports() as ports:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.inherit_from_codex=true",
                "-c",
                "sandbox.network=enabled",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            {**os.environ, "MCP_CONSOLE_TEST_PORTS": json.dumps(ports)},
            root,
        ) as client:
            client.initialize_and_list_tools()
            state = {
                "permissionProfile": {
                    "type": "managed",
                    "file_system": {"type": "unrestricted"},
                    "network": "restricted",
                }
            }
            send_with_state(
                client,
                state,
                # fmt: python
                python=code("""
                    import json
                    import os
                    import socket
                    from pathlib import Path

                    assert os.environ["MCP_CONSOLE_SANDBOX"] == "1"
                    _ = Path("created").write_text("unrestricted filesystem")
                    for port in json.loads(os.environ["MCP_CONSOLE_TEST_PORTS"]):
                        try:
                            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
                        except OSError:
                            pass
                        else:
                            connection.close()
                            raise AssertionError("inherited network restriction was lost")
                    print("unrestricted filesystem with restricted networking")
                    """),
            )
            assert (
                last_tool_text(client)
                == "unrestricted filesystem with restricted networking\n"
            ), last_tool_text(client)
            # External enforcement maps to the same supported permissions.
            send_with_state(
                client,
                {"permissionProfile": {"type": "external", "network": "restricted"}},
                python="Path('created').read_text()",
            )
            assert last_tool_text(client) == "'unrestricted filesystem'\n", (
                last_tool_text(client)
            )
            assert (root / "created").read_text() == "unrestricted filesystem"
            return finish(client, root)


@requires(SANDBOX)
def test_inheritance_preserves_explicit_console_proxy(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        with McpClient(
            binary,
            (
                "serve",
                "-c",
                "sandbox.inherit_from_codex=true",
                "-c",
                "sandbox.network={proxy: {mode: limited, allow_upstream_proxy: true}}",
                "-c",
                "cache=host",
                "-c",
                f"python.existing={python}",
            ),
            os.environ.copy(),
            root,
        ) as client:
            client.initialize_and_list_tools()
            send_with_state(
                client,
                {
                    "permissionProfile": {
                        "type": "managed",
                        "file_system": {"type": "restricted", "entries": []},
                        "network": "enabled",
                    }
                },
                # fmt: python
                python=code("""
                    import http.client
                    import os
                    from urllib.parse import urlsplit

                    assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
                    proxy = urlsplit(os.environ["HTTP_PROXY"])
                    connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=5)
                    connection.request("GET", "http://blocked.invalid/")
                    response = connection.getresponse()
                    assert response.status == 403, response.status
                    _ = response.read()
                    connection.close()
                    assert os.environ["ALL_PROXY"].startswith("http:")
                    print("configured limited proxy and destination restrictions retained")
                    """),
            )
            assert (
                last_tool_text(client)
                == "configured limited proxy and destination restrictions retained\n"
            ), last_tool_text(client)
            return finish(client, root)


if __name__ == "__main__":
    run_this_suite(__file__)
