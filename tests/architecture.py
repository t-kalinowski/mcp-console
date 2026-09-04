#!/usr/bin/env python3

"""Static guards for the sandbox process-boundary dependency direction."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"


def rust_sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.rs"))


def matching_lines(paths: list[Path], needles: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(needle in line for needle in needles):
                matches.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
    return matches


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
        sandbox_sources = [SOURCE_ROOT / "sandbox.rs", *rust_sources(SOURCE_ROOT / "sandbox")]
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
            "sandbox code depends on relay or server internals:\n" + "\n".join(violations),
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
            "relay code knows sandbox control-plane arguments:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
