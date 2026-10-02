"""Blocking Windows checkout ownership shared by staging and packaging."""

import ctypes
import msvcrt
from contextlib import contextmanager
from ctypes import wintypes


def lock_windows(descriptor):
    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    lock_file = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
    lock_file.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock_file.restype = wintypes.BOOL
    overlapped = Overlapped()
    if not lock_file(
        msvcrt.get_osfhandle(descriptor), 2, 0, 1, 0, ctypes.byref(overlapped)
    ):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def exclusive(paths, label, *, wait=False):
    path = paths[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        lock_windows(lock.fileno())
        yield


def checkout_owner(root):
    return exclusive([root / ".dev-workflow/checkout.lock"], "checkout", wait=True)
