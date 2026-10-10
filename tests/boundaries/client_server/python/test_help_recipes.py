"""Installed-object documentation recipes through ordinary Python cells."""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


@executions(RUNTIME)
def test_documents_existing_module_and_function_with_provenance(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(prefix="PythonHelp-") as directory:
        workspace = Path(directory).resolve()
        module_path = workspace / "recipe_example.py"
        module_path.write_text(
            # fmt: python
            code('''
                """A local documentation example."""


                def double(value: int) -> int:
                    """Return twice the value."""
                    return value * 2
                '''),
            encoding="utf-8",
        )
        # Offline distribution metadata; the import and distribution names differ.
        metadata = workspace / "recipe_distribution-1.2.3.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: recipe-distribution\nVersion: 1.2.3\n",
            encoding="utf-8",
        )
        with McpClient(
            binary, execution.serve(), current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                python="import importlib.metadata, platform, pydoc, recipe_example"
            )
            client.send(
                # fmt: python
                python=code("""
                    module = recipe_example
                    print(f"Python {platform.python_version()}")
                    print(f"Module {module.__name__}: {module.__spec__.origin}")
                    print(
                        f"Distribution recipe-distribution: {importlib.metadata.version('recipe-distribution')}"
                    )
                    for target in (module, module.double):
                        encoded = pydoc.render_doc(target, renderer=pydoc.plaintext).encode("utf-8")
                        preview = encoded[:4096].decode("utf-8", errors="ignore")
                        print(preview, end="")
                        omitted = len(encoded) - len(preview.encode("utf-8"))
                        if omitted:
                            print(f"\\n[documentation preview; {omitted} UTF-8 bytes omitted]")
                    """),
            )
            output = last_tool_text(client)
            version, origin, distribution = output.splitlines()[:3]
            assert re.fullmatch(r"Python \d+\.\d+\.\d+", version), output
            assert origin == f"Module recipe_example: {module_path}", output
            assert distribution == "Distribution recipe-distribution: 1.2.3", output
            assert "A local documentation example." in output, output
            assert "double(value: int) -> int" in output, output
            assert "Return twice the value." in output, output
            assert "[documentation preview;" not in output and "\b" not in output, (
                output
            )
            file_section = re.search(r"\nFILE\n    ([^\n]+)\n", output)
            assert file_section is not None, output
            file_path = file_section.group(1)
            # pydoc's FILE path is normcase-normalized, unlike module provenance.
            assert Path(file_path).samefile(module_path), output
            client.transcript[-1]["result"]["content"][0]["text"] = (
                output.replace(version, "Python <installed version>")
                .replace(str(module_path), "<workspace>/recipe_example.py")
                .replace(file_path, "<workspace>/recipe_example.py")
            )
            return client.finish()


@executions(RUNTIME)
def test_missing_documentation_target_preserves_python_error_and_session(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(python="import math, pydoc")
        client.send(
            python="pydoc.render_doc(math.missing_documentation_target, renderer=pydoc.plaintext)"
        )
        output = last_tool_text(client)
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith(
            "AttributeError: module 'math' has no attribute 'missing_documentation_target'\n"
        ), output
        client.expect("9.0\n", python="math.sqrt(81)")
        return client.finish()


@executions(RUNTIME)
def test_long_unicode_documentation_has_bounded_preview_and_omission_count(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import pydoc


                def documented() -> None:
                    pass


                documented.__doc__ = "🙂" * 3000
                """),
        )
        client.send(
            # fmt: python
            python=code("""
                target = documented
                encoded = pydoc.render_doc(target, renderer=pydoc.plaintext).encode("utf-8")
                preview = encoded[:4096].decode("utf-8", errors="ignore")
                print(preview, end="")
                omitted = len(encoded) - len(preview.encode("utf-8"))
                if omitted:
                    print(f"\\n[documentation preview; {omitted} UTF-8 bytes omitted]")
                """),
        )
        # The 95-byte heading leaves space for 1,000 complete four-byte characters.
        # One byte of the next character is excluded and counted as omitted.
        prefix = (
            "Python Library Documentation: function documented in module __main__\n\n"
            "documented() -> None\n    "
        )
        expected = (
            prefix
            + "🙂" * 1000
            + "\n[documentation preview; 8001 UTF-8 bytes omitted]\n"
        )
        assert last_tool_text(client) == expected, client.transcript[-1]
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
