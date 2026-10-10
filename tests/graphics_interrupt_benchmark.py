#!/usr/bin/env python3
"""Check the Mac measurement CLI's report against real MCP admission output."""

import io
import json
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from support.client import McpClient
from support.requirements import MACOS_SANDBOX, R

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(
    MACOS_SANDBOX.available and R.available,
    "requires the Mac measurement host and R",
)
class GraphicsInterruptBenchmarkTests(unittest.TestCase):
    def test_reports_admission_text_and_image_once(self) -> None:
        send = McpClient.send

        def collect_admission(client: McpClient, **arguments: Any) -> dict:
            # Exercise a completed admission deterministically, through the real
            # MCP boundary. Only its wait budget and R output are fixture-owned.
            if "r" in arguments and arguments.get("timeout_ms") == 0:
                arguments["timeout_ms"] = 30000
                arguments["r"] += '\ncat("admission output\\n")'
            return send(client, **arguments)

        for action in ("complete", "interrupt", "restart"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                arguments = [
                    str(ROOT / "docs/benchmarks/graphics_interrupt.py"),
                    tmp,
                    action,
                    "lines",
                    action,
                    "--pixels",
                    "512",
                    "--vertices",
                    "16",
                ]
                with (
                    patch.object(sys, "argv", arguments),
                    patch.object(McpClient, "send", collect_admission),
                    redirect_stdout(io.StringIO()),
                ):
                    runpy.run_path(arguments[0], run_name="__main__")

                calls = json.loads((output / f"{action}.calls.json").read_text())
                admission = next(
                    call["result"]
                    for call in calls
                    if call.get("send", {})
                    .get("r", "")
                    .endswith('cat("admission output\\n")')
                )
                self.assertEqual(
                    sum(part["type"] == "image" for part in admission["content"]),
                    1,
                    "fixture must deliver the plot in the admission response",
                )
                self.assertIn(
                    "admission output\n",
                    "".join(
                        part["text"]
                        for part in admission["content"]
                        if part["type"] == "text"
                    ),
                )
                report = json.loads((output / f"{action}.json").read_text())
                with self.subTest(field="images"):
                    self.assertEqual(report["images"], 1)
                with self.subTest(field="settled_text"):
                    self.assertEqual(
                        report["settled_text"].count("admission output\n"), 1
                    )


if __name__ == "__main__":
    unittest.main()
