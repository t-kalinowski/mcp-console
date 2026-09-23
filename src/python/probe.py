"""Inspect one selected environment; this program never chooses an interpreter."""

import importlib.metadata
import json
import os
import site
import sys
import sysconfig

# -S gives site processing an explicit path baseline in this disposable child.
# In particular, executable .pth lines can add paths outside the environment.
before = set(sys.path)
site.main()
directories = [path for path in site.getsitepackages() if os.path.isdir(path)]
owned = (set(sys.path) - before) | set(directories)
directory = sysconfig.get_config_var(
    "PYTHONFRAMEWORKPREFIX" if sysconfig.get_config_var("PYTHONFRAMEWORK") else "LIBDIR"
)
library = os.path.realpath(
    os.path.join(directory, sysconfig.get_config_var("INSTSONAME"))
)
# Match reticulate's optional NumPy metadata without importing it in the worker.
numpy = None
try:
    import numpy as np
except Exception:
    pass
else:
    numpy = {"path": os.path.realpath(np.__path__[0]), "version": np.__version__}
print(
    "\x1eMCP_CONSOLE_ENVIRONMENT\x1e"
    + json.dumps(
        {
            "executable": sys.executable,
            "libpython": library,
            "version": sys.version.split()[0],
            "prefix": sys.prefix,
            "exec_prefix": sys.exec_prefix,
            "site_packages": directories,
            "site_paths": sorted(owned),
            "numpy": numpy,
            "distributions": {
                d.metadata["Name"]: d.version
                for d in importlib.metadata.distributions()
            },
        }
    )
    + "\x1f",
    flush=True,
)
