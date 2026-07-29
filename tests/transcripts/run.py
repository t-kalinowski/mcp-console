# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12==0.1.0"]
# ///

import difflib
import os
import runpy
import sys
from pathlib import Path

from support import McpClient
from yaml12 import format_yaml


directory = Path(__file__).resolve().parent
root = directory.parents[1]
binary = root / "target" / "debug" / "mcp-console"
case_paths = sorted((directory / "cases").glob("*.py"))

assert binary.is_file(), f"{binary.relative_to(root)} is missing; run ./test"
assert case_paths, "no transcript cases found"

for case_path in case_paths:
    namespace = runpy.run_path(str(case_path))
    run_case = namespace.get("run")
    assert callable(run_case), f"{case_path.relative_to(root)} must define run(client)"

    argument_sets = namespace.get("server_argument_sets", ((),))
    transcripts = []
    for arguments in argument_sets:
        client = McpClient(binary, arguments)
        run_case(client)
        transcripts.append((arguments, format_yaml(client.finish(), multi=True)))

    transcript = transcripts[0][1]
    for arguments, other_transcript in transcripts[1:]:
        if other_transcript != transcript:
            sys.stderr.writelines(
                difflib.unified_diff(
                    transcript.splitlines(keepends=True),
                    other_transcript.splitlines(keepends=True),
                    fromfile="mcp-console",
                    tofile=" ".join(("mcp-console", *arguments)),
                )
            )
            raise SystemExit(
                f"{case_path.relative_to(root)} differs between server entry points"
            )

    golden = directory / "golden" / f"{case_path.stem}.yaml"

    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(transcript, encoding="utf-8")
        print(f"updated {golden.relative_to(root)}")
    elif not golden.exists():
        raise SystemExit(
            f"{golden.relative_to(root)} is missing; run UPDATE_GOLDEN=1 ./test"
        )
    else:
        expected = golden.read_text(encoding="utf-8")
        if transcript != expected:
            sys.stderr.writelines(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    transcript.splitlines(keepends=True),
                    fromfile=str(golden.relative_to(root)),
                    tofile="actual",
                )
            )
            raise SystemExit(
                f"{case_path.relative_to(root)} differs from its golden snapshot"
            )

        print(f"{golden.relative_to(root)}: ok")
