"""Capabilities of the implemented runtime and of the host test facilities."""

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar


@dataclass(frozen=True)
class Requirement:
    name: str
    available: bool
    reason: str


# Keep implementation availability here until the corresponding runtime lands.
WORKER = Requirement(
    "worker", sys.platform == "darwin", "workers are implemented only on macOS"
)
SANDBOX = Requirement(
    "sandbox", sys.platform == "darwin", "the sandbox is implemented only on macOS"
)
POSIX = Requirement(
    "POSIX", os.name == "posix", "requires POSIX processes and descriptors"
)
PROCESS_EVENTS = Requirement(
    "process events",
    sys.platform == "darwin",
    "requires macOS process identity and kqueue events",
)
NATIVE_FIXTURES = Requirement(
    "native fixtures",
    sys.platform == "darwin",
    "requires macOS native fixture compilation and interposition",
)
NO_WORKER = Requirement(
    "unsupported worker", not WORKER.available, "workers are available on this platform"
)
NO_SANDBOX = Requirement(
    "unsupported sandbox",
    not SANDBOX.available,
    "the sandbox is available on this platform",
)

SYSTEM_PYTHON = Path("/usr/bin/python3")
OLD_PYTHON = Requirement(
    "Python before 3.10",
    sys.platform == "darwin" and SYSTEM_PYTHON.is_file(),
    "requires the macOS system Python fixture",
)

SYSTEM_FONT_DIRECTORY = Path("/System/Library/Fonts")
SYSTEM_FONTS = Requirement(
    "system font discovery",
    sys.platform == "darwin" and SYSTEM_FONT_DIRECTORY.is_dir(),
    "requires macOS system_profiler font discovery",
)


def command(name: str) -> Requirement:
    return Requirement(
        name, shutil.which(name) is not None, f"{name} is missing from PATH"
    )


Case = TypeVar("Case", bound=Callable)


def requires(*requirements: Requirement) -> Callable[[Case], Case]:
    def decorate(case: Case) -> Case:
        case.requirements = (*getattr(case, "requirements", ()), *requirements)
        return case

    return decorate


def missing_reasons(requirements: tuple[Requirement, ...]) -> str:
    return "; ".join(
        f"{requirement.name}: {requirement.reason}"
        for requirement in dict.fromkeys(requirements)
        if not requirement.available
    )
