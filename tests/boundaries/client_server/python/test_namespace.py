"""Console exports public conveniences without implementation workspace names."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import wait_for_evaluation_output
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.installation import installed_console
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import POSIX, R, SQL, command, requires
from support.suites import run_this_suite


# Inline imports and comprehension variables do not create diagnostic bindings.
# fmt: python
CLEAN_NAMESPACE = code("""
    assert globals() is __import__("__main__").__dict__
    assert __name__ == "__main__"
    assert globals().keys() <= {
        "__name__", "__doc__", "__package__", "__loader__", "__spec__",
        "__builtins__", "__annotations__", "r",
    }
    assert not {"os", "sys", "tempfile"} & globals().keys()
    assert not {"os", "sys", "tempfile"} & vars(__import__("builtins")).keys()
    assert not any(name.startswith("_mcp_console") for name in vars(__import__("builtins")))
    assert callable(_console.sql_connection)
    assert hasattr(__import__("builtins"), "r")
    print("clean Python namespace")
    """)


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_python_only_namespace_and_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary)
        retain_system_bwrap(path)
        # Keep the complete globals transcript on Python 3.13's standard metadata.
        subprocess.run(
            ["uv", "python", "install", "--no-bin", "--no-registry", "3.13"],
            check=True,
        )
        python = subprocess.check_output(
            ["uv", "python", "find", "3.13"], text=True
        ).strip()
        environment = dict(os.environ, PATH=str(path))
        for name in ("R_HOME", "RHOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_UV"):
            environment.pop(name, None)
        with McpClient(
            binary,
            execution.serve("-c", f"python={json.dumps(python)}"),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "{'__name__': '__main__', '__doc__': None, '__package__': None, "
                "'__loader__': <class '_frozen_importlib.BuiltinImporter'>, '__spec__': None, "
                "'__annotations__': {}, '__builtins__': <module 'builtins' (built-in)>}\n",
                python="print(globals())",
            )
            client.expect(
                # fmt: python
                python=code("""
                    assert globals().keys() == {
                        "__name__",
                        "__doc__",
                        "__package__",
                        "__loader__",
                        "__spec__",
                        "__annotations__",
                        "__builtins__",
                    }
                    """),
            )
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            client.expect(
                "R unavailable\n",
                # fmt: python
                python=code("""
                    try:
                        r.pi
                    except RuntimeError as error:
                        assert str(error) == "R is unavailable in this session"
                    else:
                        raise AssertionError("fixture unexpectedly has R")
                    print("R unavailable")
                    """),
            )
            client.expect(
                # fmt: python
                python=code("""
                    import os, sys, tempfile

                    assert os is __import__("os")
                    assert sys is __import__("sys")
                    assert tempfile is __import__("tempfile")
                    retained = object()
                    """),
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            client.expect(python='assert "retained" not in globals()')
            return client.finish()


def lazy_namespace(
    binary: Path, execution: Execution, first_language: str
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        worker = root / "worker"
        worker.write_text(
            "#!/bin/sh\nexec " + shlex.join([str(binary), "worker"]) + "\n"
        )
        worker.chmod(0o755)
        environment, rscript = r_test_environment()
        # Custom workers skip managed bootstrap. Prepare the real bridge and SQL
        # prerequisites without initializing any interpreter in the worker.
        library = subprocess.check_output(
            [
                "ir",
                "run",
                "--rscript",
                str(rscript),
                "--isolated",
                "--vanilla",
                *(
                    argument
                    for package in (
                        "DBI",
                        "arrow",
                        "duckdb",
                        "jsonlite",
                        "nanoarrow",
                        "pillar",
                        "reticulate",
                        "tibble",
                        "utf8",
                    )
                    for argument in ("--with", package)
                ),
                "-e",
                "cat(normalizePath(.libPaths()[[1L]]))",
            ],
            env=environment,
            text=True,
        ).strip()
        for name in ("R_LIBS", "R_LIBS_SITE", "R_LIBS_USER"):
            environment[name] = library
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(
            binary, execution.serve("--worker", str(worker)), environment
        ) as client:
            client.initialize_and_list_tools()
            if first_language == "r":
                client.expect(r="namespace_r_value <- 42L")
            elif first_language == "sql":
                client.expect(
                    "# A tibble: 1 × 1\n  namespace_value\n          <int32>\n1              42\n",
                    sql="select 42 as namespace_value",
                )
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            client.expect(python="assert int(r['sum(c(20, 22))']) == 42")
            client.expect(r='stopifnot(reticulate::py_eval("42") == 42L)')
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            client.expect(
                # fmt: python
                python=code("""
                    import os, sys, tempfile

                    assert os is __import__("os")
                    assert sys is __import__("sys")
                    assert tempfile is __import__("tempfile")


                    def namespace_callback(value):
                        return value + 1


                    class NamespaceValue:
                        def __init__(self, value):
                            self.value = value


                    namespace_object = NamespaceValue(41)
                    assert (
                        __import__("pickle").loads(__import__("pickle").dumps(namespace_callback))
                        is namespace_callback
                    )
                    assert (
                        __import__("pickle").loads(__import__("pickle").dumps(namespace_object)).value == 41
                    )
                    """),
            )
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(reticulate::py_eval("namespace_callback(41)") == 42L)
                    namespace_r_value <- 42L
                    """),
            )
            client.expect(
                # fmt: python
                python=code("""
                    assert int(r.namespace_r_value) == 42
                    assert os is __import__("os")
                    assert sys is __import__("sys")
                    assert tempfile is __import__("tempfile")
                    os, sys, tempfile = object(), object(), object()
                    namespace_bindings = (os, sys, tempfile)
                    """),
            )
            client.expect(
                "# A tibble: 1 × 1\n  namespace_value\n          <int32>\n1              43\n",
                sql="select 43 as namespace_value",
            )
            client.expect(
                r='stopifnot(reticulate::py_eval("namespace_callback(41)") == 42L)'
            )
            client.expect(python="assert int(r.namespace_r_value) == 42")
            client.expect(
                '[input requested: "namespace interrupt> "]\n[waiting for stdin]',
                python='input("namespace interrupt> ")',
            )
            output = wait_for_evaluation_output(
                client,
                None,
                "input-gated namespace interrupt",
                control="interrupt",
            )
            assert output.endswith("KeyboardInterrupt\n"), output
            assert output.count("KeyboardInterrupt") == 1, output
            client.expect(
                "user namespace preserved\n",
                # fmt: python
                python=code("""
                    assert all(a is b for a, b in zip((os, sys, tempfile), namespace_bindings))
                    assert namespace_callback(41) == 42
                    assert globals() is __import__("__main__").__dict__
                    assert not any(name.startswith("_mcp_console") for name in vars(__import__("builtins")))
                    print("user namespace preserved")
                    """),
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            return client.finish()


@requires(POSIX, R, SQL, command("ir"))
@executions(RUNTIME)
def test_lazy_python_first_namespace(binary: Path, execution: Execution) -> Transcript:
    return lazy_namespace(binary, execution, "python")


@requires(POSIX, R, SQL, command("ir"))
@executions(RUNTIME)
def test_lazy_r_first_namespace(binary: Path, execution: Execution) -> Transcript:
    return lazy_namespace(binary, execution, "r")


@requires(POSIX, R, SQL, command("ir"))
@executions(RUNTIME)
def test_lazy_sql_first_namespace(binary: Path, execution: Execution) -> Transcript:
    return lazy_namespace(binary, execution, "sql")


@requires(R, command("uv"))
@executions(RUNTIME)
def test_interrupted_setup_preserves_startup_bindings(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        modules = root / "modules"
        modules.mkdir()
        # A startup hook intentionally binds user state in main, but its own
        # diagnostics and imports remain in its private module namespace.
        # fmt: python
        checkpoint = code("""
            import __main__, os, sys, tempfile

            if "_mcp_console_services" in sys.modules and not os.path.exists(marker_path):
                open(marker_path, "w").close()
                __main__.os, __main__.sys, __main__.tempfile = os, object(), tempfile
                __main__.namespace_bindings = (__main__.os, __main__.sys, __main__.tempfile)
                input("namespace setup> ")
            """)
        source = modules / "namespace_setup.py"
        source.write_text(checkpoint)
        sitecustomize = modules / "sitecustomize.py"
        sitecustomize.write_text(
            f"marker_path = {str(root / 'attempted')!r}\n"
            f"with open({str(source)!r}) as stream:\n"
            "    checkpoint = compile(stream.read(), '<namespace setup checkpoint>', 'exec')\n"
            "exec(checkpoint)\n"
        )
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHONPATH"] = str(modules)
        # Keep site traceback lines and carets independent of the test runner.
        subprocess.run(
            ["uv", "python", "install", "--no-bin", "--no-registry", "3.13"],
            check=True,
        )
        python = subprocess.check_output(
            ["uv", "python", "find", "3.13"], text=True
        ).strip()
        arguments = execution.serve(
            "-c",
            f"python={json.dumps(python)}",
            *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
        )
        with McpClient(
            binary,
            arguments,
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                '[input requested: "namespace setup> "]\n[waiting for stdin]',
                python='raise AssertionError("interrupted setup ran the cell")',
            )
            output = wait_for_evaluation_output(
                client, None, "interrupted site setup", control="interrupt"
            )
            assert output.endswith("KeyboardInterrupt\n"), output
            assert "AssertionError" not in output, output
            fixture_paths = (str(sitecustomize.resolve()), str(sitecustomize))
            assert any(path in output for path in fixture_paths), output
            # Normalize the complete filename before serialization escapes it.
            for path in fixture_paths:
                output = output.replace(
                    path, "<namespace-workspace>/modules/sitecustomize.py"
                )
            client.transcript[-1]["result"]["content"][0]["text"] = output
            client.expect(
                "startup namespace preserved\n",
                # fmt: python
                python=code("""
                    assert all(a is b for a, b in zip((os, sys, tempfile), namespace_bindings))
                    assert os is __import__("os") and tempfile is __import__("tempfile")
                    assert not any(name.startswith("_mcp_console") for name in vars(__import__("builtins")))
                    print("startup namespace preserved")
                    """),
            )
            client.expect(
                r='stopifnot(reticulate::py_eval("all(a is b for a, b in zip((os, sys, tempfile), namespace_bindings))"))'
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            return client.finish()


@requires(POSIX, R, command("uv"))
@executions(RUNTIME)
def test_activation_error_preserves_r_condition(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_without_r import (
        preparation_directory,
        preparation_environment,
    )

    with preparation_directory() as temporary:
        root = Path(temporary)
        environment = preparation_environment(root, with_r=True)
        (root / "mode").write_text("activation-failure")
        with McpClient(
            installed_console(binary), execution.serve("-c", "cache=host"), environment
        ) as client:
            client.initialize_and_list_tools()
            client.expect("clean Python namespace\n", python=CLEAN_NAMESPACE)
            client.expect(
                # fmt: python
                python=code("""
                    os, sys, tempfile = object(), object(), object()
                    namespace_bindings = (os, sys, tempfile)
                    __import__ = namespace_import_binding = object()
                    """),
            )
            client.expect(
                "Python exception preserved in R\n",
                # fmt: r
                r=code("""
                    invisible(reticulate::py_config())
                    failure <- tryCatch(
                      reticulate::py_require("py-yaml12"),
                      error = identity
                    )
                    # py_require's native boundary preserves ordinary error
                    # messages; reticulate also retains the Python exception.
                    stopifnot(
                      identical(class(failure), c("simpleError", "error", "condition")),
                      startsWith(
                        conditionMessage(failure),
                        "RuntimeError: synthetic activation failure"
                      ),
                      identical(reticulate::py_last_error()$type, "RuntimeError"),
                      identical(reticulate::py_last_error()$value, "synthetic activation failure")
                    )
                    cat("Python exception preserved in R\\n")
                    """),
            )
            client.expect(
                "user namespace preserved\n",
                # fmt: python
                python=code("""
                    assert all(a is b for a, b in zip((os, sys, tempfile), namespace_bindings))
                    assert __import__ is namespace_import_binding
                    assert not any(name.startswith("_mcp_console") for name in (lambda: None).__builtins__)
                    print("user namespace preserved")
                    """),
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
