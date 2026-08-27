from __future__ import annotations

import json
import os
import shutil
import sysconfig
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

Command = Union[str, os.PathLike]


def executable() -> str:
    executable_name = "mcp-console.exe" if os.name == "nt" else "mcp-console"
    scripts = sysconfig.get_path("scripts")
    if scripts is not None:
        candidate = Path(scripts) / executable_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    candidate = shutil.which("mcp-console")
    if candidate is not None:
        return candidate

    raise FileNotFoundError(
        "Could not find the mcp-console executable. Install mcp-console into "
        "this Python environment, or pass command= and args= explicitly."
    )


def stdio_command(
    command: Optional[Command], args: Optional[Sequence[Command]]
) -> Tuple[str, List[str]]:
    resolved_command = executable() if command is None else os.fspath(command)
    resolved_args = ["serve"] if args is None else [os.fspath(arg) for arg in args]
    return resolved_command, resolved_args


def field(value: Any, snake_case: str, camel_case: str) -> Any:
    if isinstance(value, Mapping):
        if snake_case in value:
            return value[snake_case]
        return value.get(camel_case)
    if hasattr(value, snake_case):
        return getattr(value, snake_case)
    return getattr(value, camel_case, None)


def as_json(value: Any) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value)
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(by_alias=True, exclude_none=True))
    if hasattr(value, "dict"):
        return json.dumps(value.dict(by_alias=True, exclude_none=True))
    return str(value)


def content_text(content: Any) -> str:
    content_type = field(content, "type", "type")
    if content_type == "text":
        return str(field(content, "text", "text") or "")
    if content_type == "image":
        mime_type = field(content, "mime_type", "mimeType") or "image"
        return f"[{mime_type} output]"
    return as_json(content)


def result_text(result: Any) -> str:
    content = field(result, "content", "content") or []
    parts = [content_text(item) for item in content]
    if not parts:
        structured = field(result, "structured_content", "structuredContent")
        if structured is not None:
            parts.append(json.dumps(structured))
    text = "\n".join(part for part in parts if part)
    if field(result, "is_error", "isError"):
        raise RuntimeError(text or "MCP Console returned an error")
    return text


def openai_result_output(result: Any) -> Union[str, List[Dict[str, Any]]]:
    """Convert an MCP result into a Responses function-call output value."""
    error_text = result_text(result)
    content = field(result, "content", "content") or []
    if not content:
        return error_text

    output: List[Dict[str, Any]] = []
    for item in content:
        content_type = field(item, "type", "type")
        if content_type == "text":
            output.append(
                {
                    "type": "input_text",
                    "text": str(field(item, "text", "text") or ""),
                }
            )
        elif content_type == "image":
            mime_type = field(item, "mime_type", "mimeType") or "image/png"
            data = field(item, "data", "data")
            if data:
                output.append(
                    {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": f"data:{mime_type};base64,{data}",
                    }
                )
            else:
                output.append(
                    {"type": "input_text", "text": f"[{mime_type} output]"}
                )
        else:
            output.append({"type": "input_text", "text": as_json(item)})

    if len(output) == 1 and output[0]["type"] == "input_text":
        return output[0]["text"]
    return output
