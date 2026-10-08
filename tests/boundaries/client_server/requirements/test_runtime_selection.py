"""Installed R selection, path expansion, and input layering contracts."""

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_configuration import configure
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment, isolated_r_home
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import recording_ir_environment, ir_run_records
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_r_shorthand_layering_and_path_expansion(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    variants = [
        ("./chosen R", ("r.resolution=disabled",), False),
        (
            {"resolution": "disabled", "vanilla": True, "packages": []},
            ("r=./chosen R",),
            True,
        ),
        (
            {"resolution": "disabled", "vanilla": True},
            ("r=null", "r=./chosen R", "r.resolution=disabled"),
            False,
        ),
        ({"resolution": "disabled"}, ("r.executable=~/chosen R",), False),
        ({"executable": "./chosen R", "resolution": "disabled"}, (), False),
    ]
    for document, overrides, vanilla in variants:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            selected = shutil.which("R")
            assert selected is not None
            (root / "chosen R").symlink_to(selected)
            env, _ = r_test_environment()
            env.update(
                HOME=str(root),
                RETICULATE_PYTHON=sys.executable,
                R_HOME=str(root / "stale home"),
            )
            configure(root, {"r": document, "languages": ["r"]})
            arguments = execution.serve(
                "-c",
                "cache=host",
                *(item for override in overrides for item in ("-c", override)),
            )
            with McpClient(binary, arguments, env, root) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "layered R selected\n",
                    r=f'stopifnot(identical("--vanilla" %in% commandArgs(), {"TRUE" if vanilla else "FALSE"})); cat("layered R selected\\n")',
                )
                inspection = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]
                assert (
                    inspection["resolution"]["r"] == "disabled"
                    and inspection["requirements"]["r"] == []
                ), inspection
                records.append(
                    {
                        "r": document,
                        "overrides": list(overrides),
                        "vanilla": vanilla,
                        "resolution": inspection["resolution"]["r"],
                        "requirements": inspection["requirements"]["r"],
                    }
                )
                client.finish()
    return records


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_r_preparation_detects_content_changes_with_retained_metadata(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, record = recording_ir_environment(root)
        original = Path(environment["R_HOME"]) / "bin/Rscript"
        installed = isolated_r_home(root, environment)
        rscript = installed / "bin/Rscript"
        rscript.unlink()
        source = (
            "#!/bin/sh\nexec "
            + shlex.quote(str(original))
            + ' "$@"\n'
            + "#" * 1024
            + "\n"
        )
        rscript.write_text(source)
        rscript.chmod(0o755)
        marker = root / "modified-rscript-ran"
        configure(
            root,
            {
                "r": {
                    "executable": str(installed / "bin/R"),
                    "resolution": "explicit",
                    "packages": [],
                },
                "python": sys.executable,
                **(
                    {
                        "sandbox": {
                            "filesystem": {
                                "read_only": [str(installed)],
                                "read_write": [str(root)],
                            }
                        }
                    }
                    if execution == SANDBOXED
                    else {}
                ),
            },
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, root
        ) as client:
            client.initialize_and_list_tools()
            client.expect("selected R ready\n", r='cat("selected R ready\\n")')
            if execution == SANDBOXED:
                # fmt: python
                program = code("""
                    from pathlib import Path

                    for mutate in (
                        lambda: Path("R/bin/Rscript").open("r+b"),
                        lambda: Path("R/bin").rename("changed-bin"),
                        lambda: Path("R").rename("changed-R"),
                    ):
                        try:
                            mutate()
                        except OSError:
                            pass
                        else:
                            raise AssertionError("worker changed the selected installation")
                    assert Path("R/bin/Rscript").is_file()
                    print("selected R protected")
                    """)
                client.expect("selected R protected\n", python=program)
            before = rscript.stat()
            changed = (
                "#!/bin/sh\nprintf changed > "
                + shlex.quote(str(marker))
                + "\nexit 91\n"
            ).encode()
            rscript.write_bytes(changed.ljust(len(source), b"#"))
            os.utime(rscript, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = rscript.stat()
            assert (
                before.st_size,
                before.st_mtime_ns,
                before.st_ino,
                before.st_mode,
            ) == (after.st_size, after.st_mtime_ns, after.st_ino, after.st_mode)
            runs = ir_run_records(record)
            result = client.send(requirements={"r": ["DBI"]})
            assert (
                result.get("isError")
                and "selected R installation changed" in result["content"][0]["text"]
            ), result
            assert ir_run_records(record) == runs and not marker.exists()
            client.finish()
    return [
        {
            "retained_r_identity": "content changes rejected despite matching file metadata"
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
