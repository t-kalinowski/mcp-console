#!/usr/bin/env -S uv run --script

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.sandbox_configuration import NATIVE_PROXY
from support.suites import run_this_suite


CONFIG = ".agents/console/config.yaml"


@requires(SANDBOX, NATIVE_FIXTURES)
def test_omits_absent_proxy_and_native_lifecycle_defaults(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        for name, settings in (
            ("absent file", None),
            ("omitted", {}),
            ("null", {"proxy": None}),
        ):
            if settings is not None:
                config.write_text(json.dumps({"sandbox": settings}), encoding="utf-8")
            result = subprocess.run(
                [binary, "sandbox", "--", "/usr/bin/true"],
                cwd=host,
                env=environment,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0 and result.stdout == result.stderr == "", (
                result
            )
            payloads = [json.loads(line) for line in capture.read_text().splitlines()]
            for payload in payloads:
                assert "proxy" not in payload, payload
                assert "cleanup_timeout_ms" not in payload["lifecycle"], payload
            assert payloads[-1]["lifecycle"] == {
                "private_tmp": {"environment": ["TMPDIR"]}
            }, payloads
            capture.unlink()
            transcript.append(
                {"case": name, "proxy_omitted": True, "native_cleanup_default": True}
            )
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_forwards_unrestricted_and_external_filesystems(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        for kind in ("unrestricted", "external-sandbox"):
            filesystem = {"kind": kind}
            config.write_text(
                json.dumps({"sandbox": {"filesystem": filesystem}}), encoding="utf-8"
            )
            result = subprocess.run(
                [binary, "sandbox", "--", "/usr/bin/touch", kind],
                cwd=host,
                env=environment,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0 and result.stdout == result.stderr == "", (
                result
            )
            assert (host / kind).is_file()
            payloads = [json.loads(line) for line in capture.read_text().splitlines()]
            assert len(payloads) == 2, payloads
            for payload in payloads:
                assert payload["filesystem"] == filesystem, payload
                assert "macos_seatbelt_profile_extension" not in payload, payload
            capture.unlink()
            transcript.append({"filesystem": filesystem, "workload_write": "allowed"})
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_preserves_native_validation_errors(binary: Path) -> Transcript:
    cases = (
        {"filesystem": {"kind": "future-filesystem"}},
        {
            "filesystem": {
                "entries": [{"path": {"type": "future-path"}, "access": "read"}]
            }
        },
        {
            "filesystem": {
                "entries": [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "future-access",
                    }
                ]
            }
        },
        {"proxy": {"enabled": True}},
        {"proxy": {**NATIVE_PROXY, "enabled": False}},
        {"environment": {"INVALID_VALUE": "\u0000"}},
        {"future_policy": {"enabled": True}},
        {"linux_backend": "future-backend"},
    )
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
        }
        for settings in cases:
            config.write_text(json.dumps({"sandbox": settings}), encoding="utf-8")
            for arguments in (
                ("serve",),
                ("sandbox", "--", "/bin/echo", "workload started"),
            ):
                result = subprocess.run(
                    [binary, *arguments],
                    cwd=host,
                    env=environment,
                    input="",
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 1 and result.stdout == "", result
                assert capture.exists(), result.stderr
                payloads = [
                    json.loads(line) for line in capture.read_text().splitlines()
                ]
                assert len(payloads) == 1, payloads
                payload = payloads[0]
                if "proxy" in settings:
                    assert payload["proxy"] == settings["proxy"], payload
                for key, value in settings.items():
                    if key == "filesystem":
                        for field, supplied in value.items():
                            assert payload[key][field] == (
                                [payload[key][field][0], *supplied]
                                if field == "entries"
                                else supplied
                            ), payload
                    else:
                        assert payload[key] == value, payload
                # Compare the actual native diagnostic via the complete-policy
                # CLI, with only launch-dependent JSON columns normalized.
                payload["lifecycle"]["parent_pid"] = os.getpid()
                native = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--config-env",
                        "TEST_POLICY",
                        "--",
                        "/usr/bin/true",
                    ],
                    cwd=host,
                    env={**os.environ, "TEST_POLICY": json.dumps(payload)},
                    capture_output=True,
                    text=True,
                )
                assert native.returncode != 0 and native.stdout == "", native
                normalize = lambda text: re.sub(
                    r"(at line [0-9]+, column )[0-9]+", r"\1<column>", text
                )
                diagnostic = normalize(native.stderr)
                assert diagnostic.startswith("mcp-console-sandbox: "), native
                assert normalize(result.stderr).endswith(diagnostic), result
                assert f"{CONFIG}: sandbox preflight failed" in result.stderr, result
                transcript.append(
                    {
                        "settings": settings,
                        "command": arguments[0],
                        "stderr": normalize(result.stderr),
                    }
                )
                capture.unlink()
    return transcript


@requires(SANDBOX, NATIVE_FIXTURES)
def test_forwards_native_fields_without_proxy_expansion(binary: Path) -> Transcript:
    proxy = {**NATIVE_PROXY, "enableSocks5Udp": True, "unixSockets": {}}
    entries = [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
        {"path": {"type": "path", "path": "./read café"}, "access": "read"},
    ]
    settings = {
        "filesystem": {"entries": entries, "glob_scan_max_depth": 1},
        "proxy": proxy,
        "environment": {"FORWARDED_POLICY": "native"},
        "macos_seatbelt_profile_extension": None,
    }
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        (host / "read café").mkdir()
        config = host / CONFIG
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"sandbox": settings}), encoding="utf-8")
        capture = host / "payloads.jsonl"
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                "import os; print(os.environ['FORWARDED_POLICY'])",
            ],
            cwd=host,
            env={
                **os.environ,
                LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
                "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert result.stdout == "native\n", result
        payloads = [json.loads(line) for line in capture.read_text().splitlines()]
        assert len(payloads) == 2, payloads
        entries[1]["path"]["path"] = str(host / "read café")
        for payload in payloads:
            assert payload["proxy"] == proxy, payload
            assert payload["filesystem"] == {
                "kind": "restricted",
                "entries": [entries[0], *entries],
                "glob_scan_max_depth": 1,
            }, payload
            assert payload["environment"] == settings["environment"], payload
            assert payload["macos_seatbelt_profile_extension"] is None, payload
    return [
        {
            "proxy": proxy,
            "literal_path": "launch-relative",
            "other_native_fields_preserved": True,
            "stdout": result.stdout,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
