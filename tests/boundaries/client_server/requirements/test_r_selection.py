"""Installed R selection survives configuration layering and worker replacement."""

import json
import shlex
import sys
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import isolated_r_home, r_test_environment
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import recording_ir_environment, ir_run_records
from support.suites import run_this_suite


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_installed_r_selection_and_layering(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    # The home-relative path uses the real HOME, preserving the caller's tool environment.
    with TemporaryDirectory(dir=Path.home()) as temporary:
        root = Path(temporary).resolve()
        environment, record = recording_ir_environment(root)
        original = Path(environment["R_HOME"])
        installed = isolated_r_home(root, environment)
        (root / "chosen R").symlink_to(installed / "bin/R")
        environment.update(
            R_HOME="stale inherited home",
            RHOME="stale inherited home",
            RETICULATE_PYTHON=sys.executable,
            R_SHARE_DIR="stale share",
            R_INCLUDE_DIR="stale include",
            R_DOC_DIR="stale doc",
        )
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        variants = [
            ("chosen R", ("r.vanilla=true",), True),
            ({"vanilla": True}, ("r=./chosen R",), True),
            ({"vanilla": True}, ("r=null", "r=./chosen R"), False),
            (
                {
                    "executable": "~/"
                    + str((root / "chosen R").relative_to(Path.home()))
                },
                (),
                False,
            ),
            ({"executable": str(original / "bin/exec/R")}, (), False),
        ]
        for selection, overrides, vanilla in variants:
            record.write_text("")
            config.write_text(json.dumps({"r": selection}))
            with McpClient(
                binary,
                execution.serve(
                    "-c",
                    "cache=host",
                    *(item for override in overrides for item in ("-c", override)),
                ),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                expected_home = (
                    original
                    if isinstance(selection, dict)
                    and selection.get("executable", "").endswith("bin/exec/R")
                    else installed
                )
                program = (
                    # fmt: r
                    code(r"""
                        stopifnot(
                          identical(normalizePath(R.home()), normalizePath(EXPECTED_HOME)),
                          identical(Sys.getenv("RHOME"), R.home()),
                          identical("--vanilla" %in% commandArgs(), VANILLA)
                        )
                        Sys.setenv(R_HOME = "changed", RHOME = "changed")
                        cat("selected installation\n")
                        """)
                    .replace("EXPECTED_HOME", json.dumps(str(expected_home)))
                    .replace("VANILLA", "TRUE" if vanilla else "FALSE")
                )
                client.expect("selected installation\n", r=program)
                runs = ir_run_records(record)
                assert runs and all(
                    run["arguments"][run["arguments"].index("--rscript") + 1]
                    == str(expected_home / "bin/Rscript")
                    for run in runs
                ), runs
                client.send(control="restart")
                client.expect("selected installation\n", r=program)
                assert ir_run_records(record) == runs
                client.finish()
            records.append(
                {
                    "selection": selection
                    if isinstance(selection, str)
                    else list(selection),
                    "vanilla": vanilla,
                    "matching_installation_after_restart": True,
                }
            )
        return records


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_missing_selected_r_can_be_repaired(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"r": "./chosen R"}))
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, root
        ) as client:
            client.initialize_and_list_tools()
            failed = client.send(r='cat("must not run\\n")')
            assert failed.get("isError") and "r.executable" in str(failed), failed
            (root / "chosen R").symlink_to(rscript.with_name("R"))
            # Retry retains the captured path even if the project file is edited.
            config.write_text(json.dumps({"r": "./another R"}))
            client.send(control="restart")
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            client.finish()
            return json.loads(
                json.dumps(
                    [{"initial_error": failed, "repaired_captured_selection": True}]
                ).replace(str(root), "<workspace>")
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_changed_selected_launcher_is_rejected(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        selected = root / "chosen R"
        selected.write_text(
            "#!/bin/sh\nexec " + shlex.quote(str(rscript.with_name("R"))) + ' "$@"\n'
        )
        selected.chmod(0o755)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.executable=./chosen R"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            selected.write_text(selected.read_text() + "# changed identity\n")
            failed = client.send(control="restart", r='cat("must not run\\n")')
            assert failed.get("isError") and "selected R installation changed" in str(
                failed
            ), failed
            client.finish()
            return json.loads(
                json.dumps([{"restart_error": failed}]).replace(
                    str(root), "<workspace>"
                )
            )


@requires(POSIX, R)
@executions(SANDBOXED)
def test_r_launcher_inspection_uses_worker_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        selected = root / "chosen R"
        marker = root / "resolver-only" / "probe"
        marker.parent.mkdir()
        selected.write_text(
            "#!/bin/sh\nif ! (printf probe > "
            + shlex.quote(str(marker))
            + ") 2>/dev/null; then\n  echo 'probe cannot write resolver-only directory' >&2\n  exit 1\nfi\nexec "
            + shlex.quote(str(rscript.with_name("R")))
            + ' "$@"\n'
        )
        selected.chmod(0o755)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "r": str(selected),
                    "resolver": {
                        "sandbox": {"filesystem": {"read_write": [str(marker.parent)]}}
                    },
                }
            )
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, root
        ) as client:
            client.initialize_and_list_tools()
            failed = client.send(r='cat("must not run\\n")')
            assert failed.get("isError") and "R installation inspection failed" in str(
                failed
            ), failed
            assert not marker.exists(), "inspection used resolver permissions"
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            assert "R installation inspection failed" in stderr, stderr
            return json.loads(
                json.dumps(
                    [
                        {
                            "inspection_error": failed,
                            "stderr": stderr,
                            "resolver_grant_did_not_reach_inspection": True,
                        }
                    ]
                ).replace(str(root), "<workspace>")
            )


@requires(POSIX, R)
@executions(DIRECT)
def test_r_inspection_uses_worker_temporary_storage(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        selected = root / "chosen R"
        selected.write_text(
            '#!/bin/sh\nset -e\nscratch=$(mktemp -d "$TMPDIR/mcp-console-probe.XXXXXX")\nrmdir "$scratch"\nexec '
            + shlex.quote(str(rscript.with_name("R")))
            + ' "$@"\n'
        )
        selected.chmod(0o755)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                "-c",
                "r.executable=./chosen R",
                "-c",
                "environment.TMPDIR="
                + str(root / "invalid configured temporary directory"),
                "-c",
                "resolver.environment.TMPDIR=" + gettempdir(),
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", r="cat(42, '\\n', sep='')")
            client.finish()
            return [{"R_inspection_uses_worker_temporary_storage": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
