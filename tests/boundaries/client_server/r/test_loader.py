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
from support.suites import run_this_suite

PLATFORMS = {"linux"}


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
            binary, ("serve",), environment, current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(
                r=code("""
                dyn.load("loader_probe.so")
                .C("loader_probe", result = 0L)$result
                """)
            )
            assert tool_text(result) == "[1] 42\n", result
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
