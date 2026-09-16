#!/usr/bin/env python3
"""Public command tests for local development reports."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def command(self, name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, ROOT / "scripts" / name, *arguments],
            cwd=self.root,
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


if __name__ == "__main__":
    unittest.main()
