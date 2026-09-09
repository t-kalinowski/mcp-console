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
            for name in ("mcp-console-sandbox", "LICENSE", "NOTICE"):
                if name == omit:
                    continue
                directory = (
                    "libexec"
                    if name == "mcp-console-sandbox"
                    else "share/licenses/mcp-console"
                )
                info = zipfile.ZipInfo(
                    f"mcp_console-0.0.2.data/data/{directory}/{name}"
                )
                mode = 0o755 if name == "mcp-console-sandbox" and executable else 0o644
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

    def test_smoke_linux_wheel_uses_no_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            linux_wheel = wheel.with_name(
                "mcp_console-0.0.2-py3-none-manylinux_2_34_x86_64.whl"
            )
            self.write_wheel(linux_wheel, omit="mcp-console-sandbox")
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
            self.assertIn(["serve", "--no-sandbox"], invocations)
            self.assertFalse(any(call[:1] == ["sandbox"] for call in invocations))

            self.write_wheel(linux_wheel)
            result = self.run_script(
                *command,
                cwd=directory,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "Linux wheel contains a macOS sandbox executable", result.stderr
            )

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

    @unittest.skipUnless(sys.platform == "darwin", "requires a native runner target")
    def test_stage_runner_ignores_ambient_cargo_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            project = directory / "project"
            (project / "scripts").mkdir(parents=True)
            script = project / "scripts" / STAGE_SCRIPT.name
            shutil.copyfile(STAGE_SCRIPT, script)
            pin = json.loads((ROOT / "sandbox-runner.json").read_text())
            # The tiny fixture uses the installed compiler; a runner cache hit
            # in CI must not require downloading the upstream toolchain.
            pin["rust_toolchain"] = subprocess.check_output(
                ["rustup", "show", "active-toolchain"], text=True
            ).split()[0]
            (project / "sandbox-runner.json").write_text(json.dumps(pin))
            checkout = directory / "source"
            workspace = checkout / "codex-rs"
            crate = workspace / "mcp-console-sandbox"
            (crate / "src").mkdir(parents=True)
            (workspace / "Cargo.toml").write_text(
                '[workspace]\nmembers = ["mcp-console-sandbox"]\nresolver = "2"\n'
            )
            (workspace / "Cargo.lock").write_text(
                'version = 4\n[[package]]\nname = "codex-mcp-console-sandbox"\nversion = "0.1.0"\n'
            )
            (crate / "Cargo.toml").write_text(
                '[package]\nname = "codex-mcp-console-sandbox"\nversion = "0.1.0"\n'
                '[[bin]]\nname = "mcp-console-sandbox"\npath = "src/main.rs"\n'
            )
            (crate / "src/main.rs").write_text(
                textwrap.dedent("""
                #[cfg(ambient_cargo_config)]
                compile_error!("ambient Cargo flags");
                #[cfg(panic = "abort")]
                compile_error!("ambient Cargo profile");
                fn main() { println!("{}", env!("RUNNER_PINNED_CONFIG")); }
                """)
            )
            pinned_config = workspace / ".cargo/config.toml"
            pinned_config.parent.mkdir()
            pinned_config.write_text('[env]\nRUNNER_PINNED_CONFIG = "pinned"\n')
            for name in ("LICENSE", "NOTICE"):
                (checkout / name).write_text(name)
            commands = directory / "commands"
            commands.mkdir()
            write_executable(
                commands / "git",
                f"""
                #!/usr/bin/env python3
                import sys
                if sys.argv[1:] == ["rev-parse", "HEAD"]:
                    print({pin["commit"]!r})
                else:
                    assert sys.argv[1:] == ["status", "--porcelain", "--untracked-files=all"]
                """,
            )
            cargo_home = directory / "cargo-home"
            cargo_home.mkdir()
            ancestor_config = directory / ".cargo/config.toml"
            ancestor_config.parent.mkdir()
            environment = os.environ.copy() | {
                "CARGO_HOME": str(cargo_home),
                "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            }
            target = subprocess.check_output(
                [
                    "rustup",
                    "run",
                    "--install",
                    pin["rust_toolchain"],
                    "rustc",
                    "--print",
                    "host-tuple",
                ],
                text=True,
            ).strip()
            configurations = [
                (
                    {
                        cargo_home
                        / "config.toml": '[build]\nrustflags = ["--cfg=ambient_cargo_config"]\n'
                    },
                    {},
                ),
                (
                    {
                        ancestor_config: f'[target.{target}]\nlinker = "/ambient/linker-wrapper"\n'
                    },
                    {},
                ),
                *(
                    ({}, {name: "/ambient/compiler-override"})
                    for name in (
                        "CARGO_BUILD_RUSTC",
                        "CARGO_BUILD_RUSTC_WRAPPER",
                        "CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER",
                    )
                ),
                ({}, {"CARGO_PROFILE_RELEASE_PANIC": "abort"}),
            ]
            for files, overrides in configurations:
                with self.subTest(files=list(files), overrides=overrides):
                    for location, configuration in files.items():
                        location.write_text(configuration)
                    output = directory / "output"
                    command = [
                        sys.executable,
                        str(script),
                        str(checkout),
                        "--target",
                        target,
                        "--output-dir",
                        str(output),
                    ]
                    result = subprocess.run(
                        command,
                        cwd=project,
                        env=environment | overrides,
                        capture_output=True,
                        text=True,
                    )
                    for location, configuration in files.items():
                        self.assertEqual(location.read_text(), configuration)
                        location.unlink()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        subprocess.check_output(
                            [output / "mcp-console-sandbox"], text=True
                        ),
                        "pinned\n",
                    )
                    # Every ambient configuration must be harmless on a fresh
                    # build, not just when the completed bundle is reused.
                    shutil.rmtree(project / "target")

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
            # Exercise canonical paths on every host, including when its
            # temporary directory itself does not contain a symlink.
            source = directory / "source-directory"
            source.mkdir()
            checkout = directory / "source"
            checkout.symlink_to(source, target_is_directory=True)
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

                for name in (
                    "CARGO_MAKEFLAGS",
                    "CARGO_BUILD_BUILD_DIR",
                    "RUSTC",
                    "RUSTC_WRAPPER",
                    "RUSTC_WORKSPACE_WRAPPER",
                    "CARGO_ENCODED_RUSTFLAGS",
                    "CARGO_BUILD_RUSTFLAGS",
                    "CARGO_TARGET_X86_64_APPLE_DARWIN_RUSTFLAGS",
                    "CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS",
                    "CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER",
                    "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER",
                    "MACOSX_DEPLOYMENT_TARGET",
                ):
                    assert name not in os.environ, name
                Path(os.environ["FAKE_CARGO_ARGUMENTS"]).write_text(json.dumps(sys.argv[1:]))
                target = sys.argv[sys.argv.index("--target") + 1] if "--target" in sys.argv else os.environ["CARGO_BUILD_TARGET"]
                output = Path(os.environ["CARGO_TARGET_DIR"]) / target / "release"
                output.mkdir(parents=True, exist_ok=True)
                (output / "mcp-console-sandbox").write_bytes(b"runner bytes with debug symbols")
                """,
            )
            write_executable(
                commands / "xcrun",
                """
                #!/usr/bin/env python3
                import sys
                from pathlib import Path

                assert sys.argv[1:4] == ["strip", "-S", "-x"], sys.argv
                runner = Path(sys.argv[4])
                assert runner.read_bytes() == b"runner bytes with debug symbols"
                runner.write_bytes(b"runner bytes")
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
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "FAKE_SOURCE_REVISION": pin["commit"],
                    "FAKE_CARGO_ARGUMENTS": str(directory / "cargo.json"),
                    "CARGO_BUILD_TARGET": "x86_64-apple-darwin",
                    "CARGO_MAKEFLAGS": "outer jobserver",
                    "CARGO_BUILD_BUILD_DIR": "/outer/build",
                    "RUSTC": "/outer/rustc",
                    "RUSTC_WRAPPER": "/outer/wrapper",
                    "RUSTC_WORKSPACE_WRAPPER": "/outer/clippy-driver",
                    "CARGO_ENCODED_RUSTFLAGS": "--deny=warnings",
                    "CARGO_BUILD_RUSTFLAGS": "--deny=warnings",
                    "CARGO_TARGET_X86_64_APPLE_DARWIN_RUSTFLAGS": "-C link-arg=-mmacosx-version-min=15.0",
                    "CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS": "-C link-arg=-mmacosx-version-min=15.0",
                    "CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER": "/outer/linker-wrapper",
                    "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER": "/outer/linker-wrapper",
                    "MACOSX_DEPLOYMENT_TARGET": "15.0",
                }
            )
            command = [
                sys.executable,
                str(launcher),
                str(scripts / STAGE_SCRIPT.name),
                str(checkout),
            ]
            # Exercise a real one-token Cargo jobserver with a tiny outer
            # package, rather than serializing the full application build.
            outer = directory / "outer"
            (outer / "src").mkdir(parents=True)
            (outer / "Cargo.toml").write_text(
                '[package]\nname = "staging-fixture"\nversion = "0.1.0"\n'
            )
            (outer / "src/main.rs").write_text("fn main() {}\n")
            (outer / "build.rs").write_text(
                textwrap.dedent("""
                fn main() {
                    assert!(std::env::var_os("CARGO_MAKEFLAGS").is_some());
                    let status = std::process::Command::new(std::env::var_os("STAGING_PYTHON").unwrap())
                        .arg(std::env::var_os("STAGING_LAUNCHER").unwrap())
                        .arg(std::env::var_os("STAGING_SCRIPT").unwrap())
                        .arg(std::env::var_os("STAGING_CHECKOUT").unwrap())
                        .arg("--target")
                        .arg("aarch64-apple-darwin")
                        .status().unwrap();
                    assert!(status.success());
                }
                """)
            )
            outer_environment = os.environ.copy() | {
                "PATH": environment["PATH"],
                "FAKE_SOURCE_REVISION": pin["commit"],
                "FAKE_CARGO_ARGUMENTS": environment["FAKE_CARGO_ARGUMENTS"],
                "RUSTC": shutil.which("rustc"),
                "STAGING_PYTHON": sys.executable,
                "STAGING_LAUNCHER": str(launcher),
                "STAGING_SCRIPT": str(scripts / STAGE_SCRIPT.name),
                "STAGING_CHECKOUT": str(checkout),
            }
            result = subprocess.run(
                [
                    shutil.which("cargo"),
                    "build",
                    "--offline",
                    "--jobs",
                    "1",
                    "--target-dir",
                    str(outer / "target"),
                ],
                cwd=outer,
                env=outer_environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for arguments, target in (
                ([], "aarch64-apple-darwin"),
                (["--target", "x86_64-apple-darwin"], "x86_64-apple-darwin"),
            ):
                with self.subTest(target=target):
                    result = subprocess.run(
                        command + arguments,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(placeholder.read_text(), "/*\n!/.gitignore\n")
                    self.assertEqual(
                        json.loads((directory / "cargo.json").read_text()),
                        [
                            "+1.95.0",
                            "build",
                            "--manifest-path",
                            str(checkout.resolve() / "codex-rs/Cargo.toml"),
                            "--config",
                            str(checkout.resolve() / "codex-rs/.cargo/config.toml"),
                            "--locked",
                            "--release",
                            "-p",
                            "codex-mcp-console-sandbox",
                            "--bin",
                            "mcp-console-sandbox",
                            "--target",
                            target,
                        ],
                    )
                    self.assertEqual(
                        json.loads(
                            (root / "target/sandbox-runner/build.json").read_text()
                        ),
                        {
                            "source_revision": pin["commit"],
                            "target": target,
                            "sha256": {
                                name: hashlib.sha256(contents).hexdigest()
                                for name, contents in (
                                    ("mcp-console-sandbox", b"runner bytes"),
                                    ("LICENSE", b"license\n"),
                                    ("NOTICE", b"notice\n"),
                                )
                            },
                        },
                    )
            data = root / "target" / "sandbox-runner"
            runner = data / "mcp-console-sandbox"
            self.assertEqual(runner.read_bytes(), b"runner bytes")
            self.assertTrue(os.access(runner, os.X_OK))
            self.assertEqual(
                (
                    checkout
                    / "codex-rs/target/x86_64-apple-darwin/release/mcp-console-sandbox"
                ).read_bytes(),
                b"runner bytes with debug symbols",
            )
            self.assertEqual(
                (data / "LICENSE").read_text(),
                "license\n",
            )
            (directory / "cargo.json").unlink()
            # A completed build must serve another Cargo profile without a
            # source checkout, compiler, or nested Cargo invocation.
            for name in ("git", "rustup"):
                (commands / name).rename(commands / f"{name}.working")
                write_executable(commands / name, "#!/bin/sh\nexit 97\n")
            cached_output = directory / "cached-output"
            cached_command = [
                sys.executable,
                str(launcher),
                str(scripts / STAGE_SCRIPT.name),
                "--target",
                "x86_64-apple-darwin",
                "--output-dir",
                str(cached_output),
            ]
            cached_environment = environment.copy()
            cached_environment.pop("MCP_CONSOLE_SANDBOX_SOURCE", None)
            for name in (
                "CARGO_BUILD_RUSTFLAGS",
                "CARGO_TARGET_X86_64_APPLE_DARWIN_RUSTFLAGS",
                "CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS",
                "CARGO_TARGET_X86_64_APPLE_DARWIN_LINKER",
                "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER",
            ):
                cached_environment.pop(name)
            # Lowering the application's deployment target must reuse a runner
            # built for the pinned compiler's default deployment target.
            cached_environment["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
            result = subprocess.run(
                cached_command,
                env=cached_environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("mcp-console-sandbox", "LICENSE", "NOTICE", "build.json"):
                self.assertEqual(
                    (cached_output / name).read_bytes(), (data / name).read_bytes()
                )
            self.assertFalse((directory / "cargo.json").exists())
            for name in ("git", "rustup"):
                (commands / f"{name}.working").replace(commands / name)
            for changes in (
                {"FAKE_SOURCE_REVISION": "b" * 40},
                {"FAKE_SOURCE_DIRTY": " M Cargo.lock"},
            ):
                result = subprocess.run(
                    command, env=environment | changes, capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "cargo.json").exists())

            # Both provenance and the build recipe invalidate the artifact.
            pin["protocol_version"] = 3
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            for recipe_change in (False, True):
                if recipe_change:
                    with (scripts / STAGE_SCRIPT.name).open("a") as script:
                        script.write("\n# changed build recipe\n")
                result = subprocess.run(
                    command + ["--target", "x86_64-apple-darwin"],
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((directory / "cargo.json").exists())
                (directory / "cargo.json").unlink()

            for name in ("mcp-console-sandbox", "LICENSE", "NOTICE"):
                with self.subTest(corrupt_file=name):
                    originals = {
                        path: path.read_bytes()
                        for path in (
                            root / "target/sandbox-runner-cache/artifacts"
                        ).rglob(name)
                    }
                    try:
                        for path in originals:
                            path.write_bytes(b"corrupt cached file")
                        result = subprocess.run(
                            cached_command,
                            env=cached_environment,
                            capture_output=True,
                            text=True,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("does not match", result.stderr)
                        self.assertFalse((directory / "cargo.json").exists())
                    finally:
                        for path, original in originals.items():
                            path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
