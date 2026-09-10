"""Blocking file-change and process-exit checkpoints for public boundary tests."""

import ctypes
import os
import select
import sys
from pathlib import Path


class Events:
    def __init__(self) -> None:
        self.files: dict[Path, int] = {}
        self.processes: dict[int, int] = {}
        if sys.platform == "darwin":
            self.queue = select.kqueue()
        else:
            assert sys.platform == "linux"
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.inotify_add_watch.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            self.fd = self.libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
            if self.fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1 failed")

    def watch_file(self, path: Path) -> None:
        if path in self.files:
            return
        if sys.platform == "darwin":
            descriptor = os.open(path, os.O_EVTONLY | os.O_CLOEXEC)
            self.files[path] = descriptor
            self.queue.control(
                [
                    select.kevent(
                        descriptor,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_WRITE
                        | select.KQ_NOTE_RENAME
                        | select.KQ_NOTE_DELETE,
                    )
                ],
                0,
                0,
            )
        else:
            # MODIFY, ATTRIB, MOVED_FROM/TO, CREATE, DELETE, DELETE_SELF, MOVE_SELF.
            descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(path), 0xFC6)
            if descriptor < 0:
                raise OSError(ctypes.get_errno(), f"cannot watch {path}")
            self.files[path] = descriptor

    def watch_process(self, pid: int) -> None:
        assert pid not in self.processes
        if sys.platform == "darwin":
            self.queue.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
                0,
            )
            self.processes[pid] = pid
        else:
            self.processes[pid] = os.pidfd_open(pid)

    def wait(self, timeout: float) -> set[int]:
        """Return exited PIDs, with -1 indicating a filesystem change."""
        if sys.platform == "darwin":
            events = self.queue.control(
                None, max(1, len(self.files) + len(self.processes)), timeout
            )
            return {
                event.ident if event.filter == select.KQ_FILTER_PROC else -1
                for event in events
            }
        ready, _, _ = select.select(
            [self.fd, *self.processes.values()], [], [], timeout
        )
        result = {
            pid for pid, descriptor in self.processes.items() if descriptor in ready
        }
        for pid in result:
            os.close(self.processes.pop(pid))
        if self.fd in ready:
            os.read(self.fd, 64 * 1024)
            result.add(-1)
        return result

    def close(self) -> None:
        if sys.platform == "darwin":
            self.queue.close()
            for descriptor in self.files.values():
                os.close(descriptor)
        else:
            os.close(self.fd)
            for descriptor in self.processes.values():
                os.close(descriptor)

    def __enter__(self) -> "Events":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
