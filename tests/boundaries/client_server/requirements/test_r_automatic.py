#!/usr/bin/env -S uv run --script

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    code,
    r_test_environment,
    run_this_suite,
    stop_client,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"ir"}


def recording_ir_environment(
    directory: Path,
    *,
    fail_requirement: str | None = None,
) -> tuple[dict[str, str], Path]:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    real_ir = shutil.which("ir")
    assert real_ir is not None, "real `ir` is required"
    fake_bin = directory / "bin"
    fake_bin.mkdir()
    fixture = Path(__file__).resolve().parents[3] / "fixtures" / "record_ir"
    (fake_bin / "ir").symlink_to(fixture)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    record = directory / "ir.jsonl"
    environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
    environment["MCP_CONSOLE_TEST_IR_RECORD"] = str(record)
    if fail_requirement is not None:
        environment["MCP_CONSOLE_TEST_IR_FAIL_REQUIREMENT"] = fail_requirement
    return environment, record


def ir_run_records(record: Path) -> list[dict[str, object]]:
    if not record.exists():
        return []
    records = [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]
    return [entry for entry in records if entry["arguments"][0] == "run"]


def ir_requirements(record: dict[str, object]) -> list[str]:
    arguments = record["arguments"]
    assert isinstance(arguments, list), arguments
    return [
        arguments[index + 1]
        for index, argument in enumerate(arguments[:-1])
        if argument == "--with"
    ]


def fixture_r_libraries(
    environment: dict[str, str],
    directory: Path,
    packages: tuple[str, ...],
) -> tuple[Path, Path]:
    source_root = directory / "r-package-sources"
    libraries = (
        directory / "r-package-library-one",
        directory / "r-package-library-two",
    )
    source_root.mkdir()
    sources: tuple[list[Path], list[Path]] = ([], [])
    for library in libraries:
        library.mkdir()
    for index, package in enumerate(packages):
        source = source_root / package
        (source / "R").mkdir(parents=True)
        (source / "DESCRIPTION").write_text(
            f"""\
Package: {package}
Title: MCP Console Runtime Resolution Fixture
Version: 0.0.0.9000
Authors@R: person("MCP Console", role = c("aut", "cre"), email = "fixture@example.com")
Description: Provides a function for automatic R resolution transcript tests.
License: MIT
Encoding: UTF-8
""",
            encoding="utf-8",
        )
        (source / "NAMESPACE").write_text("export(fixture)\n", encoding="utf-8")
        (source / "R" / "fixture.R").write_text(
            "fixture <- function() TRUE\n", encoding="utf-8"
        )
        sources[index % len(sources)].append(source)

    r_home = Path(environment["R_HOME"])
    for library, library_sources in zip(libraries, sources, strict=True):
        if not library_sources:
            continue
        subprocess.run(
            [
                r_home / "bin/R",
                "CMD",
                "INSTALL",
                "--use-vanilla",
                f"--library={library}",
                "--no-docs",
                "--no-help",
                "--no-byte-compile",
                "--no-test-load",
                *library_sources,
            ],
            check=True,
            capture_output=True,
            env=environment,
        )
    return libraries


def recording_fixture_r_environment(
    directory: Path,
    packages: tuple[str, ...],
) -> tuple[dict[str, str], Path]:
    environment, record = recording_ir_environment(directory)
    isolated_library = directory / "r-library"
    environment["R_LIBS_SITE"] = str(isolated_library)
    environment["R_LIBS_USER"] = str(isolated_library)
    source_libraries = fixture_r_libraries(environment, directory, packages)
    environment["MCP_CONSOLE_TEST_IR_SOURCE_LIBRARIES"] = os.pathsep.join(
        map(str, source_libraries)
    )
    environment["MCP_CONSOLE_TEST_IR_LIBRARY_ROOT"] = str(
        directory / "resolved-r-libraries"
    )
    environment["MCP_CONSOLE_TEST_IR_FIXTURE_PACKAGES"] = ",".join(packages)
    environment["MCP_CONSOLE_TEST_IR_BASE_LIBRARY"] = str(directory / "base-r-library")
    return environment, record


def send_and_collect_runtime_r_resolution(
    client: McpClient,
    expected: str,
    **arguments: object,
) -> None:
    call_start = len(client.transcript)
    client.send(**arguments)
    chunks = []
    for attempt in range(5):
        output = last_tool_text(client)
        # A timeout can drain final R output before the completion event,
        # so retain every output delta instead of only the last poll.
        if output.endswith("\n[running; poll with an empty send]"):
            chunks.append(output.removesuffix("\n[running; poll with an empty send]"))
            if attempt == 4:
                raise AssertionError(
                    "automatic R resolution remained running after five responses: "
                    f"collected={''.join(chunks)!r}, last={output!r}"
                )
            client.send()
            continue

        if output != "[done]" or not chunks:
            chunks.append(output)
        collected = "".join(chunks)
        assert collected == expected, repr(collected)

        calls = client.transcript[call_start:]
        submitted = calls[0]
        final_result = calls[-1]["result"]
        assert isinstance(final_result, dict), final_result
        content = final_result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        content[0]["text"] = collected
        submitted["result"] = final_result
        client.transcript[call_start:] = [submitted]
        return


def test_resolves_missing_r_packages_during_evaluation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        packages = ("mcpfirst", "mcpsecond")
        environment, _ = recording_fixture_r_environment(directory, packages)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()

        client.send(python="python_sentinel = 40")
        output = last_tool_text(client)
        assert output == "[done]", repr(output)
        client.send(sql="CREATE TABLE automatic_r_state AS SELECT 42 AS answer")

        # fmt: r
        setup = code(r"""
            sentinel <- 42L
            worker_pid <- Sys.getpid()
            """)
        client.send(r=setup)
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            stopifnot(is.function(mcpfirst::fixture))
            suppressPackageStartupMessages(library(mcpsecond))
            stopifnot(
              identical(sentinel, 42L),
              identical(Sys.getpid(), worker_pid),
              suppressPackageStartupMessages(require(mcpfirst, quietly = TRUE)),
              requireNamespace("mcpsecond", quietly = TRUE)
            )
            connection <- suppressWarnings(file("/dev/stdin"))
            on.exit(close(connection))
            input <- readLines(connection, n = 1L)
            cat("answer: ", input, "\n", sep = "")
            """)
        client.send(r=r, stdin="42\n")
        output = last_tool_text(client)
        assert output == "answer: 42\n", repr(output)

        client.send(python="python_sentinel + 2")
        assert last_tool_text(client) == "42\n"
        client.send(sql="SELECT answer FROM automatic_r_state")
        assert last_tool_text(client).splitlines()[-1].split() == ["1", "42"]
        return client._finish()


def test_does_not_resolve_missing_r_packages_from_sql_callbacks(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcpsqlcallback"
        environment, record = recording_fixture_r_environment(directory, (package,))
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()

        # fmt: r
        r = code(r"""
            sql_requires_package <- function() {
              as.integer(suppressWarnings(requireNamespace(
                "mcpsqlcallback",
                quietly = TRUE
              )))
            }
            invisible()
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        # fmt: python
        python = code("""
            import sqlite3

            connection = sqlite3.connect(":memory:")
            connection.create_function("sql_requires_package", 0, r.sql_requires_package)
            console_sql_connection(connection)
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[done]"
        baseline = len(ir_run_records(record))

        client.send(sql="SELECT sql_requires_package() AS resolved")
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["0"], preview
        assert len(ir_run_records(record)) == baseline

        # Automatic resolution resumes outside the SQL evaluation.
        # fmt: r
        r = code(r"""
            stopifnot(
              requireNamespace("mcpsqlcallback", quietly = TRUE),
              is.function(mcpsqlcallback::fixture)
            )
            42L
            """)
        send_and_collect_runtime_r_resolution(client, "[1] 42\n", r=r)
        runs = ir_run_records(record)[baseline:]
        assert len(runs) == 1, runs
        assert package in ir_requirements(runs[0]), runs
        return client._finish()


def test_resolves_reached_r_packages_at_runtime(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        packages = (
            "mcplibrary",
            "mcpcharacter",
            "mcprequire",
            "mcpnamespace",
            "mcpload",
            "mcpinternal",
            "mcpdynamic",
            "mcpdynamicns",
        )
        environment, record = recording_fixture_r_environment(directory, packages)
        environment["PKG_SUBPROCESS_TIMEOUT"] = "0"
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = len(ir_run_records(record))

        # fmt: r
        static = code(r"""
            invisible(suppressWarnings(suppressPackageStartupMessages({
              base::library(package = mcplibrary, quietly = TRUE)
              library(
                "mcpcharacter",
                help = stats,
                character.only = TRUE,
                quietly = TRUE
              )
              stopifnot(
                base::require(mcprequire, quietly = TRUE),
                base::requireNamespace("mcpnamespace", quietly = TRUE),
                is.environment(base::loadNamespace("mcpload")),
                is.environment(mcpinternal:::.__NAMESPACE__.),
                is.function(mcplibrary::fixture),
                is.function("mcplibrary"::fixture),
                is.function(mcplibrary::fixture)
              )
            })))
            42L
            """)
        send_and_collect_runtime_r_resolution(client, "[1] 42\n", r=static)
        static_runs = ir_run_records(record)[baseline:]
        static_packages = packages[:6]
        assert len(static_runs) == len(static_packages), static_runs
        for index, (run, package) in enumerate(
            zip(static_runs, static_packages, strict=True)
        ):
            requirements = ir_requirements(run)
            assert requirements.count(package) == 1, requirements
            for retained in static_packages[: index + 1]:
                assert requirements.count(retained) == 1, requirements
            assert all(
                later not in requirements for later in static_packages[index + 1 :]
            )
            assert run["no_local_sources"] == "1", run
            assert run["subprocess_timeout"] == "60000", run

        dynamic_baseline = len(ir_run_records(record))
        # fmt: r
        dynamic = code(r"""
            invisible(suppressWarnings(suppressPackageStartupMessages({
              attached <- "mcpdynamic"
              stopifnot(do.call(
                base::library,
                list(
                  package = attached,
                  help = NULL,
                  character.only = TRUE,
                  logical.return = TRUE,
                  quietly = TRUE
                )
              ))
              package <- "mcpdynamicns"
              stopifnot(do.call(
                base::requireNamespace,
                list(package = package, quietly = TRUE)
              ))
            })))
            42L
            """)
        send_and_collect_runtime_r_resolution(client, "[1] 42\n", r=dynamic)
        dynamic_runs = ir_run_records(record)[dynamic_baseline:]
        assert len(dynamic_runs) == 2, dynamic_runs
        assert "mcpdynamic" in ir_requirements(dynamic_runs[0]), dynamic_runs
        assert "mcpdynamicns" in ir_requirements(dynamic_runs[1]), dynamic_runs
        return client._finish()


def test_retains_automatic_r_package_after_error_and_restart(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            stopifnot(is.function(fortunes::fortune))
            stop("after activation")
            """)
        client.send(r=r)
        assert "Error: after activation" in last_tool_text(client)
        assert len(ir_run_records(record)) == baseline + 1

        client.send(r='stopifnot(requireNamespace("fortunes", quietly = TRUE)); 42L')
        assert last_tool_text(client) == "[1] 42\n"
        assert len(ir_run_records(record)) == baseline + 1

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(r='stopifnot(requireNamespace("fortunes", quietly = TRUE)); 42L')
        assert last_tool_text(client) == "[1] 42\n"
        assert len(ir_run_records(record)) == baseline + 1
        return client._finish()


def test_does_not_resolve_unreached_package_loads(binary: Path) -> Transcript:
    missing = "mcpconsolenotarealpackage"
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(
            Path(temporary),
            fail_requirement=missing,
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = len(ir_run_records(record))

        client.send(r=f"if (FALSE) library({missing}); 42L")
        assert last_tool_text(client) == "[1] 42\n"
        assert len(ir_run_records(record)) == baseline

        client.send(r=f"library({missing})")
        assert f"synthetic `ir` failure for {missing}" in last_tool_text(client)
        failed = len(ir_run_records(record))
        assert failed == baseline + 1

        client.send(r="42L")
        assert last_tool_text(client) == "[1] 42\n"
        client.send(r=f"library({missing})")
        assert f"synthetic `ir` failure for {missing}" in last_tool_text(client)
        assert len(ir_run_records(record)) == failed + 1
        return client._finish()


def test_rejects_non_package_runtime_names_before_ir(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            invalid <- c(
              paste0("package", intToUtf8(10L)),
              "github::owner/repo",
              "https://example.com/package",
              "../local/package",
              "package@version",
              "package name"
            )
            available <- vapply(
              invalid,
              requireNamespace,
              logical(1L),
              quietly = TRUE
            )
            host_response <- .Call("mcp_console_resolve_r", invalid)
            stopifnot(
              !any(available),
              identical(host_response[[1L]], "failed"),
              identical(host_response[[2L]], "host")
            )
            42L
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] 42\n", repr(output)
        assert len(ir_run_records(record)) == baseline
        return client._finish()


def test_preserves_base_r_loading_semantics_without_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            ambient_package <- find.package("codetools")
            hidden_library <- file.path(tempdir(), "libpath-library")
            dir.create(hidden_library)
            stopifnot(file.copy(
              ambient_package,
              hidden_library,
              recursive = TRUE
            ))
            attributed_package <- structure(
              "codetools",
              LibPath = hidden_library
            )
            namespace <- loadNamespace(attributed_package)
            if (getRversion() < "4.6.0") {
              stopifnot(identical(
                normalizePath(getNamespaceInfo(namespace, "path")),
                normalizePath(file.path(hidden_library, "codetools"))
              ))
            }
            unloadNamespace("codetools")

            listing <- library()
            help_info <- library(help = base)
            restricted <- suppressWarnings(library(
              "fortunes",
              lib.loc = .Library,
              character.only = TRUE,
              logical.return = TRUE,
              quietly = TRUE
            ))
            partial_failed <- inherits(
              try(
                loadNamespace("fortunes", lib.loc = .Library, partial = TRUE),
                silent = TRUE
              ),
              "try-error"
            )
            package <- "methods"
            library(package, character.only = TRUE, quietly = TRUE)
            invalid <- require(
              "mcpconsole-invalid",
              character.only = TRUE,
              quietly = TRUE
            )
            stopifnot(
              inherits(listing, "libraryIQR"),
              inherits(help_info, "packageInfo"),
              identical(restricted, FALSE),
              partial_failed,
              identical(invalid, FALSE),
              identical(jsonlite::fromJSON("42"), 42L)
            )
            42L
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] 42\n", repr(output)

        # fmt: r
        r = code(r"""
            stopifnot(!"package:splines" %in% search())
            forwarded <- function(x) {
              base::library(
                splines,
                attach.required = x,
                quietly = TRUE
              )
            }
            error <- try(forwarded(), silent = TRUE)
            stopifnot(
              inherits(error, "try-error"),
              identical(
                conditionMessage(attr(error, "condition")),
                'argument "x" is missing, with no default'
              )
            )
            42L
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] 42\n", repr(output)

        # fmt: r
        r = code(r"""
            stopifnot(!isNamespaceLoaded("stats4"))
            forwarded <- function(x) {
              base::loadNamespace("stats4", keep.source = x)
            }
            messages <- capture.output(
              error <- try(forwarded(), silent = TRUE),
              type = "message"
            )
            stopifnot(
              inherits(error, "try-error"),
              any(grepl(
                'argument "x" is missing, with no default',
                messages,
                fixed = TRUE
              ))
            )
            42L
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] 42\n", repr(output)
        assert len(ir_run_records(record)) == baseline
        return client._finish()


def test_loads_package_with_devtools(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        fixture = Path(__file__).resolve().parents[3] / "fixtures" / "load_all"
        package = directory / "package"
        shutil.copytree(fixture, package)
        environment, record = recording_ir_environment(directory)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=package,
        )
        client._initialize_and_list_tools()
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            stopifnot(
              !isNamespaceLoaded("devtools"),
              !isNamespaceLoaded("pkgload"),
              !isNamespaceLoaded("mcpconsoleloadall")
            )
            devtools::load_all(
              reset = TRUE,
              recompile = FALSE,
              export_all = FALSE,
              helpers = FALSE,
              quiet = TRUE
            )
            stopifnot(
              !"internal_value" %in%
                getNamespaceExports("mcpconsoleloadall")
            )
            list(
              exported = mcpconsoleloadall::exported_value(),
              internal = mcpconsoleloadall:::internal_value()
            )
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "$exported\n[1] 42\n\n$internal\n[1] 41\n\n", repr(output)
        assert len(ir_run_records(record)) == baseline
        return client._finish()


def test_r_activation_failure_requires_restart_without_stopping_worker(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.send(r="activation_state <- 41L; activation_pid <- Sys.getpid()")
        assert last_tool_text(client) == "[done]"
        baseline = len(ir_run_records(record))

        # The private bridge deliberately uses the live base::.libPaths binding
        # shared with explicit preparation.
        # fmt: r
        r = code(r"""
            local({
              invisible(suppressMessages(base::trace(
                ".libPaths",
                tracer = quote(if (!missing(new)) {
                  stop("synthetic managed R activation failure")
                }),
                print = FALSE,
                where = base::baseenv()
              )))
              on.exit(invisible(suppressMessages(base::untrace(
                ".libPaths",
                where = base::baseenv()
              ))))
              package <- "fortunes"
              do.call(
                base::loadNamespace,
                list(package = package)
              )
            })
            """)
        client.send(r=r)
        assert "synthetic managed R activation failure" in last_tool_text(client)
        assert len(ir_run_records(record)) == baseline + 1

        client.send(
            r=("activation_state + as.integer(identical(Sys.getpid(), activation_pid))")
        )
        assert last_tool_text(client) == "[1] 42\n"
        client.send(requirements={"r": ["english"]})
        assert last_tool_text(client) == "[restart required]"

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(
            r=(
                "package <- 'fortunes'; "
                "stopifnot(do.call(base::requireNamespace, "
                "list(package = package, quietly = TRUE))); 42L"
            )
        )
        assert last_tool_text(client) == "[1] 42\n"
        assert len(ir_run_records(record)) == baseline + 2
        return client._finish()


def test_restart_discards_unactivated_r_candidate(binary: Path) -> Transcript:
    checkpoint_name = "automatic-r-activation-before-report"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcprestart"
        environment, record = recording_fixture_r_environment(directory, (package,))
        environment["TMPDIR"] = temporary
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="invisible(NULL)")
            assert last_tool_text(client) == "[done]"
            baseline = len(ir_run_records(record))

            # The live .libPaths() binding is reached after RResolved and
            # immediately before the bridge applies and reports the candidate.
            # Restart closes fd 0 after rotating the lifecycle generation, so
            # EOF releases the old worker only after its candidate is stale.
            # fmt: r
            r = code(f"""
                activation_checkpoint <- base::file.path(
                  base::tempdir(),
                  "{checkpoint_name}"
                )
                activation_gate_used <- FALSE
                invisible(suppressMessages(base::trace(
                  ".libPaths",
                  tracer = quote({{
                    if (
                      !missing(new) &&
                        base::length(new) > 0L &&
                        !activation_gate_used
                    ) {{
                      activation_gate_used <<- TRUE
                      base::stopifnot(base::file.create(activation_checkpoint))
                      gate <- base::suppressWarnings(base::file(
                        "/dev/stdin",
                        open = "rb"
                      ))
                      base::on.exit(base::close(gate), add = TRUE)
                      base::stopifnot(base::length(base::readBin(
                        gate,
                        what = "raw",
                        n = 1L
                      )) == 0L)
                    }}
                  }}),
                  print = FALSE,
                  where = base::baseenv()
                )))
                package <- "{package}"
                invisible(base::do.call(
                  base::loadNamespace,
                  list(package = package)
                ))
                """)
            evaluation = client._start_send(r=r, timeout_ms=0)
            wait_for_worker_file(directory, checkpoint_name, client)
            assert len(ir_run_records(record)) == baseline + 1

            restart = client._start_send(control="restart")
            client._receive_many([evaluation, restart])
            assert (
                last_tool_text_from_entry(evaluation)
                == "\n[running; poll with an empty send]"
            )
            assert last_tool_text_from_entry(restart) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )
            assert len(ir_run_records(record)) == baseline + 1

            client.send(
                r=(
                    f"package <- '{package}'; "
                    "invisible(base::do.call(base::loadNamespace, "
                    "list(package = package))); 42L"
                )
            )
            output = last_tool_text(client)
            assert output == "[1] 42\n", repr(output)
            assert len(ir_run_records(record)) == baseline + 2
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_rejects_preparation_while_automatic_r_resolver_is_running(
    binary: Path,
) -> Transcript:
    package = "RcppRoll"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_ir_environment(directory)
        started = FifoCheckpoint(directory / "ir-started")
        release = FifoCheckpoint(directory / "ir-release")
        environment["MCP_CONSOLE_TEST_IR_BLOCK_REQUIREMENT"] = package
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(release.path)
        client = McpClient(binary, ("serve",), environment)
        resolver_released = False
        finished = False
        try:
            client._initialize_and_list_tools()
            baseline = len(ir_run_records(record))

            evaluation = client._start_send(
                r=(f'invisible(base::loadNamespace("{package}")); 42L'),
                timeout_ms=0,
            )
            started.wait("automatic R resolver")
            client._receive(evaluation)
            assert (
                last_tool_text_from_entry(evaluation)
                == "\n[running; poll with an empty send]"
            )
            preparation = client._start_send(
                requirements={"r": ["english"]},
            )
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert readable, "preparation waited for the active R resolver"
            client._receive(preparation)
            assert preparation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "worker is already evaluating a cell; poll it before "
                            "preparing requirements"
                        ),
                    }
                ],
                "isError": True,
            }, preparation

            poll = client._start_send()
            release.release()
            resolver_released = True
            client._receive(poll)
            assert last_tool_text_from_entry(poll) == "[1] 42\n"
            evaluation["result"] = poll["result"]
            assert client.transcript.pop() is poll
            assert len(ir_run_records(record)) == baseline + 1
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not resolver_released:
                release.release()
            started.close()
            release.close()
            if not finished:
                stop_client(client)


def test_interrupts_automatic_r_resolver_and_preserves_worker(
    binary: Path,
) -> Transcript:
    package = "RcppRoll"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_ir_environment(directory)
        started = FifoCheckpoint(directory / "ir-started")
        release = FifoCheckpoint(directory / "ir-release")
        environment["MCP_CONSOLE_TEST_IR_BLOCK_REQUIREMENT"] = package
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(release.path)
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(
                r="resolver_interrupt_state <- 41L; resolver_pid <- Sys.getpid()"
            )
            assert last_tool_text(client) == "[done]"
            baseline = len(ir_run_records(record))

            # fmt: r
            r = code(r"""
                package <- "RcppRoll"
                do.call(base::loadNamespace, list(package = package))
                resolver_interrupt_cell_ran <- TRUE
                """)
            evaluation = client._start_send(r=r)
            started.wait("automatic R resolver")
            interrupt = client._start_send(control="interrupt", timeout_ms=0)
            calls_returned = threading.Event()
            forced_release = threading.Event()

            def release_if_calls_block() -> None:
                if not calls_returned.wait(2):
                    forced_release.set()
                    release.release()

            watchdog = threading.Thread(target=release_if_calls_block)
            watchdog.start()
            client._receive_many([evaluation, interrupt])
            calls_returned.set()
            watchdog.join()
            assert not forced_release.is_set(), "interrupt did not stop the R resolver"
            assert last_tool_text_from_entry(interrupt) == "\n[idle]"
            error = last_tool_text_from_entry(evaluation)
            assert error == "Error: R package resolution interrupted\n", repr(error)
            assert len(ir_run_records(record)) == baseline + 1

            client.send(
                r=(
                    "resolver_interrupt_state + "
                    "as.integer(!exists('resolver_interrupt_cell_ran')) + "
                    "as.integer(identical(Sys.getpid(), resolver_pid)) - 1L"
                )
            )
            assert last_tool_text(client) == "[1] 42\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            release.release()
            started.close()
            release.close()
            if not passed:
                stop_client(client)


def last_tool_text(client: McpClient) -> str:
    return last_tool_text_from_entry(client.transcript[-1])


def last_tool_text_from_entry(entry: dict[str, object]) -> str:
    result = entry["result"]
    assert isinstance(result, dict), result
    content = result["content"]
    assert len(content) == 1, content
    assert content[0]["type"] == "text", content
    return content[0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
