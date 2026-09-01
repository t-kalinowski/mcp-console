import os
import shutil
from pathlib import Path

from _support import FifoCheckpoint, McpClient, code, r_test_environment

from .coordination import release_fixture_checkpoint, wait_for_marker
from .processes import build_killpg_denial_interposer


LARGE_OUTPUT_SIZE = 2 * 1024 * 1024


PENDING_TEXT_BUDGET = 8 * 1024 * 1024


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


def record_resolved_r_library(environment: dict[str, str], directory: Path) -> None:
    real_ir = shutil.which("ir", path=environment.get("PATH"))
    assert real_ir is not None, "ir is required"
    identity = directory / "resolved-r-library"
    fake_bin = directory / "fixture-r-bin"
    fake_bin.mkdir()
    ir = fake_bin / "ir"
    ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              exec "$MCP_CONSOLE_TEST_REAL_IR" "$@"
            fi
            if [ -n "${MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE:-}" ] &&
              [ -e "$MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE" ]; then
              printf 'fixture R resolver failed\n' >&2
              exit 1
            fi
            library=$("$MCP_CONSOLE_TEST_REAL_IR" "$@")
            printf '%s' "$library" > "$MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"
            printf '%s' "$library"
            """),
        encoding="utf-8",
    )
    ir.chmod(0o755)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
    environment["MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"] = str(identity)


def expose_idle_input_request(client: McpClient, temporary_path: Path) -> None:
    requested = client._start_send(r="request input while idle")
    completed = wait_for_marker(
        temporary_path,
        "zod-idle-input-cell-completed",
        client,
    )
    client._receive(requested)
    assert last_tool_text(client) == "[done]"

    release_fixture_checkpoint(completed.parent / "zod-release-idle-input-request")
    wait_for_marker(
        temporary_path,
        "zod-idle-input-request-processed",
        client,
    )
    client.send()
    assert last_tool_text(client) == (
        '[input requested: "idle> "]\n[waiting for stdin]'
    )


def resolver_interrupt_permission_environment(
    temporary_path: Path,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint, Path, Path]:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    fake_bin = temporary_path / "bin"
    fake_bin.mkdir()
    fake_ir = fake_bin / "ir"
    fake_ir.write_text(
        code(r"""
            #!/bin/sh

            set -eu
            if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
              printf 'ir 0.4.0\n'
              exit 0
            fi
            exec 3< "$MCP_CONSOLE_TEST_RESOLVER_LIFETIME"
            printf '%s\n' "$$" > "$MCP_CONSOLE_TEST_RESOLVER_GROUP"
            printf 1 > "$MCP_CONSOLE_TEST_RESOLVER_STARTED"
            IFS= read -r _ <&3
            """),
        encoding="utf-8",
    )
    fake_ir.chmod(0o755)

    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["TMPDIR"] = str(temporary_path)
    denied_interrupt = temporary_path / "resolver-sigint-denied"
    resolver_group = temporary_path / "resolver-group"
    resolver_started = FifoCheckpoint(temporary_path / "resolver-started")
    resolver_lifetime = FifoCheckpoint(temporary_path / "resolver-lifetime")
    environment["MCP_CONSOLE_TEST_DENIED_SIGINT"] = str(denied_interrupt)
    environment["MCP_CONSOLE_TEST_RESOLVER_GROUP"] = str(resolver_group)
    environment["MCP_CONSOLE_TEST_RESOLVER_STARTED"] = str(resolver_started.path)
    environment["MCP_CONSOLE_TEST_RESOLVER_LIFETIME"] = str(resolver_lifetime.path)
    # The interposer removes its loader variable after reaching the server, so
    # the resolver and Zod do not inherit it.
    environment["DYLD_INSERT_LIBRARIES"] = str(
        build_killpg_denial_interposer(temporary_path)
    )
    return (
        environment,
        resolver_started,
        resolver_lifetime,
        resolver_group,
        denied_interrupt,
    )


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    return result["content"][0]["text"]


def assert_large_output(output: str, prefix: str) -> None:
    expected = prefix + ("x" * LARGE_OUTPUT_SIZE)
    assert output.startswith(expected), (
        f"captured {len(output)} bytes without the complete {len(expected)}-byte payload"
    )
    barrier = output.removeprefix(expected)
    assert barrier and not barrier.strip("y"), "unexpected text after captured payload"


def large_output(prefix: str) -> str:
    return prefix + ("x" * LARGE_OUTPUT_SIZE) + ("y" * LARGE_OUTPUT_SIZE)


def remove_length_marker(output: str, marker_prefix: str) -> tuple[str, int]:
    marker_start = output.find(marker_prefix)
    assert marker_start >= 0, (
        f"raw output lost length marker {marker_prefix!r}: {output[-500:]!r}"
    )
    marker_end = output.find("\n", marker_start)
    if marker_end < 0:
        marker_end = len(output)
        after_marker = marker_end
    else:
        after_marker = marker_end + 1
    length = int(output[marker_start + len(marker_prefix) : marker_end])
    return output[:marker_start] + output[after_marker:], length


def expose_idle_sideband_output(
    client: McpClient,
    temporary_path: Path,
    marker: str | None = None,
) -> None:
    suffix = f"-{marker}" if marker else ""
    source = (
        f"start background sideband: {marker}"
        if marker
        else "start background sideband"
    )
    client.send(r=source)
    assert last_tool_text(client) == "[done]", repr(last_tool_text(client))
    started = wait_for_marker(
        temporary_path,
        f"zod-background-sideband-started{suffix}",
        client,
    )
    (started.parent / f"zod-release-background-sideband{suffix}").touch()
    wait_for_marker(
        temporary_path,
        f"zod-background-sideband-emitted{suffix}",
        client,
    )
