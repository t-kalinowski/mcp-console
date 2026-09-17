#!/usr/bin/env python3
"""Public command tests for local development reports."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent


class DevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=self.root, text=True
        ).strip()

    def write(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def command(
        self,
        name: str,
        *arguments: str,
        script_root: Path = ROOT,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, script_root / "scripts" / name, *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_review_diff_uses_intended_base_and_separates_generated_changes(
        self,
    ) -> None:
        self.write("src/lib.rs", "first\n")
        first = self.commit()
        self.write("src/lib.rs", "first\nlower layer\n")
        parent = self.commit()
        self.write("src/lib.rs", "first\nlower layer\nnew behavior\n")
        self.write("tests/snapshots/case.yaml", "result: 42\nstatus: done\n")
        self.write("tests/case.py", "assert True\n")
        self.write("docs/change.md", "Explanation\n")
        self.write("scripts/a tool", "command\n")
        (self.root / "tests/binary").write_bytes(b"\0binary")
        self.git("add", ".")
        self.write("scratch.txt", "not staged\n")
        result = self.command("review-diff", parent, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["base"], parent)
        self.assertEqual(report["groups"]["production"]["added"], 1)
        self.assertEqual(report["groups"]["snapshots"]["added"], 2)
        self.assertEqual(report["groups"]["tests"]["binary_files"], 1)
        self.assertEqual(report["groups"]["tooling"]["files"], 1)
        self.assertEqual(report["groups"]["documentation"]["added"], 1)
        self.assertEqual(report["untracked"], ["scratch.txt"])
        wider = self.command("review-diff", first, "--json")
        self.assertEqual(json.loads(wider.stdout)["groups"]["production"]["added"], 2)
        human = self.command("review-diff", parent)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("snapshots", human.stdout)
        self.assertIn("1 untracked file(s) excluded", human.stdout)

    def test_review_diff_requires_a_valid_explicit_base(self) -> None:
        self.write("README.md", "Fixture\n")
        self.commit()
        for arguments in ((), ("missing-branch",)):
            with self.subTest(arguments=arguments):
                result = self.command("review-diff", *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)

    def test_preflight_reports_preparation_and_optional_skips_without_building(
        self,
    ) -> None:
        for name in (
            "scripts/preflight",
            "scripts/stage-sandbox-runner",
            "checkout_workflow.py",
            "sandbox-runner.json",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        shutil.copytree(
            ROOT / "tests/support",
            self.root / "tests/support",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        commands = self.root / "commands"
        commands.mkdir()
        (commands / "git").symlink_to(shutil.which("git"))
        for name in ("uv", "cargo", "rustup", "R", "Rscript"):
            self.write(
                f"commands/{name}",
                f"#!{sys.executable}\n"
                # fmt: python
                + code(r"""
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    name = Path(sys.argv[0]).name
                    arguments = sys.argv[1:]
                    with open("probes.jsonl", "a") as log:
                        log.write(json.dumps([name, *arguments]) + "\n")
                    if arguments == ["--version"]:
                        print(name + " fixture version")
                    elif name == "uv" and arguments == ["cache", "dir"]:
                        print(os.environ["FIXTURE_CACHE"])
                    elif name == "R" and arguments == ["RHOME"]:
                        print(os.environ["FIXTURE_R_HOME"])
                    elif name == "rustup" and arguments == ["show", "active-toolchain"]:
                        print("fixture-console-toolchain (default)")
                    else:
                        raise AssertionError((name, arguments))
                    """),
            )
            (commands / name).chmod(0o755)
        environment = os.environ | {
            "PATH": str(commands),
            "FIXTURE_CACHE": str(self.root / "shared-cache"),
            "FIXTURE_R_HOME": str(self.root / "R-home"),
            "R_HOME": "",
            "RETICULATE_PYTHON": "/explicit/workload/python",
            "MCP_CONSOLE_SANDBOX_SOURCE": str(self.root / "runner-source"),
            "MCP_CONSOLE_TEST_DOCKER_IMAGE": "",
            "MCP_CONSOLE_TEST_SBX_TEMPLATE": "",
            "MCP_CONSOLE_TEST_SSH_EXTERNAL": "",
            "MCP_CONSOLE_TEST_SSH_HOST": "",
        }
        self.write(
            "runner-source/codex-rs/rust-toolchain.toml",
            '[toolchain]\nchannel = "fixture-runner-toolchain"\n',
        )
        self.commit()
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["checkout"], str(self.root.resolve()))
        self.assertEqual(report["required_missing"], [])
        self.assertTrue(report["preparation_needed"])
        self.assertEqual(
            report["companion"]["source_toolchain"], "fixture-runner-toolchain"
        )
        self.assertEqual(
            report["runtime"]["python_selection"], "/explicit/workload/python"
        )
        self.assertEqual(report["runtime"]["r_home"], str(self.root / "R-home"))
        self.assertEqual(report["caches"]["uv"], str(self.root / "shared-cache"))
        self.assertTrue(
            all(item["status"] == "skip" for item in report["providers"].values())
        )
        self.assertFalse((self.root / "target").exists())
        self.assertFalse((self.root / ".dev-workflow").exists())
        pin = json.loads((self.root / "sandbox-runner.json").read_text())
        self.write(
            "target/sandbox-runner-build.json",
            json.dumps({"source_revision": pin["commit"], "target": "fixture-target"}),
        )
        for path in (
            "target/release/mcp-console",
            "target/libexec/mcp-console-sandbox",
            "wheel-data/data/libexec/mcp-console-sandbox",
        ):
            self.write(path, "fixture\n")
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["preparation_needed"], [])
        self.write(
            "target/sandbox-runner-build.json",
            json.dumps({"source_revision": "0" * 40, "target": "fixture-target"}),
        )
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["preparation_needed"]), 2)
        (commands / "cargo").write_text(
            "#!/bin/sh\necho no installed toolchain >&2\nexit 9\n"
        )
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("cargo", report["required_missing"])
        self.assertIn("no installed toolchain", report["tools"]["cargo"]["error"])
        (commands / "cargo").unlink()
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("cargo", json.loads(result.stdout)["required_missing"])


if __name__ == "__main__":
    unittest.main()
