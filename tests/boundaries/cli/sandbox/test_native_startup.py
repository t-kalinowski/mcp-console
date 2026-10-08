"""Native containment precedes execution of target loader constructors."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires


def constructor(binary: Path, *, restricted_reads: bool) -> Transcript:
    with TemporaryDirectory(prefix="native-startup-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        working = root / "workspace"
        working.mkdir()
        protected = root / "protected"
        protected.write_text("preserved")
        target = working / "target"
        fixture = (
            Path(__file__).resolve().parents[3] / "fixtures/native/target_constructor.c"
        )
        subprocess.run(
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", target, fixture],
            check=True,
            capture_output=True,
        )
        if restricted_reads:
            # ELF's interpreter and libraries may use /lib or /lib64 aliases.
            reads = ["/usr", "/bin", "/lib", "/lib64", str(working)]
            if sys.platform == "darwin":
                reads.extend(["/System", "/Library"])
            config = working / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "filesystem": {
                                "read_only": reads,
                            }
                        }
                    }
                )
            )
            alias = working / "target-alias"
            alias.symlink_to(target)
            target = alias
        result = subprocess.run(
            [binary, "sandbox", "--", str(target)],
            cwd=working,
            env={**os.environ, "MCP_CONSOLE_TEST_PROTECTED_FILE": str(protected)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result
        assert result.stderr == "", result
        assert result.stdout == "target constructor observed native enforcement\n", (
            result
        )
        assert protected.read_text() == "preserved"
    return [
        {
            "target_constructor_contained": True,
            "restricted_system_read_grants": restricted_reads,
        }
    ]


@requires(SANDBOX, NATIVE_FIXTURES)
def test_contains_target_loader_before_main(binary: Path) -> Transcript:
    return constructor(binary, restricted_reads=False)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_restricted_read_aliases_preserve_loader_and_containment(
    binary: Path,
) -> Transcript:
    return constructor(binary, restricted_reads=True)
