"""Local Python distributions for public preparation and runtime tests."""

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
    root: Path, name: str, module_source: str | None, *, command: str | None = None
) -> Path:
    wheels = root / "wheels"
    wheels.mkdir(exist_ok=True)
    wheel = wheels / f"{name}-1.0.0-py3-none-any.whl"
    dist_info = f"{name}-1.0.0.dist-info"
    entries = {
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.3\nName: {name.replace('_', '-')}\nVersion: 1.0.0\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: mcp-console test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if module_source is not None:
        entries[f"{name}/__init__.py"] = module_source
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
