from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

STABLE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
PACKAGE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')
TARGET_ARCHITECTURES = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
    "aarch64-unknown-linux-gnu": "aarch64",
    "x86_64-unknown-linux-gnu": "x86_64",
}


class ReleaseError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def command_output(
    command: list[str], env: dict[str, str] | None = None, *, strip: bool = True
) -> str:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(f"{' '.join(command)} failed{suffix}")
    return result.stdout.strip() if strip else result.stdout


def run_command(command: list[str], env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, env=env, check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"{' '.join(command)} failed with status {result.returncode}"
        )


def package_version() -> str:
    in_package = False
    for line in Path("Cargo.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_package:
                break
            in_package = stripped == "[package]"
        elif in_package and (match := PACKAGE_VERSION.fullmatch(stripped)):
            return match.group(1)
    raise ReleaseError("Cargo.toml package version is missing")


def terminate(process: subprocess.Popen[bytes]) -> str:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    assert process.stderr is not None
    return process.stderr.read().decode(errors="replace").strip()


def receive(
    process: subprocess.Popen[bytes], buffer: bytearray, timeout_seconds: float
) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_seconds
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        require(
            remaining > 0, f"MCP response timed out after {timeout_seconds:g} seconds"
        )
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        require(
            bool(readable), f"MCP response timed out after {timeout_seconds:g} seconds"
        )
        chunk = os.read(process.stdout.fileno(), 65536)
        require(bool(chunk), "MCP server closed stdout before completing a response")
        buffer.extend(chunk)

    line, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    message = json.loads(line)
    require(isinstance(message, dict), "MCP response must be a JSON object")
    return message


def smoke_mcp(
    executable: Path,
    version: str,
    env: dict[str, str],
    startup_timeout: float,
    response_timeout: float,
) -> None:
    process = subprocess.Popen(
        [str(executable), "serve"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    buffer = bytearray()

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "wheel-smoke-test",
                        "version": "1.0.0",
                    },
                },
            }
        )
        initialization = receive(process, buffer, startup_timeout)
        require(initialization.get("id") == 1, "unexpected initialize response ID")
        require(
            initialization.get("result", {}).get("serverInfo")
            == {"name": "mcp-console", "version": version},
            "unexpected initialize serverInfo",
        )

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # Runtime preparation is lazy; start the worker under the startup deadline.
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "send", "arguments": {"control": "restart"}},
            }
        )
        startup = receive(process, buffer, startup_timeout)
        require(startup.get("id") == 2, "unexpected startup response ID")
        require(
            startup.get("result")
            == {
                "content": [{"type": "text", "text": "[starting new worker]\n[idle]"}],
                "isError": False,
            },
            "unexpected runtime startup response",
        )

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "send",
                    "arguments": {"r": "6 * 7"},
                },
            }
        )
        evaluation = receive(process, buffer, response_timeout)
        require(evaluation.get("id") == 3, "unexpected evaluation response ID")
        require(
            evaluation.get("result")
            == {
                "content": [{"type": "text", "text": "[1] 42\n"}],
                "isError": False,
            },
            f"unexpected R evaluation response: {json.dumps(evaluation, ensure_ascii=False)}",
        )
    except Exception as error:
        standard_error = terminate(process)
        if standard_error:
            raise ReleaseError(f"{error}: {standard_error}") from error
        raise

    process.stdin.close()
    try:
        returncode = process.wait(timeout=response_timeout)
    except subprocess.TimeoutExpired as error:
        standard_error = terminate(process)
        detail = f": {standard_error}" if standard_error else ""
        raise ReleaseError(f"MCP server did not shut down{detail}") from error

    assert process.stderr is not None
    standard_error = process.stderr.read().decode(errors="replace").strip()
    require(
        returncode == 0, f"MCP server exited with status {returncode}: {standard_error}"
    )
    require(not standard_error, f"MCP server wrote to stderr: {standard_error}")


def inspect_wheel_commands(wheel: Path, *, linux: bool) -> None:
    data = f"mcp_console-{package_version()}.data/data"
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        for name in ("mcp-console-sandbox", *(["bwrap"] if linux else [])):
            runner = f"{data}/libexec/{name}"
            require(
                [member for member in members if Path(member).name == name] == [runner],
                f"wheel must contain exactly one private sandbox runner {name} under libexec",
            )
            require(
                archive.getinfo(runner).external_attr >> 16 & 0o111 != 0,
                f"private sandbox runner {name} is not executable",
            )
        for name in (
            "LICENSE",
            "NOTICE",
            *(["bubblewrap-COPYING", "bubblewrap-SOURCE.json"] if linux else []),
        ):
            require(
                f"{data}/share/licenses/mcp-console/{name}" in members,
                f"private sandbox runner is missing {name}",
            )

        if linux:
            prefix = f"{data}/share/licenses/mcp-console"
            provenance = json.loads(archive.read(f"{prefix}/bubblewrap-SOURCE.json"))
            pin = json.loads(Path("sandbox-runner.json").read_text())
            helper = archive.read(f"{data}/libexec/bwrap")
            for key, expected in {
                "source_repository": pin["repository"],
                "source_revision": pin["commit"],
                "source_directory": "codex-rs/vendor/bubblewrap",
                "build_script": "codex-rs/bwrap/build.rs",
                "wrapper_directory": "codex-rs/bwrap",
                "sha256": hashlib.sha256(helper).hexdigest(),
            }.items():
                require(
                    provenance.get(key) == expected,
                    f"Bubblewrap provenance has inconsistent {key}",
                )
            with tempfile.TemporaryDirectory() as directory:
                elf = Path(directory) / "bwrap"
                elf.write_bytes(helper)
                dynamic = command_output(
                    ["readelf", "-d", "-W", str(elf)], env=os.environ | {"LC_ALL": "C"}
                )
            needed = sorted(re.findall(r"\(NEEDED\).*\[([^]]+)\]", dynamic))
            linkage = (
                "dynamic"
                if any(name.startswith("libcap.so.") for name in needed)
                else "static"
            )
            require(
                provenance.get("elf_needed") == needed,
                "Bubblewrap provenance has inconsistent ELF dependencies",
            )
            require(
                provenance.get("libcap_linkage") == linkage,
                "Bubblewrap provenance has inconsistent libcap linkage",
            )
            libcap_notice = f"{prefix}/libcap-NOTICE"
            require(
                (libcap_notice in members) == (linkage == "static"),
                "Bubblewrap provenance requires libcap-NOTICE only for redistributed static libcap",
            )
            if linkage == "static":
                require(
                    bool(archive.read(libcap_notice).strip()),
                    "Bubblewrap provenance has an empty libcap-NOTICE",
                )


def smoke_wheel(args: argparse.Namespace) -> None:
    wheel = Path(args.wheel).resolve()
    cargo_bin = Path(args.cargo_bin).resolve()
    version = package_version()

    require(wheel.is_file(), f"wheel does not exist: {wheel}")
    require(cargo_bin.is_file(), f"Cargo binary does not exist: {cargo_bin}")
    require(
        os.access(cargo_bin, os.X_OK), f"Cargo binary is not executable: {cargo_bin}"
    )
    require(
        wheel.name.startswith(f"mcp_console-{version}-"),
        f"wheel version does not match {version}: {wheel.name}",
    )
    platform = wheel.name.rsplit("-", 1)[-1]
    require(
        platform.startswith(("macosx_", "manylinux", "linux_")),
        f"unsupported wheel platform: {wheel.name}",
    )
    linux = not platform.startswith("macosx_")
    require(not wheel.name.endswith("-none-any.whl"), "wheel must be platform-specific")
    inspect_wheel_commands(wheel, linux=linux)

    if args.target is not None:
        architecture = TARGET_ARCHITECTURES[args.target]
        require(
            wheel.name.endswith(f"_{architecture}.whl")
            and linux == ("linux" in args.target),
            f"wheel does not match {args.target}: {wheel.name}",
        )

    expected_version = command_output([str(cargo_bin), "--version"])
    actual_version = command_output(
        ["uv", "tool", "run", "--from", str(wheel), "mcp-console", "--version"]
    )
    require(actual_version == expected_version, "installed and Cargo versions differ")

    cargo_help = command_output([str(cargo_bin), "--help"], strip=False)
    wheel_help = command_output(
        ["uv", "tool", "run", "--from", str(wheel), "mcp-console", "--help"],
        strip=False,
    )
    require(wheel_help == cargo_help, "installed and Cargo help output differ")

    run_command(["uv", "tool", "install", str(wheel)])

    tool_bin = Path(os.environ["UV_TOOL_BIN_DIR"])
    installed = tool_bin / "mcp-console"
    require(installed.is_file(), f"installed executable does not exist: {installed}")
    require(
        os.access(installed, os.X_OK),
        f"installed executable is not executable: {installed}",
    )
    require(
        command_output([str(installed), "--version"]) == expected_version,
        "`uv` tool and Cargo versions differ",
    )
    require(
        command_output([str(installed), "--help"], strip=False) == cargo_help,
        "`uv` tool and Cargo help output differ",
    )
    public_runner = tool_bin / "mcp-console-sandbox"
    require(
        not public_runner.exists(),
        f"private sandbox runner was installed as a public command: {public_runner}",
    )
    with tempfile.TemporaryDirectory(prefix="mcp-console-empty-path-") as directory:
        sandbox_env = os.environ.copy()
        sandbox_env["PATH"] = directory
        run_command([str(cargo_bin), "sandbox", "--", "/usr/bin/true"], env=sandbox_env)
        run_command([str(installed), "sandbox", "--", "/usr/bin/true"], env=sandbox_env)

    internal_ir = installed.resolve().with_name("ir")
    require(not internal_ir.exists(), f"wheel contains sibling `ir`: {internal_ir}")

    r_home = command_output(["R", "RHOME"])
    uv = shutil.which("uv")
    require(uv is not None, "host `uv` is not on `PATH`")
    with tempfile.TemporaryDirectory(prefix="mcp-console-uv-path-") as directory:
        uv_bin = Path(directory)
        (uv_bin / "uv").symlink_to(Path(uv).resolve())
        unavailable_uvx = uv_bin / "uvx"
        unavailable_uvx.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        unavailable_uvx.chmod(0o755)
        path = os.pathsep.join(
            [str(uv_bin)]
            + [
                entry
                for entry in os.environ.get("PATH", "").split(os.pathsep)
                if not (Path(entry) / "ir").is_file()
            ]
        )

        env = os.environ.copy()
        env.pop("RETICULATE_UV", None)
        env["R_HOME"] = r_home
        env["PATH"] = path
        smoke_mcp(
            installed,
            version,
            env,
            args.startup_timeout_seconds,
            args.response_timeout_seconds,
        )


def validate_publish(_: argparse.Namespace) -> None:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    ref_type = os.environ["GITHUB_REF_TYPE"]
    tag = os.environ["GITHUB_REF_NAME"]
    repository = os.environ["GITHUB_REPOSITORY"]

    require(
        event_name == "push",
        f"release publication requires a push event, got {event_name}",
    )
    require(ref_type == "tag", f"release publication requires a tag, got {ref_type}")
    require(STABLE_TAG.fullmatch(tag) is not None, f"invalid release tag: {tag}")

    version = package_version()
    require(
        tag == f"v{version}", f"tag {tag} does not match Cargo.toml version v{version}"
    )

    release_commit = command_output(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"])
    run_command(["git", "fetch", "--no-tags", "origin", "main"])
    main_commit = command_output(["git", "rev-parse", "FETCH_HEAD"])
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", release_commit, main_commit],
        check=False,
    )
    require(ancestry.returncode == 0, f"release commit {release_commit} is not on main")

    response = command_output(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "--method",
            "GET",
            f"/repos/{repository}/actions/workflows/ci.yaml/runs",
            "-f",
            f"head_sha={release_commit}",
            "-f",
            "event=push",
            "-f",
            "branch=main",
            "-f",
            "status=completed",
            "-f",
            "per_page=100",
        ]
    )
    runs = json.loads(response)["workflow_runs"]
    matching_runs = [
        run
        for run in runs
        if run.get("head_sha") == release_commit
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
    ]
    require(
        bool(matching_runs) and matching_runs[0].get("conclusion") == "success",
        f"release commit {release_commit} lacks successful CI from a push to main",
    )


def verify_wheel_set(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    wheels = sorted(directory.glob("*.whl"))
    arm64 = list(directory.glob("mcp_console-*-macosx_*_arm64.whl"))
    x86_64 = list(directory.glob("mcp_console-*-macosx_*_x86_64.whl"))
    linux_arm64 = list(directory.glob("mcp_console-*-manylinux_*_aarch64.whl"))
    linux_x86_64 = list(directory.glob("mcp_console-*-manylinux_*_x86_64.whl"))
    universal = list(directory.glob("*-none-any.whl"))
    sdists = list(directory.glob("*.tar.gz"))

    require(len(wheels) == 4, f"expected exactly four wheels, found {len(wheels)}")
    require(len(arm64) == 1, "expected exactly one Apple Silicon wheel")
    require(len(x86_64) == 1, "expected exactly one Intel macOS wheel")
    require(len(linux_arm64) == 1, "expected exactly one ARM64 Linux wheel")
    require(len(linux_x86_64) == 1, "expected exactly one x86-64 Linux wheel")
    require(not universal, "a platform-independent wheel must not be published")
    require(not sdists, "a source distribution must not be published")

    print("Publishing:")
    for wheel in wheels:
        print(f"  {wheel}")


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    commands = argument_parser.add_subparsers(required=True)

    smoke = commands.add_parser("smoke-wheel")
    smoke.add_argument("wheel")
    smoke.add_argument("cargo_bin")
    smoke.add_argument("--target", choices=sorted(TARGET_ARCHITECTURES))
    smoke.add_argument("--startup-timeout-seconds", type=float, default=1200.0)
    smoke.add_argument("--response-timeout-seconds", type=float, default=30.0)
    smoke.set_defaults(function=smoke_wheel)

    validate = commands.add_parser("validate-publish")
    validate.set_defaults(function=validate_publish)

    verify = commands.add_parser("verify-wheel-set")
    verify.add_argument("directory")
    verify.set_defaults(function=verify_wheel_set)

    return argument_parser


def main() -> int:
    args = parser().parse_args()
    try:
        for name in ("startup_timeout_seconds", "response_timeout_seconds"):
            require(getattr(args, name, 1) > 0, "timeouts must be greater than zero")
        args.function(args)
    except (FileNotFoundError, KeyError, json.JSONDecodeError, ReleaseError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
