from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"


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
            import json
            import os
            import sys
            import time

            if sys.argv[1:] == ["--version"]:
                print("mcp-console 0.0.2")
            elif sys.argv[1:] == ["--help"]:
                print("mcp-console help")
            elif sys.argv[1:3] == ["sandbox", "--"]:
                pass
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
        wheel.touch()
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "UV_TOOL_DIR": str(directory / "tool"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
            }
        )
        return environment, wheel, cargo_bin

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
        self.assertIn("scripts/release.py validate-publish", release)
        self.assertIn("scripts/release.py verify-wheel-set", release)
        self.assertNotIn("python -", ci_source)
        self.assertNotIn("python -", release_source)
        self.assertNotIn("python3 -", ci_source)
        self.assertNotIn("python3 -", release_source)
        self.assertIn("GITHUB_PAT: ${{ github.token }}", release_source)
        self.assertIn('install.libs("gettext")', release_source)
        self.assertEqual(ci_source.count('UV_VERSION: "0.12.4"'), 1)
        self.assertEqual(ci_source.count("version: ${{ env.UV_VERSION }}"), 2)
        self.assertEqual(release_source.count('version: "0.12.4"'), 2)

    def test_ci_prefers_binary_r_packages_and_installs_uv_tools_first(self) -> None:
        ci_source = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()
        checks, transcripts = ci_source.split("\n  transcripts:\n", 1)

        self.assertEqual(ci_source.count("use-public-rspm: always"), 2)
        self.assertIn('upgrade: "FALSE"', transcripts)
        self.assertLess(
            checks.index("- name: Install IR"), checks.index("- name: Install R\n")
        )
        self.assertLess(
            transcripts.index("- name: Install transcript tools"),
            transcripts.index("- name: Install R\n"),
        )
        self.assertIn(
            '-e \'sessionInfo(package = c("tidyverse", "reticulate", '
            '"DBI", "duckdb", "arrow", "nanoarrow"))\'',
            transcripts,
        )


if __name__ == "__main__":
    unittest.main()
