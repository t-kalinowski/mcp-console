# MCP Console's private Python runtime.
import __main__ as _main
import ast as _ast
import base64 as _base64
import builtins as _builtins
import io as _io
import logging as _logging
import sys as _sys
import traceback as _traceback
import types as _types


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


_mcp_console = _types.ModuleType("_mcp_console")


def _mcp_console_dispatch(state=_mcp_console.__dict__):
    operation = state.pop("operation")
    arguments = state.pop("arguments")
    return state[operation](*arguments)


_mcp_console.disable_matplotlib_show = _mcp_console_disable_matplotlib_show
_mcp_console.eval_cell = _mcp_console_eval_cell
_mcp_console.take_images = _mcp_console_take_images
_mcp_console.dispatch = _mcp_console_dispatch
_sys.modules[_mcp_console.__name__] = _mcp_console
_builtins.__dict__["_mcp_console_dispatch"] = _mcp_console_dispatch
