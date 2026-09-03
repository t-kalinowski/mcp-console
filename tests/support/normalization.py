import re
from textwrap import dedent


def code(source: str) -> str:
    return dedent(source).removeprefix("\n")


def normalize_python_resolution_error(error: str, invalid: str | None = None) -> str:
    error = normalize_python_traceback_paths(error)
    error, python_patch = re.subn(
        r'(?m)^(  "python": "\d+\.\d+)\.\d+( \(reticulate default\))?(",)$',
        r"\1.x\2\3",
        error,
        count=1,
    )
    assert python_patch == 1, error
    has_python_version = '\n  "python_version": [\n' in error
    error, python_version_patch = re.subn(
        r'(?m)^(  "python_version": \[\n    "\d+\.\d+)\.\d+("\n  \])$',
        r"\1.x\2",
        error,
        count=1,
    )
    assert python_version_patch == int(has_python_version), error
    if invalid is not None:
        assert invalid in error, error
    return error


def normalize_python_traceback_paths(error: str) -> str:
    replacements = (
        (
            r'(?m)^(\s+File ")[^"\n]*/reticulate/python/(rpytools/loader\.py")',
            r"\1<reticulate>/python/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/lib/python\d+\.\d+/(importlib/__init__\.py")',
            r"\1<python-stdlib>/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/(tests/fixtures/checkpoint_uv")'
            r", line \d+",
            r"\1<workspace>/\2, line <line>",
        ),
    )
    for pattern, replacement in replacements:
        error = re.sub(pattern, replacement, error)
    assert re.search(r'(?m)^\s+File "/', error) is None, error
    return error
