"""Describe the interpreter selected by the test runner for native embedding."""

import json
import sys
import sysconfig
from pathlib import Path

library_root = (
    sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")
    if sysconfig.get_config_var("PYTHONFRAMEWORK")
    else sysconfig.get_config_var("LIBDIR")
)

print(
    json.dumps(
        {
            "python": sys.executable,
            "libpython": str(
                Path(library_root) / sysconfig.get_config_var("LDLIBRARY")
            ),
            "python_home": sys.base_prefix,
        }
    )
)
