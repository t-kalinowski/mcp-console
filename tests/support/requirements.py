"""Capabilities of the implemented runtime and of the host test facilities."""

import os
import platform
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
    "worker", sys.platform in {"darwin", "linux"}, "workers require macOS or Linux"
)
SANDBOX = Requirement(
    "sandbox",
    sys.platform in {"darwin", "linux"},
    "the sandbox requires macOS or Linux",
)
MACOS_SANDBOX = Requirement(
    "macOS sandbox",
    sys.platform == "darwin",
    "requires Seatbelt policy or kqueue supervision",
)
LINUX_SANDBOX = Requirement(
    "Linux sandbox",
    sys.platform == "linux",
    "requires Linux namespace isolation",
)
POSIX = Requirement(
    "POSIX", os.name == "posix", "requires POSIX processes and descriptors"
)
PROCESS_EVENTS = Requirement(
    "process events",
    sys.platform in {"darwin", "linux"},
    "requires macOS process events or Linux procfs, inotify, and pidfds",
)
NATIVE_FIXTURES = Requirement(
    "native fixtures",
    sys.platform in {"darwin", "linux"},
    "requires macOS or Linux native fixture compilation and interposition",
)
# XNU's bsd/dev/arm/unix_signal.c reports SEGV_ACCERR for every SIGSEGV,
# including null access. Keep these real kernel diagnostics in separate cases.
NULL_FAULT_ACCERR = Requirement(
    "null fault with SEGV_ACCERR",
    sys.platform == "darwin" and platform.machine() == "arm64",
    "requires ARM macOS null-fault diagnostics",
)
NULL_FAULT_MAPERR = Requirement(
    "null fault with SEGV_MAPERR",
    WORKER.available and not NULL_FAULT_ACCERR.available,
    "ARM macOS reports SEGV_ACCERR for null faults",
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


# ELF loader and seccomp fixtures exercise Linux implementation details.
LINUX_NATIVE = Requirement(
    "Linux native fixtures",
    sys.platform == "linux",
    "requires Linux ELF loading and seccomp",
)
