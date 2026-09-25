#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import tool_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.resolvers import bare_runtime_environment
from support.execution import DIRECT
from support.requirements import LINUX_NATIVE, NON_UTF8_FILENAMES, requires
from support.suites import run_this_suite


@requires(LINUX_NATIVE)
def test_loads_native_libraries_from_selected_r_home(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    original = Path(environment["R_HOME"])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        selected = root / "R"
        selected.mkdir()
        for entry in original.iterdir():
            if entry.name not in {"lib", "bin"}:
                (selected / entry.name).symlink_to(entry)
        executables = selected / "bin"
        executables.mkdir()
        for entry in (original / "bin").iterdir():
            if entry.name == "R":
                wrapper = executables / "R"
                wrapper.write_text(
                    entry.read_text().replace(
                        f'R_HOME_DIR="{original}"', f'R_HOME_DIR="{selected}"', 1
                    )
                )
                wrapper.chmod(entry.stat().st_mode)
            else:
                (executables / entry.name).symlink_to(entry)
        libraries = selected / "lib"
        libraries.mkdir()
        for entry in (original / "lib").iterdir():
            (libraries / entry.name).symlink_to(entry)
        inherited = root / "inherited"
        inherited.mkdir()
        for name, location, value in (
            ("mcp_r_home_probe", libraries, 20),
            ("mcp_inherited_probe", inherited, 22),
        ):
            source = root / f"{name}.c"
            source.write_text(
                code(f"""
                int {name}(void) {{
                    return {value};
                }}
                """)
            )
            subprocess.run(
                ["cc", "-shared", "-fPIC", "-o", location / f"lib{name}.so", source],
                check=True,
                capture_output=True,
            )
        source = root / "loader_probe.c"
        source.write_text(
            code("""
            extern int mcp_r_home_probe(void);
            extern int mcp_inherited_probe(void);
            void loader_probe(int *result) {
                *result = mcp_r_home_probe() + mcp_inherited_probe();
            }
            """)
        )
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-o",
                root / "loader_probe.so",
                source,
                "-L",
                libraries,
                "-lmcp_r_home_probe",
                "-L",
                inherited,
                "-lmcp_inherited_probe",
            ],
            check=True,
            capture_output=True,
        )
        environment["R_HOME"] = str(selected)
        # Rscript uses RHOME to override its compiled-in installation path.
        environment["RHOME"] = str(selected)
        environment["PATH"] = os.pathsep.join([str(executables), environment["PATH"]])
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            [str(inherited), *filter(None, [environment.get("LD_LIBRARY_PATH")])]
        )
        with McpClient(
            binary, DIRECT.serve(), environment, current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(
                # fmt: r
                r=code("""
                    dyn.load("loader_probe.so")
                    .C("loader_probe", result = 0L)$result
                    """)
            )
            assert tool_text(result) == "[1] 42\n", result
            return client.finish()


@requires(NON_UTF8_FILENAMES)
def test_preserves_non_utf8_r_home(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    original = Path(environment["R_HOME"])
    with tempfile.TemporaryDirectory() as directory:
        library = Path(directory) / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        selected = Path(directory) / os.fsdecode(b"R-home-\xff")
        selected.symlink_to(original, target_is_directory=True)
        environment["R_HOME"] = str(selected)
        # R requires a byte-oriented locale for paths outside UTF-8.
        environment["LC_ALL"] = "C"
        # Native sandbox policy requires UTF-8 environment values; exercise
        # the direct launch contract that supports arbitrary Unix path bytes.
        with McpClient(binary, DIRECT.serve(), environment) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: r
                    r=code("""
                        stopifnot(as.raw(255) %in% charToRaw(Sys.getenv("R_HOME")))
                        42L
                        """),
                )
                assert "[1] 42\n" in tool_text(client.transcript[-1]["result"]), (
                    client.transcript[-1]
                )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
