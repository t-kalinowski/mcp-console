"""Local Python distributions for public preparation and runtime tests."""

import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path


def runtime_source_line(statement: str) -> int:
    """Keep public setup tracebacks exact when embedded source moves."""
    source = (Path(__file__).resolve().parents[2] / "src/python/runtime.py").read_text()
    matches = [
        line
        for line, text in enumerate(source.splitlines(), 1)
        if text.strip() == statement
    ]
    assert len(matches) == 1, statement
    return matches[0]


def write_test_wheel(
    root: Path,
    name: str,
    module_source: str | None,
    *,
    command: str | None = None,
    native_module: Path | None = None,
    package_files: dict[str, str] | None = None,
) -> Path:
    wheels = root / "wheels"
    wheels.mkdir(exist_ok=True)
    tag = "py3-none-any"
    if native_module is not None:
        platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        tag = f"cp39-abi3-{platform}"
    wheel = wheels / f"{name}-1.0.0-{tag}.whl"
    dist_info = f"{name}-1.0.0.dist-info"
    entries: dict[str, str | bytes] = {
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.3\nName: {name.replace('_', '-')}\nVersion: 1.0.0\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: mcp-console test\n"
            f"Root-Is-Purelib: {str(native_module is None).lower()}\nTag: {tag}\n"
        ),
    }
    if module_source is not None:
        entries[f"{name}/__init__.py"] = module_source
    if native_module is not None:
        entries[f"{name}.abi3.so"] = native_module.read_bytes()
    for path, source in (package_files or {}).items():
        entries[f"{name}/{path}"] = source
    if command is not None:
        entries[f"{dist_info}/entry_points.txt"] = (
            f"[console_scripts]\n{command} = {name}:main\n"
        )
    entries[f"{dist_info}/RECORD"] = "\n".join(f"{entry},," for entry in entries) + "\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    index = root / "index" / name.replace("_", "-")
    index.mkdir(parents=True)
    (index / "index.html").write_text(f'<a href="{wheel.as_uri()}">{wheel.name}</a>\n')
    return index.parent


def build_test_extension(directory: Path, fixture: str) -> Path:
    source = (
        Path(__file__).resolve().parents[1] / "fixtures" / "native" / f"{fixture}.c"
    )
    library = directory / f"{fixture}.abi3.so"
    linker = (
        ["-bundle", "-undefined", "dynamic_lookup"]
        if sys.platform == "darwin"
        else ["-shared"]
    )
    subprocess.run(
        [
            "cc",
            *linker,
            "-fPIC",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            sysconfig.get_path("include"),
            "-o",
            str(library),
            str(source),
        ],
        check=True,
    )
    return library
