# MCP Console's private Python DB-API SQL backend.
import builtins as _builtins

_PREVIEW_ROWS = 20
_PREVIEW_COLUMNS = 12
_CELL_WIDTH = 40

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
        return "NULL"

    text = repr(value)
    if len(text) > _CELL_WIDTH:
        return text[: _CELL_WIDTH - 1] + "…"
    return text


def _fallback_show(cursor):
    description = cursor.description or ()
    total_columns = len(description)
    if total_columns == 0:
        return

    columns = min(total_columns, _PREVIEW_COLUMNS)
    names = [str(description[index][0]) for index in range(columns)]
    fetched = list(cursor.fetchmany(_PREVIEW_ROWS + 1))
    rows = fetched[:_PREVIEW_ROWS]

    rendered = [
        [_display_cell(row[index]) for index in range(columns)]
        for row in rows
    ]
    widths = []
    for index in range(columns):
        width = len(names[index])
        for row in rendered:
            width = max(width, len(row[index]))
        widths.append(min(_CELL_WIDTH, width))

    def line(values):
        return " | ".join(
            values[index].ljust(widths[index])
            for index in range(columns)
        ).rstrip()

    print(line(names))
    print("-+-".join("-" * width for width in widths))
    for row in rendered:
        print(line(row))

    if not rows:
        print("[0 rows]")
    if len(fetched) > _PREVIEW_ROWS:
        print("[additional rows omitted]")
    omitted = total_columns - columns
    if omitted:
        suffix = "column" if omitted == 1 else "columns"
        print(f"[{omitted} additional {suffix} omitted]")


def _duckdb_show(result, cursor):
    seen = set()
    for candidate in (result, cursor):
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)

        show = getattr(candidate, "show", None)
        if not callable(show):
            continue
        try:
            show(max_rows=_PREVIEW_ROWS)
        except TypeError:
            continue
        return True
    return False


def evaluate(source):
    connection = _connection
    if connection is None:
        raise RuntimeError("no Python DB-API connection is selected")

    cursor = None
    try:
        cursor = connection.cursor()
        result = cursor.execute(source)
        result_cursor = (
            result
            if getattr(result, "description", None) is not None
            else cursor
        )
        if getattr(result_cursor, "description", None) is None:
            return None
        if not _duckdb_show(result, result_cursor):
            _fallback_show(result_cursor)
    except Exception as error:
        print(f"Error: {error}")
    finally:
        if cursor is not None and cursor is not connection:
            close = getattr(cursor, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    return None


_builtins.console_sql_connection = console_sql_connection
