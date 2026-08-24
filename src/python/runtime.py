# MCP Console's private Python runtime.
import __main__ as _main
import ast as _ast
import base64 as _base64
import builtins as _builtins
import importlib as _importlib
import importlib.util as _importlib_util
import io as _io
import json as _json
import logging as _logging
import os as _os
import sys as _sys
import threading as _threading
import traceback as _traceback
import types as _types


_MCP_CONSOLE_IMPORT_DISTRIBUTIONS = {
    "PIL": "pillow",
    "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome",
    "_cffi_backend": "cffi",
    "attr": "attrs",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "docx": "python-docx",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "pptx": "python-pptx",
    "serial": "pyserial",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "yaml12": "py-yaml12",
}

_MCP_CONSOLE_AMBIGUOUS_IMPORT_ROOTS = {
    "azure",
    "backports",
    "google",
    "opentelemetry",
    "zope",
}

_MCP_CONSOLE_DEFAULT_IMPORT_ROOTS = {"numpy", "pandas"}


def _mcp_console_missing_module(fullname, message):
    raise ModuleNotFoundError(message, name=fullname) from None


def _mcp_console_missing_submodule(fullname, message):
    raise ImportError(message, name=fullname) from None


def _mcp_console_explicit_requirement(distribution):
    return f'requirements: {{"python": ["{distribution}"]}}'


class _McpConsoleImportFinder:
    _mcp_console_import_finder = True

    def __init__(
        self,
        importlib,
        importlib_util,
        json,
        os,
        sys,
        threading,
        distributions,
        ambiguous_roots,
        default_roots,
        missing_module,
        missing_submodule,
        explicit_requirement,
    ):
        self._importlib = importlib
        self._importlib_util = importlib_util
        self._json = json
        self._os = os
        self._sys = sys
        self._threading = threading
        self._distributions = distributions
        self._ambiguous_roots = ambiguous_roots
        self._default_roots = default_roots
        self._missing_module = missing_module
        self._missing_submodule = missing_submodule
        self._explicit_requirement = explicit_requirement
        self._fromlist_code = importlib._bootstrap._handle_fromlist.__code__
        self._callback = None
        self._disabled_reason = "automatic Python package resolution is not configured"
        self._pid = None
        self._thread = None
        self._state = threading.local()

    def configure(self, callback, disabled_reason):
        self._callback = callback
        self._disabled_reason = disabled_reason
        self._pid = self._os.getpid()
        self._thread = self._threading.get_ident()
        return None

    def _find_spec(self, finders, fullname, path, target):
        for finder in tuple(finders):
            if finder is self:
                continue
            if hasattr(finder, "find_spec"):
                specification = finder.find_spec(fullname, path, target)
            elif self._sys.version_info < (3, 12):
                find_module = getattr(finder, "find_module", None)
                loader = None if find_module is None else find_module(fullname, path)
                specification = (
                    None
                    if loader is None
                    else self._importlib_util.spec_from_loader(fullname, loader)
                )
            else:
                # Python 3.12 and later ignore legacy-only meta-path finders.
                specification = None
            if specification is not None:
                return specification
        return None

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if getattr(self._state, "resolving", False):
            return None
        # Availability probes must observe the environment without changing it.
        # A real import can resolve the distribution if the caller proceeds.
        if self._is_availability_probe():
            return None
        # The default packages probe for optional dependencies while importing.
        # Keep those probes from changing the managed environment merely because
        # a user imported an already-available default package.
        if self._is_default_package_initialization():
            return None
        # Some libraries append importers after Python initializes. Give only
        # that later suffix its ordinary chance before acting as the last finder.
        finders = self._sys.meta_path
        position = finders.index(self)
        specification = self._find_spec(
            finders[position + 1 :],
            fullname,
            path,
            target,
        )
        if specification is not None:
            return specification
        if self._callback is None:
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n{self._disabled_reason}",
            )
        if self._pid != self._os.getpid():
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n"
                "MCP Console automatic package resolution is available only in the "
                "main worker process. Prepare the distribution through "
                "`requirements.python` in the parent before starting the child, for "
                "example:\n\n" + self._explicit_requirement("distribution-name"),
            )
        if self._thread != self._threading.get_ident():
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n"
                "MCP Console automatic package resolution is available only on the "
                "configuring thread for the Python worker. Prepare the distribution "
                "through `requirements.python` before starting the background thread, "
                "for example:\n\n" + self._explicit_requirement("distribution-name"),
            )
        if fullname != root and root in self._sys.modules:
            missing = (
                self._missing_submodule
                if self._is_fromlist_import(fullname)
                else self._missing_module
            )
            missing(
                fullname,
                f"No module named {fullname!r}.\n\n"
                f"MCP Console did not resolve the top-level import {root!r} again "
                "because it is already present. The missing submodule may require an "
                "optional extra or a different distribution. Pass the correct "
                "distribution through `requirements.python` in the `send` call, for "
                "example:\n\n" + self._explicit_requirement("distribution-name"),
            )
        if root in self._sys.stdlib_module_names:
            self._missing_module(
                fullname,
                f"No module named {fullname!r}.\n\n"
                f"{root!r} is a Python standard-library module, but it is unavailable "
                "in the selected Python build. MCP Console did not try to install a "
                "same-named PyPI distribution.",
            )
        if root in self._ambiguous_roots:
            self._raise_unsafe_inference(fullname, root)

        distribution = self._distributions.get(root)
        if distribution is None:
            safe = (
                root.isascii()
                and root.isidentifier()
                and root[0].isalnum()
                and root[-1].isalnum()
            )
            if not safe:
                self._raise_unsafe_inference(fullname, root)
            distribution = root

        self._state.resolving = True
        try:
            try:
                response = self._json.loads(self._callback(root, distribution))
            except Exception as error:
                self._raise_resolution_failure(fullname, root, distribution, str(error))

            kind = response.get("kind") if isinstance(response, dict) else None
            if kind == "failed" and isinstance(response.get("message"), str):
                self._raise_resolution_failure(
                    fullname,
                    root,
                    distribution,
                    response["message"],
                )
            if kind == "disabled" and isinstance(response.get("message"), str):
                self._missing_module(
                    fullname,
                    f"No module named {fullname!r}.\n\n{response['message']}",
                )
            if kind != "ready":
                raise RuntimeError("invalid automatic Python package resolver response")

            self._importlib.invalidate_caches()
            specification = self._find_spec(
                self._sys.meta_path,
                fullname,
                path,
                target,
            )
            if specification is not None:
                return specification
        finally:
            self._state.resolving = False

        self._missing_module(
            fullname,
            f"No module named {fullname!r}.\n\n"
            f"MCP Console prepared the inferred PyPI distribution `{distribution}`, "
            f"but it did not provide the import `{fullname}`.\n\n"
            "Pass the correct distribution through `requirements.python` in the "
            "`send` call:\n\n"
            + self._explicit_requirement("correct-distribution-name"),
        )

    def _is_default_package_initialization(self):
        frame = self._sys._getframe(1)
        while frame is not None:
            specification = frame.f_globals.get("__spec__")
            module = frame.f_globals.get("__name__", "")
            root = module.partition(".")[0]
            if root in self._default_roots and getattr(
                specification, "_initializing", False
            ):
                return True
            frame = frame.f_back
        return False

    def _is_availability_probe(self):
        probe_code = getattr(self._importlib_util.find_spec, "__code__", None)
        frame = self._sys._getframe(1)
        while frame is not None:
            if frame.f_code is probe_code:
                return True
            frame = frame.f_back
        return False

    def _is_fromlist_import(self, fullname):
        frame = self._sys._getframe(1)
        while frame is not None:
            if (
                frame.f_code is self._fromlist_code
                and frame.f_locals.get("from_name") == fullname
            ):
                return True
            frame = frame.f_back
        return False

    def _raise_unsafe_inference(self, fullname, root):
        self._missing_module(
            fullname,
            f"No module named {fullname!r}.\n\n"
            "MCP Console could not safely infer a PyPI distribution for the missing "
            f"import `{root}`.\n\n"
            "Pass the distribution through `requirements.python` in the `send` call, "
            "for example:\n\n" + self._explicit_requirement("distribution-name"),
        )

    def _raise_resolution_failure(self, fullname, root, distribution, diagnostic):
        self._missing_module(
            fullname,
            f"No module named {fullname!r}.\n\n"
            f"MCP Console inferred the PyPI distribution `{distribution}` from the "
            f"import `{root}`, but automatic package resolution failed:\n\n"
            f"{diagnostic}\n\n"
            "Import names and PyPI distribution names can differ. Retry with the "
            "correct distribution declared through `requirements.python` in the "
            "`send` call, for example:\n\n" + self._explicit_requirement(distribution),
        )


_mcp_console_import_finder = None
for _mcp_console_finder in _sys.meta_path:
    if _builtins.getattr(_mcp_console_finder, "_mcp_console_import_finder", False):
        _mcp_console_import_finder = _mcp_console_finder
        break
if _mcp_console_import_finder is None:
    _mcp_console_import_finder = _McpConsoleImportFinder(
        _importlib,
        _importlib_util,
        _json,
        _os,
        _sys,
        _threading,
        _MCP_CONSOLE_IMPORT_DISTRIBUTIONS,
        _MCP_CONSOLE_AMBIGUOUS_IMPORT_ROOTS,
        _MCP_CONSOLE_DEFAULT_IMPORT_ROOTS,
        _mcp_console_missing_module,
        _mcp_console_missing_submodule,
        _mcp_console_explicit_requirement,
    )
    _sys.meta_path.append(_mcp_console_import_finder)


class _McpConsoleMatplotlibLogFilter(_logging.Filter):
    _mcp_console_filter = True

    def filter(self, record):
        return record.getMessage() != (
            "Matplotlib is building the font cache; this may take a moment."
        )


_mcp_console_logger = _logging.getLogger("matplotlib.font_manager")
_mcp_console_filter_installed = False
for _mcp_console_filter in _mcp_console_logger.filters:
    if _builtins.getattr(_mcp_console_filter, "_mcp_console_filter", False):
        _mcp_console_filter_installed = True
        break
if not _mcp_console_filter_installed:
    _mcp_console_logger.addFilter(_McpConsoleMatplotlibLogFilter())

_mcp_console_image_state = [()]


def _mcp_console_disable_matplotlib_show(
    _setattr=_builtins.setattr,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is not None:
        _setattr(pyplot, "show", lambda *args, **kwargs: None)
    return None


def _mcp_console_collect_plots(
    _BaseException=_builtins.BaseException,
    _base64=_base64,
    _io=_io,
    _print_exc=_traceback.print_exc,
    _sorted=_builtins.sorted,
    _sys=_sys,
):
    pyplot = _sys.modules.get("matplotlib.pyplot")
    if pyplot is None:
        return ()

    images = []
    try:
        for number in _sorted(pyplot.get_fignums()):
            if number not in pyplot.get_fignums():
                continue
            try:
                figure = pyplot.figure(number)
                output = _io.BytesIO()
                figure.savefig(output, format="png")
                images.append(_base64.b64encode(output.getvalue()).decode("ascii"))
            except _BaseException:
                _print_exc()
    finally:
        try:
            pyplot.close("all")
        except _BaseException:
            _print_exc()
    return tuple(images)


def _mcp_console_eval_cell(
    source,
    filename,
    _main=_main,
    _parse=_ast.parse,
    _Expr=_ast.Expr,
    _Expression=_ast.Expression,
    _isinstance=_builtins.isinstance,
    _compile=_builtins.compile,
    _exec=_builtins.exec,
    _eval=_builtins.eval,
    _BaseException=_builtins.BaseException,
    _collect_plots=_mcp_console_collect_plots,
    _image_state=_mcp_console_image_state,
    _sys=_sys,
    _print_exc=_traceback.print_exc,
):
    try:
        module = _parse(source, filename=filename, mode="exec")
        final = module.body[-1] if module.body else None
        if _isinstance(final, _Expr):
            module.body.pop()
            statements = _compile(module, filename, "exec") if module.body else None
            expression = _compile(_Expression(final.value), filename, "eval")
        else:
            statements = _compile(module, filename, "exec")
            expression = None

        if statements is not None:
            _exec(statements, _main.__dict__)
        if expression is not None:
            _sys.displayhook(_eval(expression, _main.__dict__))
    except _BaseException:
        _print_exc()
    try:
        _image_state[0] = _collect_plots()
    except _BaseException:
        _print_exc()
        _image_state[0] = ()
    return None


def _mcp_console_take_images(_image_state=_mcp_console_image_state):
    images = _image_state[0]
    _image_state[0] = ()
    return images


def _mcp_console_apply_psutil_process_group(
    _callable=_builtins.callable,
    _getattr=_builtins.getattr,
    _import_module=_importlib.import_module,
    _find_spec=_importlib_util.find_spec,
    _os=_os,
    _sys=_sys,
):
    psutil = _sys.modules.get("psutil")
    specification = (
        _find_spec("psutil") if psutil is None else _getattr(psutil, "__spec__", None)
    )
    if specification is None or specification.origin is None:
        return None
    metadata = _import_module("importlib.metadata")
    try:
        distribution = metadata.distribution("psutil")
    except metadata.PackageNotFoundError:
        return None
    resolved = _os.path.realpath(specification.origin)
    installed = _os.path.realpath(
        _os.fspath(distribution.locate_file("psutil/__init__.py"))
    )
    if resolved != installed:
        return None
    if psutil is None:
        psutil = _import_module("psutil")
    platform = _getattr(psutil, "_psplatform", None)
    pids = _getattr(platform, "pids", None)
    if not _callable(pids) or _getattr(pids, "_mcp_console_sandbox", False):
        return None

    ctypes = _import_module("ctypes")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    list_process_group = libproc.proc_listpgrppids
    list_process_group.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    list_process_group.restype = ctypes.c_int
    process_group = _os.getpgrp()

    def process_group_ids():
        capacity = 16
        while True:
            buffer = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = list_process_group(
                process_group,
                buffer,
                ctypes.sizeof(buffer),
            )
            if count <= 0:
                error = ctypes.get_errno()
                if error:
                    raise OSError(error, _os.strerror(error))
                if count < 0:
                    raise RuntimeError("process-group enumeration failed")
                return []
            if count < capacity:
                return buffer[:count]
            capacity *= 2

    process_group_ids._mcp_console_sandbox = True
    # Keep psutil's public wrapper so it retains its sorted-list contract. The
    # platform hook also survives activation during the first psutil import.
    platform.pids = process_group_ids
    return None


def _mcp_console_configure_psutil(
    _Exception=_builtins.Exception,
    _apply=_mcp_console_apply_psutil_process_group,
):
    # This adapter may run after reticulate has activated an environment, so
    # its optional probe and setup must not abort activation.
    try:
        _apply()
    except _Exception:
        pass
    return None


def _mcp_console_activate_process_environment(
    executable,
    _configure_psutil=_mcp_console_configure_psutil,
    _sys=_sys,
):
    _sys.executable = executable
    multiprocessing = _sys.modules.get("multiprocessing")
    if multiprocessing is not None:
        multiprocessing.set_executable(executable)
    _configure_psutil()
    return None


_mcp_console = _types.ModuleType("_mcp_console")


def _mcp_console_dispatch(state=_mcp_console.__dict__):
    operation = state.pop("operation")
    arguments = state.pop("arguments")
    return state[operation](*arguments)


_mcp_console.activate_process_environment = _mcp_console_activate_process_environment
_mcp_console.disable_matplotlib_show = _mcp_console_disable_matplotlib_show
_mcp_console.configure_import_resolution = _mcp_console_import_finder.configure
_mcp_console.eval_cell = _mcp_console_eval_cell
_mcp_console.take_images = _mcp_console_take_images
_mcp_console.dispatch = _mcp_console_dispatch
_sys.modules[_mcp_console.__name__] = _mcp_console
_builtins.__dict__["_mcp_console_dispatch"] = _mcp_console_dispatch
_mcp_console_configure_psutil()
