"""Image formats and capture snapshots requested over the worker sideband."""

import json
import base64
from collections.abc import Iterator

from .output import PNG_1X1
from .protocol import send, send_output
from .state import WorkerContext

# A one-pixel JPEG emitted by grDevices::jpeg(type = "cairo").
JPEG_1X1 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQgJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK"
    "+UP38//Z"
)


def emit_image_formats(context: WorkerContext, source: str) -> None:
    for mime_type, data in (("image/png", PNG_1X1), ("image/jpeg", JPEG_1X1)):
        send(context.writer, {"kind": "image", "data": data, "mime_type": mime_type})
    send(context.writer, {"kind": "completed"})


def export_session(context: WorkerContext, source: str) -> None:
    send(context.writer, {"kind": "export_session"})
    response = json.loads(context.reader.readline())
    assert response["kind"] == "session_exported", response
    assert set(response["result"]) == {"Ok"}, response
    bundle = response["result"]["Ok"]
    result = {"recording_directory": bundle["recording_directory"]}
    images = []
    for name in bundle["files"]:
        data = b"".join(export_chunks(context))
        if name == "session.qmd":
            result["qmd"] = data.decode()
        elif name == "session.md":
            result["markdown"] = data.decode()
        else:
            images.append(
                {
                    "name": name.removeprefix("images/"),
                    "data": base64.b64encode(data).decode(),
                }
            )
    result["images"] = images
    send_output(context.writer, json.dumps(result) + "\n")
    send(context.writer, {"kind": "completed"})


def emit_export_image(context: WorkerContext, source: str) -> None:
    import struct
    import zlib

    png = base64.b64decode(PNG_1X1)
    text = b"fixture\0" + b"x" * (512 * 1024)
    chunk = b"tEXt" + text
    image = (
        png[:-12]
        + struct.pack(">I", len(text))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
        + png[-12:]
    )
    send(
        context.writer,
        {
            "kind": "image",
            "mime_type": "image/png",
            "data": base64.b64encode(image).decode(),
        },
    )
    send(context.writer, {"kind": "completed"})


def export_image_summary(context: WorkerContext, source: str) -> None:
    import hashlib

    send(context.writer, {"kind": "export_session"})
    response = json.loads(context.reader.readline())
    assert response["kind"] == "session_exported", response
    bundle = response["result"]["Ok"]
    digest = hashlib.sha256()
    total = 0
    count = 0
    for name in bundle["files"]:
        image = name.startswith("images/")
        count += image
        for data in export_chunks(context):
            if image:
                digest.update(data)
                total += len(data)
    send_output(
        context.writer,
        json.dumps(
            {
                "images": count,
                "bytes": total,
                "sha256": digest.hexdigest(),
            }
        )
        + "\n",
    )
    send(context.writer, {"kind": "completed"})


def export_chunks(context: WorkerContext) -> Iterator[bytes]:
    while True:
        send(context.writer, {"kind": "export_chunk"})
        response = json.loads(context.reader.readline())
        assert response["kind"] == "session_chunk_exported", response
        assert set(response["result"]) == {"Ok"}, response
        data = response["result"]["Ok"]
        if data is None:
            return
        decoded = base64.b64decode(data, validate=True)
        assert 0 < len(decoded) <= 65536, len(decoded)
        yield decoded
