#!/usr/bin/env python3

"""Static guards for the sandbox process-boundary dependency direction."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"


def rust_sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.rs"))


def flatten_use(statement: str) -> str:
    """Expand explicit use trees; this lexical guard does not resolve names."""
    statement = re.sub(r"\s*::\s*", "::", statement)
    # Expand the innermost group first so each parent prefix reaches its leaves.
    while group := re.search(r"(\w+(?:::\w+)*::)\{([^{}]*)\}", statement):
        prefix, members = group.groups()
        paths = ", ".join(
            prefix + member.strip() for member in members.split(",") if member.strip()
        )
        statement = statement[: group.start()] + paths + statement[group.end() :]
    return " ".join(statement.split())


def matching_lines(paths: list[Path], needles: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        candidates = list(enumerate(source.splitlines(), 1))
        candidates.extend(
            (
                source.count("\n", 0, statement.start()) + 1,
                flatten_use(statement.group()),
            )
            for statement in re.finditer(r"\buse\s+[^;]+;", source)
        )
        for line_number, line in candidates:
            if any(needle in line for needle in needles):
                matches.append(
                    f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}"
                )
    return list(dict.fromkeys(matches))


class SandboxProcessBoundaryTests(unittest.TestCase):
    def test_server_relay_and_worker_do_not_import_sandbox_internals(self) -> None:
        host_sources = [
            SOURCE_ROOT / "server.rs",
            SOURCE_ROOT / "server_transport.rs",
            SOURCE_ROOT / "worker_client.rs",
            SOURCE_ROOT / "worker_relay.rs",
            SOURCE_ROOT / "relay_protocol.rs",
            SOURCE_ROOT / "worker.rs",
            SOURCE_ROOT / "worker_protocol.rs",
            *rust_sources(SOURCE_ROOT / "worker_client"),
            *rust_sources(SOURCE_ROOT / "worker_relay"),
            *rust_sources(SOURCE_ROOT / "worker"),
        ]
        missing = [path for path in host_sources if not path.is_file()]
        self.assertEqual(missing, [], f"missing source files: {missing}")
        violations = matching_lines(
            host_sources,
            ("crate::sandbox", "super::sandbox", "sandbox::platform"),
        )
        self.assertEqual(
            violations,
            [],
            "server, relay, or worker code depends on private sandbox implementation:\n"
            + "\n".join(violations),
        )

    def test_sandbox_does_not_depend_on_relay_or_server_protocols(self) -> None:
        sandbox_sources = [
            SOURCE_ROOT / "sandbox.rs",
            *rust_sources(SOURCE_ROOT / "sandbox"),
        ]
        missing = [path for path in sandbox_sources if not path.is_file()]
        self.assertEqual(missing, [], f"missing source files: {missing}")
        violations = matching_lines(
            sandbox_sources,
            (
                "crate::worker",
                "crate::relay_protocol",
                "crate::server",
            ),
        )
        self.assertEqual(
            violations,
            [],
            "sandbox code depends on relay or server internals:\n"
            + "\n".join(violations),
        )

    def test_relay_does_not_know_sandbox_control_arguments(self) -> None:
        relay_sources = [
            SOURCE_ROOT / "worker_relay.rs",
            SOURCE_ROOT / "relay_protocol.rs",
            *rust_sources(SOURCE_ROOT / "worker_relay"),
        ]
        missing = [path for path in relay_sources if not path.is_file()]
        self.assertEqual(missing, [], f"missing source files: {missing}")
        violations = matching_lines(
            relay_sources,
            ("--exit-with-parent", "sandbox-manager", "sandbox-target"),
        )
        self.assertEqual(
            violations,
            [],
            "relay code knows sandbox control-plane arguments:\n"
            + "\n".join(violations),
        )


class ArchitectureCheckTests(unittest.TestCase):
    def test_grouped_imports_preserve_dependency_checks(self) -> None:
        cases = (
            ("server.rs", "use crate::{sandbox, server};", False),
            ("worker_client/macos.rs", "use super::{sandbox as private};", False),
            (
                "server.rs",
                """
                use crate :: {
                    process_exit,
                    sandbox :: {self, child::Cleanup as Cleanup},
                };
                """,
                False,
            ),
            ("sandbox/macos.rs", "use crate::{worker, relay_protocol};", False),
            ("sandbox/macos.rs", "use crate::{server::{self, Server}};", False),
            ("server.rs", "use crate::{process_exit, cli};", True),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            checker = root / "tests" / "architecture.py"
            shutil.copy2(__file__, checker)
            shutil.copytree(SOURCE_ROOT, root / "src")
            for relative, statement, allowed in cases:
                with self.subTest(path=relative, statement=statement):
                    path = root / "src" / relative
                    original = path.read_text(encoding="utf-8")
                    path.write_text(original + "\n" + statement, encoding="utf-8")
                    try:
                        result = subprocess.run(
                            [sys.executable, checker, "SandboxProcessBoundaryTests"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                    finally:
                        path.write_text(original, encoding="utf-8")
                    if allowed:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn("depends on", result.stderr)
                        self.assertIn(f"src/{relative}:", result.stderr)


if __name__ == "__main__":
    unittest.main()
