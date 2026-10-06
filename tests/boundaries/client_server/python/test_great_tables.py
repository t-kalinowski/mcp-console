#!/usr/bin/env -S uv run --script

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, wait_for_worker_ready
from support.checkpoints import wait_for_worker_file
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.python import virtualenv_python
from support.records import Transcript
from support.requirements import GREAT_TABLES_BROWSER, command, requires
from support.suites import run_this_suite

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/great_tables"
BASE_PACKAGES = (
    "great-tables==0.21.0",
    "pandas==2.2.3",
    "markdownify==1.2.0",
    "beautifulsoup4==4.15.0",
)
IMAGE_PACKAGES = (
    "selenium==4.33.0",
    "pillow==11.2.1",
    "matplotlib==3.10.3",
)


@contextmanager
def great_tables_client(
    binary: Path, execution: Execution, *, image: bool = False
) -> Iterator[tuple[McpClient, Path, str]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        selected = root / "selected"
        subprocess.run(
            ["uv", "venv", "--python", "3.13", str(selected)],
            check=True,
            capture_output=True,
        )
        python = virtualenv_python(selected)
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                *BASE_PACKAGES,
                *(IMAGE_PACKAGES if image else ()),
            ],
            check=True,
            capture_output=True,
        )
        site = subprocess.check_output(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
        environment = dict(os.environ, RETICULATE_PYTHON=str(python))
        environment.pop("PYTHONPATH", None)
        environment.pop("RETICULATE_PYTHONPATH", None)
        if image:
            config = root / "matplotlib"
            config.mkdir()
            (config / "matplotlibrc").write_text(
                "figure.dpi: 100\nsavefig.bbox: standard\n", encoding="utf-8"
            )
            environment.update(
                MPLCONFIGDIR=str(config), MPLBACKEND="Agg", TMPDIR=str(root)
            )
            environment.pop("MATPLOTLIBRC", None)
            subprocess.run(
                [str(python), "-c", "import matplotlib.pyplot"],
                env=environment,
                check=True,
                capture_output=True,
            )
        workspace = root / "workspace"
        workspace.mkdir()
        with McpClient(binary, execution.serve(), environment, workspace) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "Great Tables preparation readiness")
            client.expect(
                python=(FIXTURES / "table.py").read_text(encoding="utf-8"),
                timeout_ms=600_000,
            )
            yield client, root, site


@requires(command("uv"))
@executions(DIRECT, SANDBOXED)
def test_previews_formatted_html_as_markdown(
    binary: Path, execution: Execution
) -> Transcript:
    with great_tables_client(binary, execution) as (client, _, _):
        client.expect(
            "**Quarterly sales**\n\n"
            "| Region | Revenue | Share |\n"
            "| --- | --- | --- |\n"
            "| North | $1,234.50 | 12.6% |\n"
            "| South | $9,876.00 | 87.4% |\n",
            python=(FIXTURES / "markdown.py").read_text(encoding="utf-8"),
            timeout_ms=600_000,
        )
        return client.finish()


@requires(command("uv"))
@executions(DIRECT, SANDBOXED)
def test_reports_missing_selenium_and_keeps_session_usable(
    binary: Path, execution: Execution
) -> Transcript:
    with great_tables_client(binary, execution) as (client, _, site):
        client.send(
            # fmt: python
            python=code("""
                import importlib.util

                assert importlib.util.find_spec("selenium") is None
                table.save("great-table.png")
                """),
            timeout_ms=600_000,
        )
        content = client.transcript[-1]["result"]["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith(
            "ImportError: Module selenium not found. Run the following to install.\n\n"
            "`pip install selenium`\n"
        ), output
        # Preserve the full traceback, normalizing only installed package paths.
        content[0]["text"] = re.sub(
            r'(?m)^(  File ")' + re.escape(site) + r'([^"]+)',
            lambda match: match[1] + "<site-packages>" + match[2].replace("\\", "/"),
            output,
        )
        client.expect(
            "Python still works: 42\n", python="print('Python still works:', 6 * 7)"
        )
        return client.finish()


@requires(command("uv"), GREAT_TABLES_BROWSER)
@executions(DIRECT)
def test_returns_rendered_table_as_image_content(
    binary: Path, execution: Execution
) -> Transcript:
    # The default macOS sandbox denies Selenium's required localhost bind.
    with great_tables_client(binary, execution, image=True) as (client, root, _):
        client.send(
            python=(FIXTURES / "image.py").read_text(encoding="utf-8"),
            timeout_ms=600_000,
        )
        result = client.transcript[-1]["result"]
        assert [part["type"] for part in result["content"]] == ["text", "image"], result
        assert len(result["content"][1]["data"]) < 8 * 1024 * 1024
        reference = wait_for_worker_file(root, "preview-reference.png", client)
        assert_result_content(
            client,
            ["Great Tables image preview\n", reference.read_bytes()],
            image_reference="live Great Tables pyplot preview {page}",
        )
        client.expect("[]\n", python="plt.get_fignums()")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
