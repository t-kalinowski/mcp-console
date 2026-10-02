"""Exercise public packaging hooks with a controllable Maturin build."""

from importlib import import_module
import sys
from types import ModuleType


def build(*args):
    print("building", flush=True)
    if sys.stdin.readline().strip() == "fail":
        raise RuntimeError("fixture build failed")
    return "fixture.whl"


maturin = ModuleType("maturin")
for name in (
    "build_wheel",
    "build_editable",
    "build_sdist",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_editable",
    "prepare_metadata_for_build_wheel",
):
    setattr(maturin, name, build)
sys.modules["maturin"] = maturin

build_backend = import_module("build_backend")

print("ready", flush=True)
print(getattr(build_backend, sys.argv[1])("."), flush=True)
