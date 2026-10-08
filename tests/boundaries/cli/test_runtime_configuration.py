"""Runtime configuration syntax at the public CLI boundary."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_accepts_runtime_schema(binary: Path) -> Transcript:
    configurations = [
        {"r": {"resolution": "explicit", "packages": []}},
        {"r": "./installed R", "python": {"managed": {}}},
        {"r": {"resolution": "disabled"}},
        {
            "python": {
                "managed": {
                    "version": ">=3.12,<3.14",
                    "packages": [],
                    "resolution": "startup_only",
                }
            }
        },
        {"python": {"existing": ".venv"}},
        {
            "python": {
                "first_available": [
                    {"existing": ".venv"},
                    "active_venv",
                    {"managed": {"resolution": "explicit"}},
                ]
            }
        },
    ]
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for document in configurations:
            config.write_text(json.dumps(document))
            result = subprocess.run(
                [binary, "sandbox", "--", sys.executable, "-c", "print('accepted')"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert (
                result.returncode == 0
                and result.stdout == "accepted\n"
                and not result.stderr
            ), (document, result)
    return [{"accepted": document} for document in configurations]


@requires(SANDBOX)
def test_rejects_runtime_schema(binary: Path) -> Transcript:
    configurations = [
        ("r.executable", ""),
        ("r.packages", [None]),
        ("r.packages", [""]),
        ("r", {"resolution": "disabled", "packages": ["dplyr"]}),
        ("r.resolution", "sometimes"),
        ("python", {}),
        ("python.managed", []),
        ("python.managed", None),
        ("r.packages", None),
        ("python", {"managed": {}, "existing": ".venv"}),
        ("python.managed.version", 3.13),
        ("python.managed.version", "python3"),
        ("python.managed.resolution", "disabled"),
        ("python.managed.packages", [""]),
        ("python.managed.packages", ["https://example.com/package.whl"]),
        ("python", {"existing": ".venv", "packages": ["numpy"]}),
        ("python.first_available", []),
        ("python.first_available", ["active_venv", "active_venv"]),
        ("python.first_available", [{"managed": {}}, {"existing": ".venv"}]),
        ("python.first_available", [{"first_available": ["active_venv"]}]),
    ]
    transcript = []
    with TemporaryDirectory() as temporary:
        for path, value in configurations:
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "-c",
                    path + "=" + json.dumps(value),
                    "--",
                    "unused-workload",
                ],
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert (
                result.returncode == 1
                and not result.stdout
                and path.split(".")[0] in result.stderr
            ), (path, value, result)
            transcript.append({"path": path, "value": value, "stderr": result.stderr})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
