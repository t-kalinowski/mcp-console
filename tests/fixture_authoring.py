#!/usr/bin/env python3
"""Public development-command regressions for fixture authoring."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent


class FixtureAuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def check(
        self, source: str, *, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        path = self.root / "fixture.py"
        path.write_text(source)
        return subprocess.run(
            [sys.executable, ROOT / "scripts/check-fixtures", path],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )

    def test_checks_formatting_without_language_runtimes(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        result = self.check(
            # fmt: python
            code('''
                # fmt: python
                python = code("""
                    raise RuntimeError("must not run")
                    """)
                # fmt: r
                r = code("""
                    stop("must not run")
                    """)
                client.send(python=("this_is_a_single_line_expression"))
                '''),
            environment=os.environ | {"PATH": str(empty), "R_HOME": str(empty)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixtures: 0 error(s)", result.stdout)

    def test_accepts_invalid_and_incomplete_language_source(self) -> None:
        result = self.check(
            # fmt: python
            code('''
                # fmt: python
                python = code("""
                    if True
                        pass
                    """)
                # fmt: r
                r = code("""
                    answer <- (
                    """)
                ''')
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_requires_directives_and_code_indentation(self) -> None:
        result = self.check(
            # fmt: python
            code('''
                client.send(
                    python=code("""
                    print(42)
                    """)
                )
                # fmt: r
                r = code("""
                print(42)
                """)
                ''')
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing # fmt: python", result.stdout)
        self.assertIn(
            "indent code() payload and closing delimiter by four spaces", result.stdout
        )

    def test_program_expressions_exclude_interpolation_and_replacement_data(
        self,
    ) -> None:
        # fmt: python
        interpolated = code('''
            # fmt: python
            python = code(f"""
                {render(DATA)}
                """)
            ''').replace("DATA", "'''human readable\n    label'''")
        # fmt: python
        transformed = code(r'''
            # fmt: python
            python = code("""
                print(42)
                """).replace("not Python prose!\n", "")
            ''')
        for source in (interpolated, transformed):
            with self.subTest(source=source):
                result = self.check(source)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_multiline_code_closes_on_its_own_indented_line(self) -> None:
        result = self.check(
            # fmt: python
            code('''
                # fmt: python
                python = code("""
                    print(42)""")
                ''')
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("indent code() payload and closing delimiter", result.stdout)

    def test_multiline_code_starts_after_the_opening_delimiter(self) -> None:
        result = self.check(
            # fmt: python
            code('''
                # fmt: python
                python = code("""if True:
                    print(2)
                    """)
                ''')
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("start code() payload after the opening line", result.stdout)

    def test_escaped_multiline_cells_are_checked(self) -> None:
        result = self.check(
            # fmt: python
            code(r"""
                python = "print(42)\nreturn 1"
                messages = [message for message in ("a\n", "b\n")]
                """)
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("missing # fmt: python", result.stdout)

    def test_annotated_assignments_require_directives(self) -> None:
        result = self.check(
            # fmt: python
            code('''
                python: str = code("""
                    print(42)
                    """)
                r: str = code("""
                    print(42)
                    """)
                ''')
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("missing # fmt: python", result.stdout)
        self.assertIn("missing # fmt: r", result.stdout)

    def test_directive_matches_program_language(self) -> None:
        result = self.check(
            # fmt: python
            code('''
                # fmt: r
                python = code("""
                    print(42)
                    """)
                client.send(
                    # fmt: python
                    r=code("""
                        print(42)
                        """)
                )
                ''')
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("expected # fmt: python; found # fmt: r", result.stdout)
        self.assertIn("expected # fmt: r; found # fmt: python", result.stdout)
        result = self.check(
            # fmt: python
            code('''
                r = client.send(
                    # fmt: python
                    python=code("""
                        print(42)
                        """)
                )
                ''')
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_format_reports_every_tool_and_strict_status(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts/format", scripts / "format")
        commands = self.root / "commands"
        commands.mkdir()
        for name in ("ruff", "yamark", "cargo", "air"):
            path = commands / name
            path.write_text(
                f"#!/bin/sh\necho {name} >> attempts\n"
                + ("exit 7\n" if name == "yamark" else "exit 0\n")
            )
            path.chmod(0o755)
        checker = scripts / "check-fixtures"
        checker.write_text("#!/bin/sh\necho fixtures >> attempts\nexit 0\n")
        checker.chmod(0o755)
        for arguments, status in (([], 0), (["--strict"], 1)):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [scripts / "format", *arguments],
                    cwd=self.root,
                    env=os.environ | {"PATH": str(commands)},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    result.returncode, status, result.stdout + result.stderr
                )
                self.assertEqual(
                    (self.root / "attempts").read_text().splitlines(),
                    ["ruff", "yamark", "cargo", "air", "fixtures"],
                )
                self.assertIn("ruff: ok", result.stdout)
                self.assertIn("yamark: failed (7)", result.stdout)
                (self.root / "attempts").unlink()


if __name__ == "__main__":
    unittest.main()
