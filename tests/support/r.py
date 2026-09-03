import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from support.client import McpClient, stop_client


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def r_test_environment() -> tuple[dict[str, str], Path]:
    environment = os.environ.copy()
    if r_home := environment.get("R_HOME"):
        home = Path(r_home)
    else:
        output = subprocess.run(
            ["R", "RHOME"],
            check=True,
            capture_output=True,
            text=True,
        )
        home = Path(output.stdout.strip())
        environment["R_HOME"] = str(home)
    return environment, home / "bin" / "Rscript"


def build_r_input_handler(
    directory: Path,
    environment: dict[str, str],
    rscript: Path,
) -> None:
    source = FIXTURES / "r_input_handler.c"
    local_source = directory / source.name
    shutil.copyfile(source, local_source)
    subprocess.run(
        [
            rscript.parent / "R",
            "CMD",
            "SHLIB",
            "-o",
            "mcp_test_input_handler.so",
            local_source.name,
        ],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def reference_plots(
    rscript: Path,
    environment: dict[str, str],
    source: str,
    *,
    width: float,
    height: float,
    dpi: float,
    pages: int,
    expected_error: str | None = None,
) -> list[bytes]:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        error_handler = ""
        if expected_error is not None:
            message = json.dumps(expected_error)
            error_handler = (
                ", error = function(error) "
                f"stopifnot(identical(conditionMessage(error), {message}))"
            )
        script = (
            "base::local({\n"
            "  directory <- commandArgs(trailingOnly = TRUE)[[1L]]\n"
            "  device_counter <- 0L\n"
            "  options(device = function(...) {\n"
            "    device_counter <<- device_counter + 1L\n"
            "    grDevices::png(\n"
            "      filename = file.path(\n"
            "        directory,\n"
            '        sprintf("device-%06d-page-%%06d.png", device_counter)\n'
            "      ),\n"
            f'      width = {width}, height = {height}, units = "in", res = {dpi}\n'
            "    )\n"
            "  })\n"
            "  tryCatch({\n"
            f"{source}"
            f"  }}{error_handler}, finally = grDevices::graphics.off())\n"
            "})\n"
        )
        subprocess.run(
            [rscript, "--vanilla", "-", str(directory)],
            input=script,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        paths = sorted(directory.glob("device-*-page-*.png"))
        assert len(paths) == pages, paths
        return [path.read_bytes() for path in paths]


@contextmanager
def r_input_handler_client(binary: Path) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        build_r_input_handler(directory, environment, rscript)
        client = McpClient(
            binary,
            ("serve",),
            environment=environment,
            current_directory=directory,
        )
        try:
            yield client, directory
        finally:
            stop_client(client)
