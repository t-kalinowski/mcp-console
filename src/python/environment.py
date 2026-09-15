"""Console-owned Python activation, input, and cell services."""

import __main__
import builtins
import copy
import importlib
import importlib.metadata
import json
import os
import re
import signal
import site
import sys
import threading
from typing import TextIO

import _mcp_console as runtime
import _mcp_console_native as native

_manifest = None
_discovery = None
_disabled_reason = None
_configured_imports = set()
_original_import = builtins.__import__


class _ConsoleStream:
    """Keep text and managed input notices on the same ordered worker stream."""

    def __init__(self, target: TextIO, channel: str) -> None:
        self.target = target
        self.channel = channel
        self.process = os.getpid()
        self.thread = threading.get_ident()
        self.tty = target.isatty()

    def _on_worker_thread(self) -> bool:
        return os.getpid() == self.process and threading.get_ident() == self.thread

    def __getattr__(self, name):
        return getattr(self.target, name)

    def write(self, text: str) -> int:
        if not self._on_worker_thread() or not isinstance(text, str):
            return self.target.write(text)
        call("output", {"channel": self.channel, "text": text})
        return len(text)

    def isatty(self) -> bool:
        return self.tty if self._on_worker_thread() else self.target.isatty()

    def flush(self) -> None:
        if not self._on_worker_thread() or not self.target.closed:
            self.target.flush()

    def close(self) -> None:
        if not self._on_worker_thread():
            self.target.close()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def call(operation, payload=None):
    return json.loads(
        native.call(json.dumps({"operation": operation, "payload": payload}))
    )


def _input(prompt=""):
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return call("input", str(prompt))
    except RuntimeError as error:
        if str(error) == "KeyboardInterrupt":
            raise KeyboardInterrupt from None
        if str(error) == "EOFError":
            raise EOFError from None
        raise


def _interrupt(signum, frame):
    if call("interrupt"):
        raise KeyboardInterrupt


def connect_interrupts(request):
    signal.signal(signal.SIGINT, _interrupt)
    return "null"


class _LazyR:
    @staticmethod
    def _target():
        call("interop")
        return __main__.r

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)

    def __getitem__(self, name):
        return self._target()[name]

    def __setitem__(self, name, value):
        self._target()[name] = value


def _import(*args, **kwargs):
    result = _original_import(*args, **kwargs)
    for name in ("numpy", "pandas", "matplotlib.pyplot"):
        module = sys.modules.get(name)
        if module is None or name in _configured_imports:
            continue
        if getattr(getattr(module, "__spec__", None), "_initializing", False):
            continue
        _configured_imports.add(name)
        if name == "numpy":
            module.set_printoptions(linewidth=200)
        elif name == "pandas":
            module.set_option("display.width", 200)
        else:
            runtime.disable_matplotlib_show()
    return result


def initialize(request):
    global _manifest, _discovery, _disabled_reason
    config = json.loads(request)
    _manifest = config["manifest"]
    _discovery = config["discovery"]
    _disabled_reason = config["disabled_reason"]
    _activate(_discovery)
    sys.path.insert(0, "")
    connect_interrupts(None)
    builtins.input = _input
    builtins.r = _LazyR()
    builtins.__import__ = _import
    runtime.configure_import_resolution(
        None if _manifest is None else _resolve_import,
        _disabled_reason if _manifest is None else None,
    )
    if _manifest is not None:
        call("activate_python", _manifest)
    return "null"


def _activate(discovery):
    previous = set(_discovery["site_packages"]) if _discovery is not None else set()
    selected = set(discovery["site_packages"])
    paths = list(sys.path)
    prefix, exec_prefix, executable = sys.prefix, sys.exec_prefix, sys.executable
    try:
        sys.path[:] = [path for path in sys.path if path not in previous - selected]
        for path in discovery["site_packages"]:
            if path not in sys.path:
                site.addsitedir(path)
        sys.prefix = sys.exec_prefix = discovery["prefix"]
        runtime.activate_process_environment(discovery["executable"])
        importlib.invalidate_caches()
    except BaseException:
        sys.path[:] = paths
        sys.prefix, sys.exec_prefix = prefix, exec_prefix
        runtime.activate_process_environment(executable)
        raise


def _name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize(manifest):
    packages = set(manifest["packages"])
    defaults = [name for name in ("numpy", "pandas") if name in packages]
    result = {"packages": defaults + sorted(packages - set(defaults))}
    if manifest.get("python_version"):
        result["python_version"] = sorted(set(manifest["python_version"]))
    if manifest.get("exclude_newer") is not None:
        result["exclude_newer"] = manifest["exclude_newer"]
    return result


def _check_compatible(discovery):
    if (
        discovery["libpython"] != _discovery["libpython"]
        or discovery["version"] != _discovery["version"]
    ):
        raise RuntimeError(
            "New environment does not use the same Python binary; restart with the requested Python version"
        )
    candidate = {
        _name(name): version for name, version in discovery["distributions"].items()
    }
    packages = importlib.metadata.packages_distributions()
    loaded = {name.partition(".")[0] for name in sys.modules}
    for root in loaded:
        for package in packages.get(root, ()):
            version = importlib.metadata.version(package)
            selected = candidate.get(_name(package))
            if selected != version:
                raise RuntimeError(
                    f"Cannot replace loaded {package} {version} with {selected or 'an absent distribution'}. "
                    "Restart with compatible requirements; the running interpreter and objects are unchanged."
                )


def _prepare(candidate, import_resolution=None):
    global _manifest, _discovery
    if _manifest is None:
        raise RuntimeError(_disabled_reason)
    candidate = _normalize(candidate)
    if candidate == _manifest:
        return
    if not set(_manifest["packages"]) <= set(candidate["packages"]):
        raise RuntimeError(
            "Python requirements are additive after interpreter activation"
        )
    requirements = copy.deepcopy(candidate)
    requirements["python_version"] = list(candidate.get("python_version", [])) + [
        "==" + ".".join(map(str, _discovery["version"]))
    ]
    request = {"requirements": requirements, "retained_requirements": candidate}
    if import_resolution is not None:
        request["import_resolution"] = import_resolution
    executable = call("resolve_python", request)
    discovery = call("discover_python", executable)
    _check_compatible(discovery)
    _activate(discovery)
    _discovery = discovery
    _manifest = candidate
    call("activate_python", _manifest)


def _candidate(packages):
    candidate = copy.deepcopy(_manifest)
    if candidate is None:
        raise RuntimeError(_disabled_reason)
    candidate["packages"] = sorted(set(candidate["packages"]) | set(packages))
    return candidate


def _resolve_import(module, distribution):
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        resolution = (
            {"module": module, "distribution": distribution}
            if module != distribution
            else None
        )
        _prepare(_candidate([distribution]), resolution)
        return json.dumps({"kind": "ready"})
    except Exception as error:
        return json.dumps({"kind": "failed", "message": str(error)})


def prepare(request):
    try:
        request = json.loads(request)
        candidate = request.get("manifest")
        if candidate is None:
            candidate = _candidate(request["packages"])
        runtime.without_automatic_resolution(_prepare, candidate)
        return json.dumps({"kind": "prepared"})
    except BaseException as error:
        return json.dumps(
            {"kind": "failed", "message": str(error) or type(error).__name__}
        )


def state(request):
    return json.dumps({"manifest": _manifest, "discovery": _discovery})


def evaluate(request):
    request = json.loads(request)
    runtime.eval_cell(request["source"], request["filename"])
    for image in runtime.take_images():
        call("plot", image)
    return "null"


def connect_streams(request):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(line_buffering=True, write_through=True)
    sys.stdout = _ConsoleStream(sys.stdout, "output")
    sys.stderr = _ConsoleStream(sys.stderr, "diagnostic")
    return "null"
