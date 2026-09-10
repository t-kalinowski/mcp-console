#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
STAGE_SCRIPT = ROOT / "scripts" / "stage-sandbox-runner"


def write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


class ReleaseScriptTests(unittest.TestCase):
    def run_script(
        self,
        *arguments: str,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def validation_environment(self, directory: Path) -> dict[str, str]:
        (directory / "Cargo.toml").write_text(
            '[package]\nversion = "0.0.2"\n', encoding="utf-8"
        )
        commands = directory / "commands"
        commands.mkdir()
        write_executable(
            commands / "git",
            """
            #!/usr/bin/env python3
            import os
            import sys

            arguments = sys.argv[1:]
            if arguments == ["rev-parse", "refs/tags/v0.0.2^{commit}"]:
                print(os.environ["FAKE_RELEASE_COMMIT"])
            elif arguments == ["fetch", "--no-tags", "origin", "main"]:
                pass
            elif arguments == ["rev-parse", "FETCH_HEAD"]:
                print(os.environ["FAKE_MAIN_COMMIT"])
            elif arguments[:2] == ["merge-base", "--is-ancestor"]:
                raise SystemExit(int(os.environ.get("FAKE_ANCESTRY_STATUS", "0")))
            else:
                print(f"unexpected git arguments: {arguments}", file=sys.stderr)
                raise SystemExit(2)
            """,
        )
        write_executable(
            commands / "gh",
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            Path(os.environ["FAKE_GH_ARGUMENTS"]).write_text(
                json.dumps(sys.argv[1:]), encoding="utf-8"
            )
            print(os.environ["FAKE_GH_RESPONSE"])
            """,
        )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_TYPE": "tag",
                "GITHUB_REF_NAME": "v0.0.2",
                "GITHUB_REPOSITORY": "t-kalinowski/mcp-console",
                "FAKE_RELEASE_COMMIT": "release-commit",
                "FAKE_MAIN_COMMIT": "main-commit",
                "FAKE_GH_ARGUMENTS": str(directory / "gh-arguments.json"),
            }
        )
        return environment

    def test_validate_publish_requires_successful_main_push_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["FAKE_GH_RESPONSE"] = json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": "release-commit",
                            "event": "push",
                            "head_branch": "main",
                            "conclusion": "success",
                        }
                    ]
                }
            )

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            gh_arguments = json.loads((directory / "gh-arguments.json").read_text())
            self.assertIn(
                "/repos/t-kalinowski/mcp-console/actions/workflows/ci.yaml/runs",
                gh_arguments,
            )
            self.assertIn("head_sha=release-commit", gh_arguments)
            self.assertIn("event=push", gh_arguments)
            self.assertIn("branch=main", gh_arguments)

    def test_validate_publish_rejects_successful_pull_request_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["FAKE_GH_RESPONSE"] = json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": "release-commit",
                            "event": "pull_request",
                            "head_branch": "feature",
                            "conclusion": "success",
                        }
                    ]
                }
            )

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lacks successful CI from a push to main", result.stderr)

    def test_validate_publish_rejects_manual_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
            environment["FAKE_GH_RESPONSE"] = '{"workflow_runs": []}'

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires a push event", result.stderr)

    def smoke_environment(self, directory: Path) -> tuple[dict[str, str], Path, Path]:
        (directory / "Cargo.toml").write_text(
            '[package]\nversion = "0.0.2"\n', encoding="utf-8"
        )
        commands = directory / "commands"
        commands.mkdir()
        tool_directory = directory / "tool" / "mcp-console" / "bin"
        tool_directory.mkdir(parents=True)
        tool_bin = directory / "bin"
        tool_bin.mkdir()

        executable_source = """
            #!/usr/bin/env python3
            import hashlib
            import json
            import os
            import signal
            import sys
            from pathlib import Path

            if record := os.environ.get("FAKE_MCP_ARGUMENTS"):
                with open(record, "a") as stream:
                    stream.write(json.dumps(sys.argv[1:]) + "\\n")

            if sys.argv[1:] == ["--version"]:
                print("mcp-console 0.0.2")
            elif sys.argv[1:] == ["--help"]:
                print("mcp-console help")
            elif sys.argv[1:3] == ["sandbox", "--"]:
                if os.environ.get("FAKE_SANDBOX_FAILURE"):
                    print("private sandbox runner failed", file=sys.stderr)
                    raise SystemExit(1)
                Path(os.environ["FAKE_SANDBOX_PATH_RECORD"]).write_text(
                    os.environ.get("PATH", ""), encoding="utf-8"
                )
            elif sys.argv[1:] in (["serve"], ["serve", "--no-sandbox"]):
                initialize = json.loads(sys.stdin.readline())
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": initialize["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "mcp-console",
                            "version": "0.0.2",
                        },
                    },
                }), flush=True)
                json.loads(sys.stdin.readline())
                startup = json.loads(sys.stdin.readline())
                assert startup["params"] == {
                    "name": "send",
                    "arguments": {"control": "restart"},
                }
                if os.environ.get("FAKE_MCP_STARTUP_HANG"):
                    signal.pause()
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": startup["id"],
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": "[starting new worker]\\n[idle]",
                        }],
                        "isError": False,
                    },
                }), flush=True)
                evaluation = json.loads(sys.stdin.readline())
                assert evaluation["params"] == {
                    "name": "send",
                    "arguments": {"r": "6 * 7"},
                }
                if os.environ.get("FAKE_MCP_EVALUATION_HANG"):
                    signal.pause()
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": evaluation["id"],
                    "result": {
                        "content": [{"type": "text", "text": "[1] 42\\n"}],
                        "isError": False,
                    },
                }), flush=True)
            else:
                raise SystemExit(2)
        """
        executable_source = executable_source.replace(
            "#!/usr/bin/env python3", f"#!{sys.executable}"
        )
        cargo_directory = directory / "cargo-target"
        (cargo_directory / "release").mkdir(parents=True)
        cargo_bin = cargo_directory / "release" / "mcp-console"
        write_executable(cargo_bin, executable_source)
        installed = tool_directory / "mcp-console"
        write_executable(installed, executable_source)
        (tool_bin / "mcp-console").symlink_to(installed)

        write_executable(
            commands / "uv",
            """
            #!/bin/sh
            if test "$1 $2 $3 $5" = "tool run --from mcp-console"; then
              case "$6" in
                --version) echo 'mcp-console 0.0.2' ;;
                --help)
                  printf 'mcp-console help\n'
                  if test "${FAKE_UV_HELP_EXTRA_NEWLINE:-}" = 1; then
                    printf '\n'
                  fi
                  ;;
                *) exit 2 ;;
              esac
            elif test "$1 $2" = "tool install"; then
              exit 0
            else
              exit 2
            fi
            """,
        )
        write_executable(
            commands / "R",
            """
            #!/bin/sh
            test "$1" = RHOME
            echo /fake/R
            """,
        )

        wheel = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
        self.write_wheel(wheel)
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "UV_TOOL_DIR": str(directory / "tool"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
                "FAKE_SANDBOX_PATH_RECORD": str(directory / "sandbox-path.txt"),
            }
        )
        return environment, wheel, cargo_bin

    def write_wheel(
        self, wheel: Path, *, omit: str | None = None, executable: bool = True
    ) -> None:
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("mcp_console-0.0.2.data/scripts/mcp-console", "fixture\n")
            names = ("mcp-console-sandbox", "LICENSE", "NOTICE")
            if "linux" in wheel.name:
                names += ("bwrap", "bubblewrap-COPYING")
            for name in names:
                if name == omit:
                    continue
                directory = (
                    "libexec"
                    if name in ("mcp-console-sandbox", "bwrap")
                    else "share/licenses/mcp-console"
                )
                info = zipfile.ZipInfo(
                    f"mcp_console-0.0.2.data/data/{directory}/{name}"
                )
                mode = 0o755 if directory == "libexec" and executable else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, "fixture\n")

    def test_smoke_wheel_requires_a_private_companion_bundle(self) -> None:
        for defect in (
            "missing runner",
            "missing notice",
            "not executable",
            "public wheel",
            "public uv",
        ):
            with self.subTest(
                defect=defect
            ), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, wheel, cargo_bin = self.smoke_environment(directory)
                if defect in {"missing runner", "missing notice", "not executable"}:
                    self.write_wheel(
                        wheel,
                        omit={
                            "missing runner": "mcp-console-sandbox",
                            "missing notice": "NOTICE",
                        }.get(defect),
                        executable=defect != "not executable",
                    )
                elif defect == "public wheel":
                    with zipfile.ZipFile(wheel, "a") as archive:
                        archive.writestr(
                            "mcp_console-0.0.2.data/scripts/mcp-console-sandbox",
                            "fixture\n",
                        )
                else:
                    (
                        Path(environment["UV_TOOL_BIN_DIR"]) / "mcp-console-sandbox"
                    ).touch()
                result = self.run_script(
                    "smoke-wheel",
                    str(wheel),
                    str(cargo_bin),
                    cwd=directory,
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("private sandbox runner", result.stderr)

    def test_smoke_wheel_requires_runnable_cargo_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            environment["FAKE_SANDBOX_FAILURE"] = "1"

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("private sandbox runner failed", result.stderr)

    def test_smoke_wheel_evaluates_r_and_bounds_response_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            sandbox_path = Path(environment["FAKE_SANDBOX_PATH_RECORD"]).read_text()
            self.assertEqual(len(sandbox_path.split(os.pathsep)), 1)
            self.assertNotEqual(sandbox_path, environment["PATH"])
            environment["FAKE_UV_HELP_EXTRA_NEWLINE"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("installed and Cargo help output differ", result.stderr)
            del environment["FAKE_UV_HELP_EXTRA_NEWLINE"]

            environment["FAKE_MCP_EVALUATION_HANG"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "0.01",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out after 0.01 seconds", result.stderr)

    def test_smoke_wheel_bounds_runtime_startup_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            environment["FAKE_MCP_STARTUP_HANG"] = "1"

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "0.01",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out after 1 seconds", result.stderr)

    def test_smoke_linux_wheel_requires_sandbox_and_bundled_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            linux_wheel = wheel.with_name(
                "mcp_console-0.0.2-py3-none-manylinux_2_34_x86_64.whl"
            )
            self.write_wheel(linux_wheel)
            wheel.unlink()
            record = directory / "arguments.jsonl"
            environment["FAKE_MCP_ARGUMENTS"] = str(record)
            command = (
                "smoke-wheel",
                str(linux_wheel),
                str(cargo_bin),
                "--target",
                "x86_64-unknown-linux-gnu",
            )
            result = self.run_script(
                *command,
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            invocations = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertIn(["serve"], invocations)
            self.assertTrue(any(call[:1] == ["sandbox"] for call in invocations))

            for missing in ("mcp-console-sandbox", "bwrap", "bubblewrap-COPYING"):
                with self.subTest(missing=missing):
                    self.write_wheel(linux_wheel, omit=missing)
                    result = self.run_script(*command, cwd=directory, env=environment)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("private sandbox runner", result.stderr)

    def test_verify_wheel_set_requires_macos_and_linux_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            arm64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
            x86_64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_x86_64.whl"
            arm64.touch()
            x86_64.touch()
            for architecture in ("aarch64", "x86_64"):
                (
                    directory
                    / f"mcp_console-0.0.2-py3-none-manylinux_2_39_{architecture}.whl"
                ).touch()

            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)

            x86_64.unlink()
            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected exactly four wheels", result.stderr)

    def test_linux_wheel_audit_checks_private_binaries_and_platform_claims(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "Cargo.toml").write_text('[package]\nversion = "0.0.2"\n')
            commands = directory / "commands"
            commands.mkdir()
            write_executable(
                commands / "readelf",
                """
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path
                name = Path(sys.argv[-1]).name
                print("Machine: " + os.environ.get("FAKE_MACHINE", "Advanced Micro Devices X86-64"))
                if name != os.environ.get("FAKE_STATIC_HELPER"):
                    print("[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]")
                    print("(NEEDED) Shared library: [libc.so.6]")
                    if name == "bwrap":
                        print("(NEEDED) Shared library: [libcap.so.2]")
                    if name == os.environ.get("FAKE_EXTRA_LIBRARY"):
                        print("(NEEDED) Shared library: [libssl.so.3]")
                    version = "2.40" if name == os.environ.get("FAKE_NEW_GLIBC") else "2.39"
                    print("Name: GLIBC_" + version)
            """,
            )
            environment = os.environ | {
                "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}"
            }
            for platform, changes, error in (
                ("manylinux_2_39_x86_64", {}, None),
                ("manylinux_2_17_x86_64", {}, "GLIBC_2.39"),
                (
                    "manylinux_2_39_x86_64",
                    {"FAKE_EXTRA_LIBRARY": "mcp-console-sandbox"},
                    "libssl.so.3",
                ),
                ("manylinux_2_39_x86_64", {"FAKE_NEW_GLIBC": "bwrap"}, "GLIBC_2.40"),
                (
                    "manylinux_2_39_x86_64",
                    {"FAKE_NEW_GLIBC": "mcp-console-sandbox"},
                    "GLIBC_2.40",
                ),
                ("manylinux_2_39_x86_64", {"FAKE_STATIC_HELPER": "bwrap"}, "bwrap"),
                (
                    "manylinux_2_39_x86_64",
                    {"FAKE_STATIC_HELPER": "mcp-console-sandbox"},
                    "mcp-console-sandbox",
                ),
                ("manylinux_2_39_x86_64", {"FAKE_MACHINE": "AArch64"}, "architecture"),
            ):
                wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                self.write_wheel(wheel)
                with zipfile.ZipFile(wheel, "a") as archive:
                    archive.writestr(
                        "mcp_console-0.0.2.dist-info/WHEEL",
                        f"Wheel-Version: 1.0\nTag: py3-none-{platform}\n",
                    )
                result = self.run_script(
                    "audit-linux-wheel",
                    str(wheel),
                    cwd=directory,
                    env=environment | changes,
                )
                if error is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for name in ("mcp-console", "mcp-console-sandbox", "bwrap"):
                        self.assertIn(name, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)

    def test_stage_runner_builds_the_pin_and_records_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "project"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(STAGE_SCRIPT, scripts / STAGE_SCRIPT.name)
            placeholder = root / "wheel-data/data/.gitignore"
            placeholder.parent.mkdir(parents=True)
            placeholder.write_text("/*\n!/.gitignore\n")
            # The toolchain below is fake; exercise its macOS staging contract
            # on every test host without building or executing a native runner.
            launcher = scripts / "macos-stage.py"
            write_executable(
                launcher,
                """
                import runpy
                import sys

                sys.platform = "darwin"
                sys.argv.pop(0)
                runpy.run_path(sys.argv[0], run_name="__main__")
                """,
            )
            pin = {
                "repository": "t-kalinowski/codex",
                "release": "rust-v0.150.1",
                "commit": "a" * 40,
                "protocol_version": 2,
                "rust_toolchain": "1.95.0",
            }
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            checkout = directory / "source"
            crate = checkout / "codex-rs" / "mcp-console-sandbox"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").touch()
            (checkout / "LICENSE").write_text("license\n")
            (checkout / "NOTICE").write_text("notice\n")
            vendor = checkout / "codex-rs/vendor/bubblewrap"
            vendor.mkdir(parents=True)
            (vendor / "COPYING").write_text("bwrap license\n")
            commands = directory / "commands"
            commands.mkdir()
            write_executable(
                commands / "git",
                """
                #!/usr/bin/env python3
                import os
                import sys

                if sys.argv[1:] == ["rev-parse", "HEAD"]:
                    print(os.environ["FAKE_SOURCE_REVISION"])
                elif sys.argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
                    print(os.environ.get("FAKE_SOURCE_DIRTY", ""), end="")
                else:
                    raise SystemExit(2)
                """,
            )
            write_executable(
                commands / "rustc",
                """
                #!/usr/bin/env python3
                import sys

                assert sys.argv[1:] == ["+1.95.0", "--print", "host-tuple"]
                print("aarch64-apple-darwin")
                """,
            )
            write_executable(
                commands / "cargo",
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                with Path(os.environ["FAKE_CARGO_ARGUMENTS"]).open("a") as record:
                    record.write(json.dumps({"arguments": sys.argv[1:], "helper_sha256": os.environ.get("CODEX_BWRAP_SHA256")}) + "\\n")
                target = sys.argv[sys.argv.index("--target") + 1] if "--target" in sys.argv else os.environ["CARGO_BUILD_TARGET"]
                output = Path(os.environ["CARGO_TARGET_DIR"]) / target / "release"
                output.mkdir(parents=True, exist_ok=True)
                (output / "mcp-console-sandbox").write_bytes(b"runner bytes with debug symbols")
                (output / "bwrap").write_bytes(b"bwrap bytes with debug symbols")
                """,
            )
            write_executable(
                commands / "rustup",
                """
                #!/usr/bin/env python3
                import os
                import sys

                assert sys.argv[1:4] == ["run", "--install", "1.95.0"]
                os.execvp(sys.argv[4], [sys.argv[4], "+1.95.0", *sys.argv[5:]])
                """,
            )
            for name in ("xcrun", "strip"):
                write_executable(
                    commands / name,
                    """
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    executable = Path(sys.argv[-1])
                    original = executable.read_bytes()
                    assert original.endswith(b" with debug symbols")
                    executable.write_bytes(original.removesuffix(b" with debug symbols"))
                    """,
                )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "FAKE_SOURCE_REVISION": pin["commit"],
                    "FAKE_CARGO_ARGUMENTS": str(directory / "cargo.json"),
                    "CARGO_BUILD_TARGET": "x86_64-apple-darwin",
                }
            )
            command = [
                sys.executable,
                str(launcher),
                str(scripts / STAGE_SCRIPT.name),
                str(checkout),
            ]
            for arguments, target in (
                ([], "aarch64-apple-darwin"),
                (["--target", "x86_64-unknown-linux-gnu"], "x86_64-unknown-linux-gnu"),
                (["--target", "x86_64-apple-darwin"], "x86_64-apple-darwin"),
            ):
                with self.subTest(target=target):
                    (directory / "cargo.json").unlink(missing_ok=True)
                    result = subprocess.run(
                        command + arguments,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(placeholder.read_text(), "/*\n!/.gitignore\n")
                    packages = [("codex-mcp-console-sandbox", "mcp-console-sandbox")]
                    if "linux" in target:
                        packages.insert(0, ("codex-bwrap", "bwrap"))
                    self.assertEqual(
                        [
                            json.loads(line)
                            for line in (directory / "cargo.json")
                            .read_text()
                            .splitlines()
                        ],
                        [
                            {
                                "arguments": [
                                    "+1.95.0",
                                    "build",
                                    "--locked",
                                    "--release",
                                    "-p",
                                    package,
                                    "--bin",
                                    binary,
                                    "--target",
                                    target,
                                ],
                                "helper_sha256": hashlib.sha256(
                                    b"bwrap bytes"
                                ).hexdigest()
                                if "linux" in target and binary == "mcp-console-sandbox"
                                else None,
                            }
                            for package, binary in packages
                        ],
                    )
                    self.assertEqual(
                        json.loads(
                            (root / "target/sandbox-runner-build.json").read_text()
                        ),
                        {
                            "source_revision": pin["commit"],
                            "target": target,
                            "artifacts": {
                                "LICENSE": hashlib.sha256(b"license\n").hexdigest(),
                                "NOTICE": hashlib.sha256(b"notice\n").hexdigest(),
                                "mcp-console-sandbox": hashlib.sha256(
                                    b"runner bytes"
                                ).hexdigest(),
                                **(
                                    {
                                        "bubblewrap-COPYING": hashlib.sha256(
                                            b"bwrap license\n"
                                        ).hexdigest(),
                                        "bwrap": hashlib.sha256(
                                            b"bwrap bytes"
                                        ).hexdigest(),
                                    }
                                    if "linux" in target
                                    else {}
                                ),
                            },
                        },
                    )
            data = root / "wheel-data" / "data"
            runner = data / "libexec" / "mcp-console-sandbox"
            self.assertEqual(runner.read_bytes(), b"runner bytes")
            self.assertTrue(os.access(runner, os.X_OK))
            self.assertEqual(
                (data / "share/licenses/mcp-console/LICENSE").read_text(),
                "license\n",
            )
            self.assertEqual(
                (
                    checkout
                    / "codex-rs/target/x86_64-apple-darwin/release/mcp-console-sandbox"
                ).read_bytes(),
                b"runner bytes with debug symbols",
            )
            (directory / "cargo.json").unlink()
            for changes in (
                {"FAKE_SOURCE_REVISION": "b" * 40},
                {"FAKE_SOURCE_DIRTY": " M Cargo.lock"},
            ):
                result = subprocess.run(
                    command, env=environment | changes, capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "cargo.json").exists())

    def test_cargo_rejects_changed_staged_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src/main.rs").write_text("fn main() {}\n")
            for name in ("r_graphics.c", "r_repl.c"):
                (root / "src" / name).touch()
            shutil.copyfile(ROOT / "build.rs", root / "build.rs")
            dependencies = tomllib.loads((ROOT / "Cargo.toml").read_text())[
                "build-dependencies"
            ]
            (root / "Cargo.toml").write_text(
                '[package]\nname = "sandbox-artifact-build"\nversion = "0.0.0"\n'
                'edition = "2024"\n[build-dependencies]\n'
                + "".join(
                    f"{name} = {json.dumps(version)}\n"
                    for name, version in dependencies.items()
                )
            )
            shutil.copyfile(ROOT / "Cargo.lock", root / "Cargo.lock")
            target = subprocess.check_output(
                ["rustc", "--print", "host-tuple"], text=True
            ).strip()
            pin = json.loads((ROOT / "sandbox-runner.json").read_text())
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            artifacts = {
                "mcp-console-sandbox": b"runner bytes",
                "LICENSE": b"license",
                "NOTICE": b"notice",
            }
            if sys.platform == "linux":
                artifacts["bwrap"] = b"bwrap bytes"
                artifacts["bubblewrap-COPYING"] = b"bwrap license"
            staged = root / "wheel-data/data"
            staged_files = {}
            for name, contents in artifacts.items():
                relative = (
                    "libexec"
                    if name in ("mcp-console-sandbox", "bwrap")
                    else "share/licenses/mcp-console"
                )
                staged_files[name] = staged / relative / name
                staged_files[name].parent.mkdir(parents=True, exist_ok=True)
                staged_files[name].write_bytes(contents)
                staged_files[name].chmod(0o755)
            (root / "target").mkdir()
            (root / "target/sandbox-runner-build.json").write_text(
                json.dumps(
                    {
                        "source_revision": pin["commit"],
                        "target": target,
                        "artifacts": {
                            name: hashlib.sha256(contents).hexdigest()
                            for name, contents in artifacts.items()
                        },
                    }
                )
            )
            arguments = [
                "cargo",
                "build",
                "--target",
                target,
                "--target-dir",
                str(root / "target"),
            ]
            result = subprocess.run(arguments, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            prefix = root / "target" / target
            for name, contents in artifacts.items():
                with self.subTest(artifact=name):
                    relative = (
                        "libexec"
                        if name in ("mcp-console-sandbox", "bwrap")
                        else "share/licenses/mcp-console"
                    )
                    installed = prefix / relative / name
                    self.assertEqual(installed.read_bytes(), contents)
                    self.assertTrue(os.access(installed, os.X_OK))
                    staged_files[name].write_bytes(b"replaced artifact")
                    result = subprocess.run(
                        arguments, cwd=root, capture_output=True, text=True
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"artifact {name} changed", result.stderr)
                    self.assertEqual(installed.read_bytes(), contents)
                    staged_files[name].write_bytes(contents)


if __name__ == "__main__":
    unittest.main()
