# MCP Console's private Python DB-API SQL backend.
import builtins as _builtins
import traceback as _traceback
import unicodedata as _unicodedata

_PREVIEW_ROWS = 20
_PREVIEW_COLUMNS = 12
_CELL_WIDTH = 160
_PREVIEW_WIDTH = 200
_RESPONSE_BYTES = 12 * 1024

try:
    _connection
except NameError:
    _connection = None

try:
    _restore_managed
except NameError:
    _restore_managed = False


def _validate_connection(connection):
    cursor = getattr(connection, "cursor", None)
    if not callable(cursor):
        raise TypeError(
            "`connection` must provide a callable cursor() method or be None"
        )


def console_sql_connection(connection=None):
    global _connection, _restore_managed

    if connection is None:
        _connection = None
        _restore_managed = True
        return None

    _validate_connection(connection)
    _connection = connection
    _restore_managed = False
    return None


def use_r():
    global _connection, _restore_managed

    _connection = None
    _restore_managed = False
    return None


def has_connection():
    return _connection is not None


def restore_managed_requested():
    return _restore_managed


def _display_cell(value):
    if value is None:
        return "NULL", False

    text = repr(value)
    if len(text) > _CELL_WIDTH:
        return text[: _CELL_WIDTH - 1] + "…", True
    return text, False


def _display_name(value):
    text = "".join(
        character if character.isprintable() else repr(character)[1:-1]
        for character in str(value)
    )
    if len(text) > _CELL_WIDTH:
        return text[: _CELL_WIDTH - 1] + "…"
    return text


def _fit(text, width):
    if _display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    return _slice_to_width(text, width - 1) + "…"


def _character_width(character):
    if _unicodedata.combining(character):
        return 0
    if _unicodedata.category(character) in {"Cf", "Me"}:
        return 0
    if _unicodedata.east_asian_width(character) in {"F", "W"}:
        return 2
    return 1


def _display_width(text):
    return sum(_character_width(character) for character in text)


def _slice_to_width(text, width):
    result = []
    used = 0
    for character in text:
        character_width = _character_width(character)
        if used + character_width > width:
            break
        result.append(character)
        used += character_width
    return "".join(result)


def _pad(text, width):
    return text + " " * (width - _display_width(text))


def _column_widths(names, rows, columns):
    widths = []
    for index in range(columns):
        width = _display_width(names[index])
        for row in rows:
            width = max(width, _display_width(row[index][0]))
        widths.append(min(_CELL_WIDTH, max(1, width)))

    available = _PREVIEW_WIDTH - 3 * (columns - 1)
    while sum(widths) > available:
        widest = max(range(columns), key=widths.__getitem__)
        widths[widest] -= 1
    return widths


def _format_preview(
    names,
    rows,
    total_columns,
    fetched_rows,
    more_rows,
    visible_rows,
    visible_columns,
):
    widths = _column_widths(names, rows[:visible_rows], visible_columns)

    def line(values):
        return " | ".join(
            _pad(_fit(values[index], widths[index]), widths[index])
            for index in range(visible_columns)
        ).rstrip()

    lines = [
        line(names),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(line([cell[0] for cell in row]) for row in rows[:visible_rows])

    if fetched_rows == 0:
        lines.append("[0 rows]")
    if more_rows or visible_rows < fetched_rows:
        lines.append("[additional rows omitted]")
    omitted = total_columns - visible_columns
    if omitted:
        suffix = "column" if omitted == 1 else "columns"
        lines.append(f"[{omitted} additional {suffix} omitted]")
    if any(cell[1] for row in rows[:visible_rows] for cell in row[:visible_columns]):
        lines.append(f"[cell values truncated to {_CELL_WIDTH} characters]")
    return "\n".join(lines)


def _fallback_show(cursor):
    description = cursor.description or ()
    total_columns = len(description)
    if total_columns == 0:
        return

    columns = min(total_columns, _PREVIEW_COLUMNS)
    names = [_display_name(description[index][0]) for index in range(columns)]
    fetched = list(cursor.fetchmany(_PREVIEW_ROWS + 1))
    values = fetched[:_PREVIEW_ROWS]
    rows = [[_display_cell(row[index]) for index in range(columns)] for row in values]
    visible_rows = len(rows)
    visible_columns = columns

    while True:
        output = _format_preview(
            names,
            rows,
            total_columns,
            len(values),
            len(fetched) > _PREVIEW_ROWS,
            visible_rows,
            visible_columns,
        )
        if len(output.encode("utf-8")) + 1 <= _RESPONSE_BYTES:
            print(output)
            return
        if visible_rows > 0:
            visible_rows -= 1
        elif visible_columns > 1:
            visible_columns -= 1
        else:
            raise RuntimeError("SQL preview cannot fit within the response budget")


def evaluate(source):
    connection = _connection
    if connection is None:
        raise RuntimeError("no Python DB-API connection is selected")

    executor = None
    result = None
    try:
        execute = getattr(connection, "execute", None)
        executor = connection if callable(execute) else connection.cursor()
        result = executor.execute(source)
        result_cursor = (
            result if getattr(result, "description", None) is not None else executor
        )
        if getattr(result_cursor, "description", None) is None:
            return None
        _fallback_show(result_cursor)
    except Exception as error:
        print(f"Error: {error}")
    except BaseException:
        _traceback.print_exc()
    finally:
        seen = set()
        for candidate in (result, executor):
            if candidate is None or candidate is connection or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            try:
                close = getattr(candidate, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            except BaseException:
                _traceback.print_exc()
    return None


_builtins.console_sql_connection = console_sql_connection
