"""Compile and load native checkpoints on macOS and Linux."""

import subprocess
import sys
from pathlib import Path

LOADER_VARIABLE = "DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"

SHARED_LIBRARY_FLAG = "-dynamiclib" if sys.platform == "darwin" else "-shared"


def build_interposer(directory: Path, name: str) -> Path:
    source = Path(__file__).resolve().parents[1] / "fixtures" / "native" / f"{name}.c"
    return compile_interposer(source, directory / name)


def compile_interposer(source: Path, output: Path) -> Path:
    library = output.with_suffix(".dylib" if sys.platform == "darwin" else ".so")
    subprocess.run(
        [
            "cc",
            SHARED_LIBRARY_FLAG,
            "-fPIC",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(library),
            str(source),
        ],
        check=True,
    )
    return library
