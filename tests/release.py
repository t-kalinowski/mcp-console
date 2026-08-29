from __future__ import annotations

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
        write_executable(libexec / "mcp-console-sandbox", "#!/bin/sh\nexit 0\n")
        tool_bin = directory / "bin"
        tool_bin.mkdir()

        executable_source = """
            #!/usr/bin/python3
            import json
            import os
            import shutil
            import sys
            import time
            from pathlib import Path

            if sys.argv[1:] == ["--version"]:
                print("mcp-console 0.0.2")
            elif sys.argv[1:] == ["--help"]:
                print("mcp-console help")
            elif sys.argv[1:3] == ["sandbox", "--"]:
                private_runner = Path(sys.argv[0]).resolve().parent.parent / "libexec" / "mcp-console-sandbox"
                if not private_runner.is_file() or not os.access(private_runner, os.X_OK):
                    raise SystemExit(3)
                if shutil.which("mcp-console-sandbox") is not None:
                    raise SystemExit(4)
                Path(os.environ["FAKE_SANDBOX_PATH_RECORD"]).write_text(
                    os.environ.get("PATH", ""), encoding="utf-8"
                )
            elif sys.argv[1:] == ["serve"]:
                if os.environ.get("FAKE_MCP_PARTIAL"):
                    sys.stdout.write('{"jsonrpc":')
                    sys.stdout.flush()
                    time.sleep(1)
                    raise SystemExit(0)
                if os.environ.get("FAKE_MCP_HANG"):
                    time.sleep(60)
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
        data = "mcp_console-0.0.2.data/data"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, source, mode in (
                (
                    f"{data}/libexec/mcp-console-sandbox",
                    "#!/bin/sh\nexit 0\n",
                    0o100755,
                ),
                (
                    f"{data}/share/licenses/mcp-console/Codex-LICENSE",
                    "Apache-2.0\n",
                    0o100644,
                ),
                (
                    f"{data}/share/licenses/mcp-console/Codex-NOTICE",
                    "OpenAI Codex\n",
                    0o100644,
                ),
            ):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, source)
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

    def test_smoke_wheel_requires_private_runner_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)

            with zipfile.ZipFile(wheel, "w"):
                pass
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                cwd=directory,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0)
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
            recorded_path = Path(environment["FAKE_SANDBOX_PATH_RECORD"]).read_text()
            self.assertEqual(len(recorded_path.split(os.pathsep)), 1)
            self.assertNotIn(str(Path(environment["UV_TOOL_BIN_DIR"])), recorded_path)

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

            environment["FAKE_MCP_HANG"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "0.2",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out", result.stderr)

            del environment["FAKE_MCP_HANG"]
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

            del environment["FAKE_MCP_EVALUATION_HANG"]
            environment["FAKE_MCP_PARTIAL"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "0.2",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out", result.stderr)

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

    def test_workflows_delegate_release_logic_to_the_script(self) -> None:
        ci_source = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()
        release_source = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        ci = " ".join(ci_source.replace("\\\n", " ").split())
        release = " ".join(release_source.replace("\\\n", " ").split())

        self.assertIn("scripts/release.py smoke-wheel", ci)
        self.assertIn("scripts/release.py smoke-wheel", release)
        self.assertIn("scripts/stage-sandbox-runner", ci)
        self.assertIn("scripts/stage-sandbox-runner", release)
        self.assertIn("sandbox-runner.json", ci)
        self.assertIn("sandbox-runner.json", release)
        self.assertIn("scripts/release.py validate-publish", release)
        self.assertIn("scripts/release.py verify-wheel-set", release)
        self.assertNotIn("python -", ci_source)
        self.assertNotIn("python -", release_source)
        self.assertNotIn("python3 -", ci_source)
        self.assertNotIn("python3 -", release_source)
        self.assertIn("GITHUB_PAT: ${{ github.token }}", release_source)
        self.assertIn('install.libs("gettext")', release_source)
        self.assertEqual(ci_source.count("UV_VERSION: 0.12.4"), 1)
        self.assertEqual(ci_source.count("version: ${{ env.UV_VERSION }}"), 1)
        self.assertEqual(release_source.count("version: 0.12.4"), 2)

    def test_private_runner_build_has_one_immutable_pin(self) -> None:
        pin = json.loads((ROOT / "sandbox-runner.json").read_text(encoding="utf-8"))
        self.assertEqual(pin["repository"], "t-kalinowski/codex")
        self.assertEqual(pin["release"], "rust-v0.150.1")
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(pin["protocol_version"], 1)
        self.assertEqual(pin["rust_toolchain"], "1.95.0")

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn('data = "target/private-wheel-data"', pyproject)
        self.assertNotIn(pin["commit"], ci)
        self.assertNotIn(pin["commit"], release)
        self.assertIn(
            "rust-toolchain: ${{ steps.sandbox-pin.outputs.rust_toolchain }}",
            release,
        )

    def test_stage_runner_stamps_the_pinned_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "project"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(STAGE_SCRIPT, scripts / "stage-sandbox-runner")
            shutil.copyfile(ROOT / "sandbox-runner.json", root / "sandbox-runner.json")

            checkout = directory / "codex"
            crate = checkout / "codex-rs" / "mcp-console-sandbox"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").touch()
            (checkout / "LICENSE").write_text("license\n", encoding="utf-8")
            (checkout / "NOTICE").write_text("notice\n", encoding="utf-8")

            commands = directory / "commands"
            commands.mkdir()
            pin = json.loads((ROOT / "sandbox-runner.json").read_text(encoding="utf-8"))
            write_executable(
                commands / "git",
                f"""
                #!/bin/sh
                if test "$1 $2" = "rev-parse HEAD"; then
                  echo {pin["commit"]}
                elif test "$1 $2" = "status --porcelain"; then
                  exit 0
                else
                  exit 2
                fi
                """,
            )
            write_executable(
                commands / "cargo",
                """
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                expected = os.environ["EXPECTED_PIN"]
                if os.environ.get("STABLE_GIT_COMMIT") != expected:
                    raise SystemExit(3)
                arguments = sys.argv[1:]
                target = arguments[arguments.index("--target") + 1] if "--target" in arguments else None
                output = Path(os.environ["CARGO_TARGET_DIR"])
                if target is not None:
                    output /= target
                runner = output / "release" / "mcp-console-sandbox"
                runner.parent.mkdir(parents=True)
                runner.write_text("runner\\n", encoding="utf-8")
                """,
            )
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"
            environment["EXPECTED_PIN"] = pin["commit"]
            environment["STABLE_GIT_COMMIT"] = "f" * 40

            result = subprocess.run(
                [sys.executable, str(scripts / "stage-sandbox-runner"), str(checkout)],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            staged = root / "target" / "private-wheel-data" / "data"
            runner = staged / "libexec" / "mcp-console-sandbox"
            self.assertEqual(runner.read_text(encoding="utf-8"), "runner\n")
            self.assertTrue(os.access(runner, os.X_OK))
            self.assertEqual(
                (
                    staged / "share" / "licenses" / "mcp-console" / "Codex-LICENSE"
                ).read_text(encoding="utf-8"),
                "license\n",
            )

    def test_ci_uses_one_macos_runner_and_installs_tools_first(self) -> None:
        ci_source = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()

        self.assertEqual(ci_source.count("runs-on: macos-latest"), 1)
        self.assertEqual(ci_source.count("actions/checkout@v7"), 2)
        self.assertEqual(ci_source.count("actions/setup-python@v7"), 1)
        self.assertEqual(ci_source.count("r-lib/actions/setup-r@v2"), 1)
        self.assertEqual(ci_source.count("dtolnay/rust-toolchain@stable"), 1)
        self.assertEqual(ci_source.count("Swatinem/rust-cache@v2"), 1)
        self.assertEqual(ci_source.count("use-public-rspm: always"), 1)
        self.assertIn('upgrade: "FALSE"', ci_source)
        self.assertLess(
            ci_source.index("- name: Install tools"),
            ci_source.index("- name: Install R\n"),
        )
        self.assertLess(
            ci_source.index("- name: Run core checks"),
            ci_source.index("- name: Install R package dependencies"),
        )
        self.assertLess(
            ci_source.index("- name: Check R package"),
            ci_source.index("- name: Install transcript dependencies"),
        )
        package_dependencies_start = ci_source.index(
            "- name: Install R package dependencies"
        )
        package_dependencies_end = ci_source.index("- name: Check R package")
        package_dependencies = ci_source[
            package_dependencies_start:package_dependencies_end
        ]
        self.assertIn(
            "cache: false",
            package_dependencies,
        )
        self.assertLess(
            ci_source.index("- name: Install transcript dependencies"),
            ci_source.index("- name: Prewarm default R environment"),
        )
        self.assertLess(
            ci_source.index("- name: Prewarm default R environment"),
            ci_source.index("- name: Run transcripts"),
        )
        self.assertIn(
            '-e \'sessionInfo(package = c("tidyverse", "reticulate", '
            '"DBI", "duckdb", "arrow", "nanoarrow"))\'',
            ci_source,
        )


if __name__ == "__main__":
    unittest.main()
