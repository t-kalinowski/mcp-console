"""Main-thread console hooks; other contexts retain their original streams."""

from __future__ import annotations

import builtins
import os
import signal
import sys
import threading
from typing import Any, Callable, TextIO

_pid = os.getpid()
_thread = threading.get_ident()
_input = builtins.input


def _managed() -> bool:
    return os.getpid() == _pid and threading.get_ident() == _thread


class _Output:
    def __init__(self, target: TextIO, publish: Callable[[str], None]) -> None:
        self.target = target
        self.publish = publish

    def write(self, text: str) -> int:
        if not _managed():
            return self.target.write(text)
        self.publish(text)
        return len(text)

    def flush(self) -> None:
        if not _managed():
            return self.target.flush()

    def isatty(self) -> bool:
        return True if _managed() else self.target.isatty()

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


def _console_input(prompt: object = "") -> str:
    if not _managed():
        return _input(prompt)
    return readline(str(prompt))


def install_interrupt() -> None:
    signal.signal(signal.SIGINT, interrupt)


sys.stdout = _Output(sys.stdout, write)
sys.stderr = _Output(sys.stderr, diagnostic)
builtins.input = _console_input
install_interrupt()
