"""Run the public sans-R cases against an installed build on an R-free host.

Use tests/fixtures/python_without_r.Dockerfile for a reproducible Linux host.
The container must permit native namespaces (for example, docker --privileged).
"""

import importlib.util
import os
import shutil
from pathlib import Path

assert "R_HOME" not in os.environ, "unset R_HOME on the R-free host"
assert all(shutil.which(name) is None for name in ("R", "Rscript", "ir"))
assert not any(
    Path(root).exists() for root in ("/usr/lib/R", "/usr/local/lib/R", "/opt/R")
)
binary = Path(shutil.which("mcp-console") or "").absolute()
assert binary.is_file(), "install the wheel before running acceptance"
suite = Path(__file__).parent / "boundaries/client_server/python/test_without_r.py"
spec = importlib.util.spec_from_file_location("sans_r_acceptance", suite)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

for name, case in vars(module).items():
    if not name.startswith("test_"):
        continue
    for execution in case.executions:
        print(f"{name}[{execution.name}]", flush=True)
        case(binary, execution)
print("Installed R-free public MCP acceptance passed", flush=True)
