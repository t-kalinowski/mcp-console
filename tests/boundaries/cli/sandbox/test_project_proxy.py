#!/usr/bin/env -S uv run --script

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.suites import run_this_suite


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "7")
        self.end_headers()
        self.wfile.write(b"allowed")

    do_POST = do_GET

    def log_message(self, *_):
        pass


def _enforces_native_proxy_settings(binary: Path, network: str) -> Transcript:
    script = code(r"""
        import errno
        import http.client
        import os
        import socket
        import sys
        from urllib.parse import urlsplit

        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
        proxy = urlsplit(os.environ["HTTP_PROXY"])
        connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=5)
        connection.request(sys.argv[2], sys.argv[1])
        response = connection.getresponse()
        assert response.status == int(sys.argv[3]), (response.status, response.read())
        if response.status == 200:
            assert response.read() == b"allowed"
        connection.close()
        assert os.environ["ALL_PROXY"].startswith(sys.argv[4])
        if os.environ["CODEX_NETWORK_ALLOW_LOCAL_BINDING"] == "0":
            origin = urlsplit(sys.argv[1])
            try:
                socket.create_connection((origin.hostname, origin.port), timeout=2)
            except OSError as error:
                # Linux isolates the host listener in a separate network namespace.
                assert error.errno in (errno.EPERM, errno.EACCES, errno.ECONNREFUSED)
                print("direct network denied")
            else:
                raise AssertionError("direct network unexpectedly allowed")
        print(f"native proxy returned {response.status}")
        """)
    defaults = {
        "enabled": True,
        "enableSocks5": True,
        "enableSocks5Udp": False,
        "allowUpstreamProxy": False,
        "dangerouslyAllowAllUnixSockets": False,
        "mode": "full",
        "domains": None,
        "unixSockets": None,
        "allowLocalBinding": False,
    }
    cases = (
        ("omitted domains", {}, "GET", 403),
        ("null domains", {"domains": None}, "GET", 403),
        ("empty domains", {"domains": {}}, "GET", 403),
        ("omitted domains with local binding", {"allowLocalBinding": True}, "GET", 403),
        (
            "empty domains with local binding",
            {"allowLocalBinding": True, "domains": {}},
            "GET",
            403,
        ),
        (
            "native wildcard",
            {"allowLocalBinding": True, "domains": {"*": "allow"}},
            "GET",
            200,
        ),
        ("allow literal", {"domains": {"127.0.0.1": "allow"}}, "POST", 200),
        (
            "deny overrides wildcard",
            {"allowLocalBinding": True, "domains": {"*": "allow", "127.0.0.1": "deny"}},
            "GET",
            403,
        ),
        (
            "none grants nothing",
            {"allowLocalBinding": True, "domains": {"127.0.0.1": "none"}},
            "GET",
            403,
        ),
        (
            "limited read",
            {"mode": "limited", "domains": {"127.0.0.1": "allow"}},
            "GET",
            200,
        ),
        (
            "limited write",
            {"mode": "limited", "domains": {"127.0.0.1": "allow"}},
            "POST",
            403,
        ),
        (
            "exposed switches",
            {
                "enableSocks5": False,
                "allowUpstreamProxy": True,
                "allowLocalBinding": True,
                "domains": {"127.0.0.1": "allow"},
            },
            "GET",
            200,
        ),
    )
    transcript = []
    with (
        TemporaryDirectory() as directory,
        ThreadingHTTPServer(("127.0.0.1", 0), Origin) as origin,
    ):
        thread = threading.Thread(target=origin.serve_forever)
        thread.start()
        try:
            host = Path(directory).resolve()
            config = host / ".mcp-console/config.yaml"
            config.parent.mkdir()
            capture = host / "payloads.jsonl"
            environment = {
                key: value
                for key, value in os.environ.items()
                if "proxy" not in key.lower()
            }
            environment.update(
                {
                    LOADER_VARIABLE: str(
                        build_interposer(host, "runner_configuration")
                    ),
                    "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
                    "MCP_CONSOLE_SANDBOX_SETTINGS": "unselected ambient value",
                }
            )
            for name, options, method, status in cases:
                config.write_text(
                    json.dumps(
                        {
                            "sandbox": {
                                "network": network,
                                "proxy": {"enabled": True, **options},
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--",
                        sys.executable,
                        "-c",
                        script,
                        f"http://127.0.0.1:{origin.server_port}/",
                        method,
                        str(status),
                        "http:" if options.get("enableSocks5") is False else "socks5h:",
                    ],
                    cwd=host,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0 and result.stderr == "", (name, result)
                payload = json.loads(capture.read_text().splitlines()[-1])
                assert payload["network"] == network, payload
                assert payload["proxy"] == {**defaults, **options}, payload
                transcript.append(
                    {
                        "case": name,
                        "proxy": payload["proxy"],
                        "network": payload["network"],
                        "stdout": result.stdout,
                    }
                )
        finally:
            origin.shutdown()
            thread.join()
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_normalizes_and_enforces_native_proxy_settings(binary: Path) -> Transcript:
    return _enforces_native_proxy_settings(binary, "restricted")


@requires(SANDBOX, NATIVE_FIXTURES)
def test_proxy_enforcement_with_network_enabled(binary: Path) -> Transcript:
    return _enforces_native_proxy_settings(binary, "enabled")


@requires(SANDBOX)
def test_project_network_enabled_allows_direct_connection(binary: Path) -> Transcript:
    script = code("""
        import socket
        import sys

        socket.create_connection(("127.0.0.1", int(sys.argv[1])))
        print("network allowed")
        """)
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory)
        config = host / ".agents/mcp-console.yaml"
        config.parent.mkdir()
        config.write_text("sandbox:\n  network: enabled\n", encoding="utf-8")
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                script,
                str(listener.getsockname()[1]),
            ],
            cwd=host,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 and result.stderr == "", result
        return [{"stdout": result.stdout}]


if __name__ == "__main__":
    run_this_suite(__file__)
