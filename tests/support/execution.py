"""Explicit launch fixtures; selecting a mode never rewrites McpClient arguments."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from support.requirements import SANDBOX, WORKER, Case, Requirement


@dataclass(frozen=True)
class Execution:
    name: str
    requirements: tuple[Requirement, ...] = ()

    def serve(self, *arguments: str) -> tuple[str, ...]:
        assert self.name in {"direct", "sandbox"}, self.name
        assert "--no-sandbox" not in arguments
        return (
            "serve",
            *(("--no-sandbox",) if self.name == "direct" else ()),
            *arguments,
        )

    def command(self, binary: Path, *arguments: str) -> list[str]:
        assert self.name in {"direct", "sandbox"}, self.name
        target = [str(binary), *arguments]
        return (
            [str(binary), "sandbox", "--", *target]
            if self.name == "sandbox"
            else target
        )


DIRECT = Execution("direct", (WORKER,))
SANDBOXED = Execution("sandbox", (WORKER, SANDBOX))


def executions(*modes: Execution) -> Callable[[Case], Case]:
    assert modes and len({mode.name for mode in modes}) == len(modes)

    def decorate(case: Case) -> Case:
        case.executions = modes
        return case

    return decorate
