#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_peer_runtime import without_r
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.python import build_test_extension, write_test_wheel
from support.requirements import NATIVE_FIXTURES, requires
from support.resolvers import recording_uv_environment, uv_tool_run_requirements
from support.suites import run_this_suite


def managed_environment(directory: Path) -> tuple[dict[str, str], Path]:
    environment, record = recording_uv_environment(directory)
    wrapper = environment["RETICULATE_UV"]
    without_r(environment, directory)
    path = Path(environment["PATH"])
    (path / "uv").symlink_to(wrapper)
    (path / "python3").symlink_to(sys.executable)
    return environment, record


@executions(DIRECT, SANDBOXED)
def test_rply_optional_import_does_not_install_rpython(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = managed_environment(directory)
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": ["rply==0.7.8"]})
            before = uv_tool_run_requirements(record)
            client.send(
                # fmt: python
                python=code(r"""
                    import importlib.util

                    assert importlib.util.find_spec("rpython") is None
                    import rply

                    lexer = rply.LexerGenerator()
                    lexer.add("NUMBER", r"\d+")
                    [(token.name, token.value) for token in lexer.build().lex("42")]
                    """),
            )
            assert last_result_text(client) == "[('NUMBER', '42')]\n", last_result_text(
                client
            )
            assert uv_tool_run_requirements(record) == before
            client.send(python='importlib.util.find_spec("rpython") is None')
            assert last_result_text(client) == "True\n"
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]
            assert declaration["requirements"]["python"] == ["rply==0.7.8"]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_installed_package_probes_leave_resolution_to_explicit_imports(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        # fmt: python
        source = code("""
            try:
                import mcp_console_test_optional_dependency
            except ImportError:
                fallback = True
            else:
                fallback = False


            def delayed_import():
                import mcp_console_test_delayed_dependency

                return mcp_console_test_delayed_dependency.answer
            """)
        index = write_test_wheel(directory, "mcp_console_test_optional_package", source)
        for name in ("optional", "delayed", "required"):
            write_test_wheel(
                directory, f"mcp_console_test_{name}_dependency", "answer = 42\n"
            )
        (directory / "local_script.py").write_text(
            "import mcp_console_test_required_dependency\n"
            "answer = mcp_console_test_required_dependency.answer\n"
        )
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(
            binary, execution.serve(), environment, current_directory=directory
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set"})
            before = uv_tool_run_requirements(record)
            client.send(
                python="import mcp_console_test_optional_package as package; package.fallback"
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            runs = uv_tool_run_requirements(record)
            assert len(runs) == len(before) + 1, runs
            assert "mcp_console_test_optional_package" in runs[-1], runs
            assert "mcp_console_test_optional_dependency" not in runs[-1], runs
            for source in (
                "import mcp_console_test_optional_dependency; mcp_console_test_optional_dependency.answer",
                "package.delayed_import()",
                "import local_script; local_script.answer",
            ):
                client.send(python=source)
                assert last_result_text(client) == "42\n", last_result_text(client)
            assert len(uv_tool_run_requirements(record)) == len(before) + 4
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_local_module_shadowing_installed_package_resolves_missing_imports(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcp_console_test_shadowed_package"
        dependency = "mcp_console_test_shadowed_dependency"
        index = write_test_wheel(directory, package, "answer = -1\n")
        write_test_wheel(directory, dependency, "answer = 42\n")
        (directory / f"{package}.py").write_text(
            f"import {dependency}\nanswer = {dependency}.answer\n"
        )
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(
            binary, execution.serve(), environment, current_directory=directory
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": [package]})
            before = uv_tool_run_requirements(record)
            client.send(
                # fmt: python
                python=code("""
                    import mcp_console_test_shadowed_package as package
                    from pathlib import Path

                    assert Path(package.__file__).samefile("mcp_console_test_shadowed_package.py")
                    package.answer
                    """),
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            runs = uv_tool_run_requirements(record)
            assert len(runs) == len(before) + 1, runs
            assert dependency in runs[-1], runs
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_installed_initializer_leaves_local_module_imports_eligible(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcp_console_test_plugin_framework"
        dependency = "mcp_console_test_settings_dependency"
        index = write_test_wheel(
            directory,
            package,
            "import local_settings\nanswer = local_settings.answer\n",
        )
        write_test_wheel(directory, dependency, "answer = 42\n")
        (directory / "local_settings.py").write_text(
            f"import {dependency}\nanswer = {dependency}.answer\n"
        )
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(
            binary, execution.serve(), environment, current_directory=directory
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": [package]})
            before = uv_tool_run_requirements(record)
            client.send(
                python="import mcp_console_test_plugin_framework; mcp_console_test_plugin_framework.answer"
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            runs = uv_tool_run_requirements(record)
            assert len(runs) == len(before) + 1, runs
            assert dependency in runs[-1], runs
            return client.finish()


@requires(NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_installed_extension_optional_import_observes_absence(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcp_console_test_native_optional"
        dependency = "mcp_console_test_native_optional_dependency"
        extension = build_test_extension(directory, "python_optional_import")
        index = write_test_wheel(directory, package, None, native_module=extension)
        write_test_wheel(directory, dependency, "answer = 42\n")
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": [package]})
            before = uv_tool_run_requirements(record)
            client.send(
                python="import mcp_console_test_native_optional as package; bool(package.fallback)"
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            assert uv_tool_run_requirements(record) == before
            client.send(
                python="import mcp_console_test_native_optional_dependency; mcp_console_test_native_optional_dependency.answer"
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            runs = uv_tool_run_requirements(record)
            assert len(runs) == len(before) + 1, runs
            assert dependency in runs[-1], runs
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_installed_initializer_leaves_cached_local_callback_eligible(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcp_console_test_callback_framework"
        dependency = "mcp_console_test_callback_dependency"
        index = write_test_wheel(
            directory,
            package,
            "import local_plugin\nanswer = local_plugin.initialize()\n",
        )
        write_test_wheel(directory, dependency, "answer = 42\n")
        (directory / "local_plugin.py").write_text(
            f"def initialize():\n    import {dependency}\n    return {dependency}.answer\n"
        )
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(
            binary, execution.serve(), environment, current_directory=directory
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": [package]})
            client.send(python="import local_plugin")
            assert last_result_text(client) == "[done]", last_result_text(client)
            before = uv_tool_run_requirements(record)
            client.send(
                python="import mcp_console_test_callback_framework; mcp_console_test_callback_framework.answer"
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            runs = uv_tool_run_requirements(record)
            assert len(runs) == len(before) + 1, runs
            assert dependency in runs[-1], runs
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_loaded_package_optional_imports_survive_compatible_activation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcp_console_test_retained_package"
        dependency = "mcp_console_test_retained_optional_dependency"
        addition = "mcp_console_test_activation_addition"
        # fmt: python
        source = code("""
            try:
                import mcp_console_test_retained_optional_dependency
            except ImportError:
                fallback = True
            else:
                fallback = False
            """)
        index = write_test_wheel(
            directory, package, "answer = 42\n", package_files={"later.py": source}
        )
        write_test_wheel(directory, dependency, "answer = 42\n")
        write_test_wheel(directory, addition, "answer = 0\n")
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": [package]})
            client.send(
                # fmt: python
                python=code("""
                    import mcp_console_test_retained_package as package
                    from pathlib import Path
                    import sys

                    initial_package_path = Path(package.__file__).resolve().parent
                    initial_site = Path(package.__spec__.submodule_search_locations[0]).parent
                    assert initial_site in {Path(path) for path in sys.path if path}
                    """),
            )
            assert last_result_text(client) == "[done]", last_result_text(client)
            client.send(requirements={"action": "add", "python": [addition]})
            assert last_result_text(client) == "[prepared]", last_result_text(client)
            before = uv_tool_run_requirements(record)
            client.send(
                # fmt: python
                python=code("""
                    assert initial_site not in {Path(path) for path in sys.path if path}
                    assert Path(package.__path__[0]).resolve() == initial_package_path
                    from mcp_console_test_retained_package import later

                    assert Path(later.__file__).samefile(initial_package_path / "later.py")
                    later.fallback
                    """),
            )
            assert last_result_text(client) == "True\n", last_result_text(client)
            assert uv_tool_run_requirements(record) == before
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_deferred_lazy_module_imports_remain_eligible(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcp_console_test_lazy_package"
        dependency = "mcp_console_test_lazy_dependency"
        # fmt: python
        source = code("""
            try:
                import mcp_console_test_lazy_dependency
            except ImportError:
                fallback = True
            else:
                fallback = False
            """)
        index = write_test_wheel(directory, package, source)
        write_test_wheel(directory, dependency, "answer = 42\n")
        environment, record = managed_environment(directory)
        environment["UV_INDEX"] = index.as_uri()
        environment["UV_INDEX_STRATEGY"] = "first-index"
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set", "python": [package]})
            before = uv_tool_run_requirements(record)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sys

                    specification = importlib.util.find_spec("mcp_console_test_lazy_package")
                    specification.loader = importlib.util.LazyLoader(specification.loader)
                    package = importlib.util.module_from_spec(specification)
                    sys.modules[specification.name] = package
                    specification.loader.exec_module(package)
                    package.fallback
                    """),
            )
            assert last_result_text(client) == "False\n", last_result_text(client)
            after = uv_tool_run_requirements(record)
            assert len(after) == len(before) + 1
            assert set(after[-1]) == set(before[-1]) | {dependency}, after
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_default_package_imports_do_not_prepare_optional_dependencies(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = managed_environment(Path(temporary))
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            # Background preparation must finish before recording resolver calls.
            client.send(python="pass")
            before = uv_tool_run_requirements(record)
            client.send(
                # fmt: python
                python=code("""
                    import numpy
                    import pandas

                    (numpy.arange(3).sum().item(), pandas.Series([1, 2]).sum().item())
                    """),
            )
            assert last_result_text(client) == "(3, 3)\n", last_result_text(client)
            assert uv_tool_run_requirements(record) == before, (
                before,
                uv_tool_run_requirements(record),
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
