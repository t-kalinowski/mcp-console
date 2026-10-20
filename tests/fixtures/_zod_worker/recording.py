"""Image formats and capture snapshots requested over the worker sideband."""

import json

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
    for image in bundle["images"]:
        send(context.writer, {"kind": "export_image"})
        response = json.loads(context.reader.readline())
        assert response["kind"] == "session_image_exported", response
        image["data"] = response["result"]["Ok"]
    send_output(context.writer, json.dumps(bundle) + "\n")
    send(context.writer, {"kind": "completed"})


def emit_export_image(context: WorkerContext, source: str) -> None:
    import base64
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
    import base64
    import hashlib

    send(context.writer, {"kind": "export_session"})
    response = json.loads(context.reader.readline())
    assert response["kind"] == "session_exported", response
    bundle = response["result"]["Ok"]
    digest = hashlib.sha256()
    total = 0
    for image in bundle["images"]:
        send(context.writer, {"kind": "export_image"})
        response = json.loads(context.reader.readline())
        assert response["kind"] == "session_image_exported", response
        data = base64.b64decode(response["result"]["Ok"], validate=True)
        digest.update(data)
        total += len(data)
    send_output(
        context.writer,
        json.dumps(
            {
                "images": len(bundle["images"]),
                "bytes": total,
                "sha256": digest.hexdigest(),
            }
        )
        + "\n",
    )
    send(context.writer, {"kind": "completed"})
