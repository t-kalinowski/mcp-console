"""Inspect the selected interpreter without importing a language adapter."""

import ctypes
import importlib.metadata
import json
import os
import site
import sys
import sysconfig

if sys.version_info < (3, 10):
    sys.exit("MCP Console requires Python 3.10 or later")

library_directory = sysconfig.get_config_var(
    "PYTHONFRAMEWORKPREFIX" if sysconfig.get_config_var("PYTHONFRAMEWORK") else "LIBDIR"
)
library_name = sysconfig.get_config_var("INSTSONAME")
if not library_directory or not library_name:
    sys.exit(
        "Python requires a shared library; selected interpreter has no configured library"
    )
library = os.path.join(library_directory, library_name)
if not os.path.isfile(library):
    sys.exit(f"Python requires a shared library; selected interpreter has no {library}")
try:
    ctypes.CDLL(library, mode=os.RTLD_NOW | os.RTLD_GLOBAL)
except OSError as error:
    sys.exit(f"Python shared library cannot be loaded: {error}")

print(
    "\x1eMCP_CONSOLE_PYTHON_DISCOVERY\x1e"
    + json.dumps(
        {
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "libpython": os.path.realpath(library),
            "version": list(sys.version_info[:3]),
            "site_packages": site.getsitepackages(),
            "distributions": {
                distribution.metadata["Name"]: distribution.version
                for distribution in importlib.metadata.distributions()
            },
        }
    )
)
