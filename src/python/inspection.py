"""Describe one already-selected CPython executable for native embedding."""

import ctypes
import json
import os
import sys
import sysconfig
from pathlib import Path


def executable_image_identity():
    if sys.platform == "linux":
        image = os.stat("/proc/self/exe")
        return [image.st_dev, image.st_ino]

    # The framework launcher can exec Python.app while retaining the selected
    # bin/python path in sys.executable. Query the mapped Mach-O vnode rather
    # than statting that pathname after it may have been replaced.
    class RegionInfo(ctypes.Structure):
        _fields_ = [
            ("protection", ctypes.c_uint32 * 4),
            ("offset", ctypes.c_uint64),
            ("counts", ctypes.c_uint32 * 14),
            ("address", ctypes.c_uint64),
            ("size", ctypes.c_uint64),
        ]

    class ImageInfo(ctypes.Structure):
        _fields_ = [
            ("region", RegionInfo),
            ("device", ctypes.c_uint32),
            ("mode", ctypes.c_uint16),
            ("links", ctypes.c_uint16),
            ("inode", ctypes.c_uint64),
        ]

    dyld = ctypes.CDLL(None)
    dyld._dyld_get_image_header.argtypes = [ctypes.c_uint32]
    dyld._dyld_get_image_header.restype = ctypes.c_void_p
    header = dyld._dyld_get_image_header(0)
    if not header:
        raise RuntimeError("selected Python has no mapped executable image")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    result = ctypes.create_string_buffer(4096)
    length = libproc.proc_pidinfo(os.getpid(), 8, header, result, len(result))
    if length < ctypes.sizeof(ImageInfo):
        raise RuntimeError("selected Python executable image cannot be inspected")
    image = ImageInfo.from_buffer(result)
    return [image.device, image.inode]


def describe():
    if sys.implementation.name != "cpython":
        raise RuntimeError("native embedding requires CPython")

    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    root_name = "PYTHONFRAMEWORKPREFIX" if framework else "LIBDIR"
    root = sysconfig.get_config_var(root_name)
    library_name = sysconfig.get_config_var("LDLIBRARY")
    if not root or not library_name:
        raise RuntimeError(
            "selected Python has no shared embedding library configuration"
        )
    if Path(library_name).is_absolute():
        raise RuntimeError("selected Python returned an invalid shared library name")

    library = Path(root) / library_name
    if not library.is_absolute() or not library.is_file():
        raise RuntimeError(f"selected Python embedding library is missing: {library}")

    try:
        loaded = ctypes.CDLL(str(library))
    except OSError as error:
        raise RuntimeError(
            f"selected Python embedding library is unusable: {error}"
        ) from error
    # Match the CPython entrypoints loaded by library.rs. This check runs in
    # the child, before the caller can commit any process-lifetime runtime.
    for symbol in (
        "Py_IsInitialized",
        "Py_SetProgramName",
        "Py_SetPythonHome",
        "Py_InitializeEx",
        "PySys_SetArgv",
        "PyOS_setsig",
        "PyEval_SaveThread",
        "PyEval_RestoreThread",
        "PyGILState_Ensure",
        "PyGILState_Release",
        "PyImport_AddModule",
        "PyModule_GetDict",
        "PyDict_New",
        "PyDict_GetItemString",
        "PyDict_SetItemString",
        "PyRun_StringFlags",
        "PyObject_CallNoArgs",
        "PyObject_CallFunctionObjArgs",
        "PyUnicode_FromStringAndSize",
        "PyLong_AsLong",
        "Py_DecRef",
        "PyErr_Fetch",
        "PyErr_NormalizeException",
        "PyErr_Display",
        "PyErr_Clear",
        "PyErr_Print",
        "PyException_SetTraceback",
    ):
        try:
            getattr(loaded, symbol)
        except AttributeError as error:
            raise RuntimeError(
                f"selected Python embedding library is missing {symbol}: {library}"
            ) from error

    return {
        "executable": sys.executable,
        "image_identity": executable_image_identity(),
        "libpython": str(library),
        "prefix": sys.prefix,
        "exec_prefix": sys.exec_prefix,
        "base_prefix": sys.base_prefix,
        "base_exec_prefix": sys.base_exec_prefix,
    }


with open(sys.argv[1], "w", encoding="utf-8") as result:
    json.dump(describe(), result)
