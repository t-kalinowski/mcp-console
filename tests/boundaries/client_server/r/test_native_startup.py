"""R owns startup discovery, ordering, and retry inside each worker."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import wait_for_evaluation_output
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.snapshots import execution_snapshots
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SANDBOX, requires
from support.r import r_test_environment
from support.resolvers import bare_runtime_environment


def startup_environment(root: Path, *, managed: bool = False) -> dict[str, str]:
    environment, rscript = r_test_environment()
    home = root / "home"
    home.mkdir()
    # Isolate native home discovery while retaining the caller's dependency cache.
    cache = subprocess.check_output(
        [
            rscript,
            "--vanilla",
            "-e",
            'cat(dirname(dirname(tools::R_user_dir("ir", "cache"))))',
        ],
        env=environment,
        text=True,
    )
    environment.setdefault("R_USER_CACHE_DIR", cache)
    environment.update(
        HOME=str(home),
        R_USER=str(home),
        R_ENVIRON=os.devnull,
        R_PROFILE=os.devnull,
        MCP_CONSOLE_LANGUAGES="r",
    )
    for name in ("R_ENVIRON_USER", "R_PROFILE_USER", "R_DEFAULT_PACKAGES"):
        environment.pop(name, None)
    if not managed:
        library = root / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
    return environment


def startup_arguments(execution: Execution, *arguments: str) -> tuple[str, ...]:
    return execution.serve("-c", "cache=host", *arguments)


def writable_arguments(execution: Execution, root: Path) -> tuple[str, ...]:
    return startup_arguments(
        execution, *(("--writable-root", str(root)) if execution == SANDBOXED else ())
    )


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_native_order_and_captured_configuration(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        (root / ".Renviron").write_text("CONSOLE_NATIVE_ENV=from-environ\n")
        # fmt: r
        profile = code("""
            stopifnot(identical(Sys.getenv("CONSOLE_NATIVE_ENV"), "from-environ"))
            options(width = 73L, defaultPackages = "utils")
            native_profile <- TRUE
            .First <- function() {
              stopifnot(identical(restored_value, 42L))
              native_first <<- TRUE
            }
            """)
        (root / ".Rprofile").write_text(profile)
        rscript = (
            Path(environment["R_HOME"])
            / "bin"
            / ("Rscript.exe" if os.name == "nt" else "Rscript")
        )
        subprocess.run(
            [
                rscript,
                "--vanilla",
                "-e",
                'restored_value <- 42L; save(restored_value, file = ".RData")',
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
        )
        workspace_contents = (root / ".RData").read_bytes()
        with McpClient(
            binary,
            startup_arguments(execution),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            # fmt: r
            check = code("""
                stopifnot(
                  native_profile,
                  native_first,
                  identical(restored_value, 42L),
                  identical(getOption("width"), 73L),
                  identical(getOption("defaultPackages"), "utils"),
                  "package:utils" %in% search(),
                  !("package:stats" %in% search()),
                  interactive(),
                  "--no-save" %in% commandArgs(),
                  !("--vanilla" %in% commandArgs())
                )
                cat("native startup complete\n")
                """)
            client.expect("native startup complete\n", r=check)
            config = root / ".agents/console/config.yaml"
            config.write_text("r:\n  vanilla: true\n")
            (root / ".Rprofile").write_text(profile + "native_edited <- TRUE\n")
            client.send(control="restart")
            client.expect("native startup complete\n", r=check)
            client.expect("[1] TRUE\n", r="native_edited")
            client.expect(r="restored_value <- 99L")
            records = client.finish()
            assert (root / ".RData").read_bytes() == workspace_contents
            return records


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_vanilla_override_survives_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        (root / ".Renviron").write_text("CONSOLE_NATIVE_ENV=unexpected\n")
        (root / ".Rprofile").write_text('stop("profile must be suppressed")\n')
        rscript = (
            Path(environment["R_HOME"])
            / "bin"
            / ("Rscript.exe" if os.name == "nt" else "Rscript")
        )
        subprocess.run(
            [
                rscript,
                "--vanilla",
                "-e",
                'restored_value <- TRUE; .First <- function() stop("workspace First must be suppressed"); save(restored_value, .First, file = ".RData")',
            ],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
        )
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("r:\n  vanilla: false\n")
        with McpClient(
            binary,
            startup_arguments(execution, "-c", "r.vanilla=true"),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            # fmt: r
            check = code("""
                stopifnot(
                  Sys.getenv("CONSOLE_NATIVE_ENV") == "",
                  "--vanilla" %in% commandArgs(),
                  !exists("native_profile"),
                  !exists("restored_value"),
                  !exists(".First"),
                  "package:stats" %in% search()
                )
                cat("vanilla startup complete\n")
                """)
            client.expect("vanilla startup complete\n", r=check)
            config.write_text("r:\n  vanilla: false\n")
            client.send(control="restart")
            client.expect("vanilla startup complete\n", r=check)
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_exit_requires_explicit_retry(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        profile = root / ".Rprofile"
        marker = root / "attempts"
        profile.write_text(
            'cat("attempt\\n", file = "attempts", append = TRUE)\n'
            'cat("native profile diagnostic before exit\\n")\n'
            'quit(save = "no", status = 23L, runLast = FALSE)\n'
        )
        with McpClient(
            binary,
            writable_arguments(execution, root),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            output = wait_for_evaluation_output(
                client,
                None,
                "failed native startup",
                expected_error=True,
                completion_timeout_seconds=client.response_timeout,
                r='stop("cell must not run")',
            )
            assert "native profile diagnostic before exit" in output, output
            assert "23" in output and "cell must not run" not in output, output
            assert marker.read_text().splitlines() == ["attempt"]
            for arguments in (
                {},
                {"r": 'stop("retry cell must not run")'},
                {"requirements": {"action": "get"}},
                {"requirements": {"action": "set", "r": []}},
            ):
                result = client.send(**arguments)
                assert marker.read_text().splitlines() == ["attempt"], result
                assert "retry cell must not run" not in str(result), result
            profile.write_text(
                'cat("attempt\\n", file = "attempts", append = TRUE)\n'
                "native_repaired <- TRUE\n"
            )
            client.send(control="restart")
            client.expect("[1] TRUE\n", r="native_repaired")
            assert marker.read_text().splitlines() == ["attempt", "attempt"]
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_directory_and_home_discovery(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        home = Path(environment["R_USER"])
        for directory, label in ((home, "home"), (root, "directory")):
            (directory / ".Renviron").write_text(f"CONSOLE_PROFILE_LOCATION={label}\n")
            (directory / ".Rprofile").write_text(
                f'stopifnot(Sys.getenv("CONSOLE_PROFILE_LOCATION") == "{label}")\n'
                f'native_location <- "{label}"\n'
            )
        with McpClient(
            binary,
            startup_arguments(execution, "-c", "r={}"),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect('[1] "directory"\n', r="native_location")
            (root / ".Renviron").unlink()
            (root / ".Rprofile").unlink()
            client.send(control="restart")
            client.expect('[1] "home"\n', r="native_location")
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_overrides_and_native_default_packages(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        environment.update(
            R_ENVIRON=str(root / "site-environ"),
            R_ENVIRON_USER=str(root / "user-environ"),
            R_PROFILE=str(root / "site-profile"),
            R_PROFILE_USER=str(root / "user-profile"),
        )
        (root / "site-environ").write_text("CONSOLE_NATIVE_ORDER=site\n")
        (root / "user-environ").write_text(
            "CONSOLE_NATIVE_ORDER=user\nR_DEFAULT_PACKAGES=utils\n"
        )
        (root / "site-profile").write_text(
            'stopifnot(Sys.getenv("CONSOLE_NATIVE_ORDER") == "user")\n'
            'native_order <- "site"\n'
        )
        (root / "user-profile").write_text(
            'stopifnot(identical(native_order, "site"))\n'
            'native_order <- "user"\n'
            '.First <- function() native_order <<- paste(native_order, "first")\n'
        )
        (root / ".Rprofile").write_text(
            'stop("directory profile must be overridden")\n'
        )
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("r:\n  vanilla: true\n")
        with McpClient(
            binary,
            startup_arguments(execution, "-c", "r.vanilla=false"),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            # fmt: r
            check = code("""
                stopifnot(
                  identical(native_order, "user first"),
                  identical(getOption("defaultPackages"), "utils"),
                  "package:utils" %in% search(),
                  !("package:stats" %in% search())
                )
                cat("native overrides and package selection retained\n")
                """)
            client.expect("native overrides and package selection retained\n", r=check)
            client.send(control="restart")
            client.expect("native overrides and package selection retained\n", r=check)
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_profile_error_follows_r_behavior(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        (root / ".Rprofile").write_text(
            'options(width = 71L)\nstop("native profile error continues in R")\n'
        )
        with McpClient(
            binary,
            startup_arguments(execution),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            output = wait_for_evaluation_output(
                client,
                None,
                "native nonfatal error",
                completion_timeout_seconds=client.response_timeout,
                r='stopifnot(getOption("width") == 71L); cat("R continued initialization\\n")',
            )
            assert "native profile error continues in R" in output, output
            assert output.endswith("R continued initialization\n"), output
            client.expect("[1] 42\n", r="6 * 7")
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_interrupted_profile_is_not_replayed(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        profile = root / ".Rprofile"
        marker = root / "attempts"
        profile.write_text(
            'cat("attempt\\n", file = "attempts", append = TRUE)\n'
            'readline("native startup> ")\n'
        )
        with McpClient(
            binary,
            writable_arguments(execution, root),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                '[input requested: "native startup> "]\n[waiting for stdin]',
                "native startup input",
                completion_timeout_seconds=client.response_timeout,
                r='stop("interrupted startup ran cell")',
                timeout_ms=0,
            )
            output = wait_for_evaluation_output(
                client,
                None,
                "interrupted native startup",
                expected_error=True,
                completion_timeout_seconds=client.response_timeout,
                control="interrupt",
            )
            assert "restart required" in output, output
            assert marker.read_text().splitlines() == ["attempt"]
            client.send(r='stop("interrupted startup retried cell")')
            assert marker.read_text().splitlines() == ["attempt"]
            profile.write_text("native_repaired <- TRUE\n")
            client.send(control="restart")
            client.expect("[1] TRUE\n", r="native_repaired")
            return client.finish()


@requires(R, SANDBOX)
def test_startup_containment_and_preparation_isolation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        environment = startup_environment(workspace, managed=True)
        environment["IR_REFRESH"] = "1"
        environment["CONSOLE_FORBIDDEN_STARTUP_WRITE"] = str(root / "forbidden")
        # fmt: r
        profile = code("""
            cat("attempt\n", file = "attempts", append = TRUE)
            blocked <- tryCatch(
              {
                suppressWarnings(writeLines(
                  "forbidden",
                  Sys.getenv("CONSOLE_FORBIDDEN_STARTUP_WRITE")
                ))
                FALSE
              },
              error = function(error) TRUE
            )
            stopifnot(blocked)
            cat("native profile contained\n")
            """)
        (workspace / ".Rprofile").write_text(profile)
        with McpClient(
            binary,
            writable_arguments(SANDBOXED, workspace),
            environment,
            current_directory=workspace,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("native profile contained\n", r="invisible(NULL)")
            assert (workspace / "attempts").read_text().splitlines() == ["attempt"]
            assert not (root / "forbidden").exists()
            output = wait_for_evaluation_output(
                client,
                None,
                "explicit restart and host preparation",
                completion_timeout_seconds=client.response_timeout,
                control="restart",
                requirements={"action": "set", "r": ["MASS"]},
                r="invisible(NULL)",
            )
            assert "native profile contained" in output, output
            assert (workspace / "attempts").read_text().splitlines() == [
                "attempt",
                "attempt",
            ]
            assert not (root / "forbidden").exists()
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_yaml_vanilla_suppresses_native_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = startup_environment(root)
        (root / ".Rprofile").write_text('stop("YAML vanilla must suppress startup")\n')
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("r:\n  vanilla: true\n")
        with McpClient(
            binary,
            startup_arguments(execution),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("[1] TRUE\n", r='"--vanilla" %in% commandArgs()')
            return client.finish()
