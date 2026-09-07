from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
        libexec = tool_directory.parent / "libexec"
        libexec.mkdir()
        runner_source = """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            assert len(sys.argv) == 1
            data = sys.stdin.buffer.read()
            size = int.from_bytes(data[:4], "big")
            request = json.loads(data[4:4 + size])
            Path(os.environ["FAKE_RUNNER_RECORD"]).write_text(json.dumps({
                "request": request,
                "path": os.environ.get("PATH", ""),
            }))
            mode = os.environ.get("FAKE_RUNNER_MODE")
            if mode == "failure":
                print("fixture launch failure", file=sys.stderr)
                raise SystemExit(42)
            sys.stdout.buffer.write(b"wrong bytes" if mode == "corrupt" else data[4 + size:])
            """
        write_executable(
            libexec / "mcp-console-sandbox",
            runner_source.replace("#!/usr/bin/env python3", f"#!{sys.executable}"),
        )
        tool_bin = directory / "bin"
        tool_bin.mkdir()

        executable_source = """
            #!/usr/bin/env python3
            import hashlib
            import json
            import os
            import sys
            import time
            from pathlib import Path

            if sys.argv[1:] == ["--version"]:
                print("mcp-console 0.0.2")
            elif sys.argv[1:] == ["--help"]:
                print("mcp-console help")
            elif sys.argv[1:3] == ["sandbox", "--"]:
                runner = Path(sys.argv[0]).resolve().parent.parent / "libexec" / "mcp-console-sandbox"
                if not runner.is_file() or not os.access(runner, os.X_OK):
                    print("the private sandbox runner is unavailable", file=sys.stderr)
                    raise SystemExit(1)
                if hashlib.sha256(runner.read_bytes()).hexdigest() != os.environ["FAKE_RUNNER_SHA256"]:
                    print("the private sandbox runner does not match this installation", file=sys.stderr)
                    raise SystemExit(1)
                Path(os.environ["FAKE_SANDBOX_PATH_RECORD"]).write_text(
                    os.environ.get("PATH", ""), encoding="utf-8"
                )
            elif sys.argv[1:] == ["serve"]:
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
                evaluation = json.loads(sys.stdin.readline())
                if os.environ.get("FAKE_MCP_EVALUATION_HANG"):
                    time.sleep(60)
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
        cargo_bin = directory / "cargo-mcp-console"
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
                "FAKE_RUNNER_RECORD": str(directory / "runner.json"),
                "FAKE_RUNNER_SHA256": hashlib.sha256(
                    (libexec / "mcp-console-sandbox").read_bytes()
                ).hexdigest(),
            }
        )
        return environment, wheel, cargo_bin

    def write_wheel(
        self, wheel: Path, *, omit_runner: bool = False, executable: bool = True
    ) -> None:
        data = "mcp_console-0.0.2.data/data"
        files = [
            (f"{data}/share/licenses/mcp-console/Codex-LICENSE", 0o100644),
            (f"{data}/share/licenses/mcp-console/Codex-NOTICE", 0o100644),
        ]
        if not omit_runner:
            files.append(
                (
                    f"{data}/libexec/mcp-console-sandbox",
                    0o100755 if executable else 0o100644,
                )
            )
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, mode in files:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, "fixture\n")

    def test_smoke_wheel_requires_private_executable_and_no_public_runner(self) -> None:
        for defect in ("missing", "not executable", "public wheel", "public uv"):
            with self.subTest(
                defect=defect
            ), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, wheel, cargo_bin = self.smoke_environment(directory)
                if defect == "missing":
                    self.write_wheel(wheel, omit_runner=True)
                elif defect == "not executable":
                    self.write_wheel(wheel, executable=False)
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
            runner = json.loads(Path(environment["FAKE_RUNNER_RECORD"]).read_text())
            request = runner["request"]
            self.assertEqual(request["version"], 1)
            self.assertEqual(request["command"], ["/bin/cat"])
            self.assertEqual(request["network"], "restricted")
            self.assertEqual(
                request["filesystem"],
                {
                    "kind": "restricted",
                    "entries": [
                        {
                            "path": {"type": "special", "value": {"kind": "root"}},
                            "access": "read",
                        },
                        {
                            "path": {"type": "path", "path": request["cwd"]},
                            "access": "write",
                        },
                    ],
                },
            )
            self.assertEqual(request["environment"]["PATH"], runner["path"])
            self.assertEqual(len(runner["path"].split(os.pathsep)), 1)

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
            self.assertIn("MCP response timed out", result.stderr)

    def test_smoke_wheel_rejects_failed_or_corrupt_private_launch(self) -> None:
        for mode in ("failure", "corrupt"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, wheel, cargo_bin = self.smoke_environment(directory)
                environment["FAKE_RUNNER_MODE"] = mode
                result = self.run_script(
                    "smoke-wheel",
                    str(wheel),
                    str(cargo_bin),
                    cwd=directory,
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("private sandbox runner", result.stderr)

    def test_verify_wheel_set_requires_both_macos_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            arm64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
            x86_64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_x86_64.whl"
            arm64.touch()
            x86_64.touch()

            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)

            x86_64.unlink()
            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected exactly two wheels", result.stderr)

    def test_stage_runner_builds_the_pin_and_records_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "project"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(STAGE_SCRIPT, scripts / STAGE_SCRIPT.name)
            pin = {
                "repository": "t-kalinowski/codex",
                "release": "rust-v0.150.1",
                "commit": "a" * 40,
                "protocol_version": 1,
                "rust_toolchain": "1.95.0",
            }
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            checkout = directory / "source"
            crate = checkout / "codex-rs" / "mcp-console-sandbox"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").touch()
            (checkout / "LICENSE").write_text("license\n")
            (checkout / "NOTICE").write_text("notice\n")
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
                commands / "cargo",
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                Path(os.environ["FAKE_CARGO_ARGUMENTS"]).write_text(json.dumps(sys.argv[1:]))
                output = Path(os.environ["CARGO_TARGET_DIR"]) / "aarch64-apple-darwin" / "release"
                output.mkdir(parents=True)
                (output / "mcp-console-sandbox").write_bytes(b"runner bytes")
                """,
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "FAKE_SOURCE_REVISION": pin["commit"],
                    "FAKE_CARGO_ARGUMENTS": str(directory / "cargo.json"),
                }
            )
            command = [
                sys.executable,
                str(scripts / STAGE_SCRIPT.name),
                str(checkout),
                "--target",
                "aarch64-apple-darwin",
            ]
            result = subprocess.run(
                command, env=environment, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((directory / "cargo.json").read_text()),
                [
                    "+1.95.0",
                    "build",
                    "--locked",
                    "--release",
                    "-p",
                    "codex-mcp-console-sandbox",
                    "--bin",
                    "mcp-console-sandbox",
                    "--target",
                    "aarch64-apple-darwin",
                ],
            )
            data = root / "target" / "private-wheel-data" / "data"
            runner = data / "libexec" / "mcp-console-sandbox"
            self.assertEqual(runner.read_bytes(), b"runner bytes")
            self.assertTrue(os.access(runner, os.X_OK))
            self.assertEqual(
                (data / "share/licenses/mcp-console/Codex-LICENSE").read_text(),
                "license\n",
            )
            self.assertEqual(
                json.loads((root / "target/sandbox-runner-build.json").read_text()),
                {
                    "source_revision": pin["commit"],
                    "sha256": hashlib.sha256(b"runner bytes").hexdigest(),
                },
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


if __name__ == "__main__":
    unittest.main()
