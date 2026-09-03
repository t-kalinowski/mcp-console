import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PYTHON_DOWNLOAD_URL = "https://example.invalid/python.tar.zst"


def checkpoint_uv_environment(
    temporary: Path,
    argument: str,
    *,
    reuse_resolved_python_for: tuple[str, ...] = (),
    provide_python_module: tuple[str, str] | None = None,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint]:
    assert all(reuse_resolved_python_for)
    assert provide_python_module is None or (
        provide_python_module[0] in reuse_resolved_python_for
        and provide_python_module[1].isidentifier()
    )
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    started = FifoCheckpoint.create(temporary / "uv-started")
    release = FifoCheckpoint.create(temporary / "uv-release")
    environment = os.environ.copy()
    environment["RETICULATE_UV"] = str(FIXTURES / "checkpoint_uv")
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_ARGUMENT"] = argument
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_CLAIM"] = str(temporary / "uv-claimed")
    environment["MCP_CONSOLE_TEST_UV_STARTED"] = str(started.path)
    environment["MCP_CONSOLE_TEST_UV_RELEASE"] = str(release.path)
    if reuse_resolved_python_for:
        environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"] = str(
            temporary / "resolved-python"
        )
        environment["MCP_CONSOLE_TEST_UV_REUSE_REQUIREMENTS"] = os.pathsep.join(
            reuse_resolved_python_for
        )
        environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"] = str(
            temporary / "uv-reuse-record"
        )
    if provide_python_module is not None:
        requirement, module = provide_python_module
        modules = temporary / "python-modules"
        modules.mkdir()
        environment["PYTHONPATH"] = str(modules)
        environment["MCP_CONSOLE_TEST_UV_PROVIDE_REQUIREMENT"] = requirement
        environment["MCP_CONSOLE_TEST_UV_PROVIDE_MODULE"] = str(
            modules / f"{module}.py"
        )
    return environment, started, release


def build_killpg_denial_interposer(directory: Path) -> Path:
    source = directory / "deny-killpg.c"
    library = directory / "deny-killpg.dylib"
    fixture = FIXTURES / "native" / "killpg_denial_interposer.c"
    shutil.copyfile(fixture, source)
    subprocess.run(
        ["cc", "-dynamiclib", "-o", library, source],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


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
    resolver_started = FifoCheckpoint.create(temporary_path / "resolver-started")
    resolver_lifetime = FifoCheckpoint.create(temporary_path / "resolver-lifetime")
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


def fake_ir_environment(root: Path, libraries: list[Path]) -> dict[str, str]:
    environment, _ = r_test_environment()
    fake_bin = root / "bin"
    fake_bin.mkdir()
    fixture = FIXTURES / "ordered_retirement_ir"
    (fake_bin / "ir").symlink_to(fixture)
    path = environment.get("PATH")
    assert path is not None, "PATH is required"
    environment["PATH"] = os.pathsep.join((str(fake_bin), path))
    environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(root / "ir-counter")
    environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = os.pathsep.join(map(str, libraries))
    return environment


def named_requirement_error(requirement: str) -> str:
    return (
        f"Python requirement `{requirement}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    )


def python_version_constraint_error(constraint: str) -> str:
    return (
        f"Python version constraint `{constraint}` is not accepted: host-side managed "
        "resolution accepts version numbers and supported PEP 440 version specifiers only"
    )


def normalize_duckdb_resolution_error(error: str, extension: str) -> str:
    detail = next(
        line.strip().removeprefix("! ")
        for line in error.splitlines()
        if f'Failed to download extension "{extension}"' in line
    )
    return detail.partition(' at URL "')[0]


def ir_cache_directory(environment: dict[str, str]) -> str:
    ir = shutil.which("ir", path=environment.get("PATH"))
    assert ir is not None, "ir is required"
    cache = subprocess.run(
        [ir, "cache", "dir"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    assert cache and Path(cache).is_absolute(), (
        f"ir returned invalid cache directory: {cache}"
    )
    return cache


def matplotlib_test_environment(cache_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    cache = ir_cache_directory(environment)
    environment["IR_CACHE_DIR"] = cache
    environment["XDG_CACHE_HOME"] = str(cache_home)
    assert ir_cache_directory(environment) == cache
    return environment


def python_inventory_client(
    binary: Path,
    directory: Path,
    *,
    preference: str | None = None,
    install_directory: Path | None = None,
    resolver_python: Path | None = None,
    resolver_record: Path | None = None,
    extra_environment: dict[str, str] | None = None,
) -> tuple[McpClient, Path, Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment.pop("UV_PYTHON_PREFERENCE", None)
    environment["RETICULATE_UV"] = str(FIXTURES / "record_uv_environment")
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv.jsonl")
    arguments = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments)
    inventories = directory / "uv-python-inventories.json"
    environment["MCP_CONSOLE_TEST_UV_PYTHON_INVENTORIES"] = str(inventories)
    if preference is not None:
        environment["UV_PYTHON_PREFERENCE"] = preference
    if install_directory is not None:
        environment["UV_PYTHON_INSTALL_DIR"] = str(install_directory)
    if resolver_python is not None:
        environment["MCP_CONSOLE_TEST_UV_PYTHON"] = str(resolver_python)
    if resolver_record is not None:
        environment["MCP_CONSOLE_TEST_UV_RESOLVER_RECORD"] = str(resolver_record)
    if extra_environment is not None:
        environment.update(extra_environment)
    client = McpClient(
        binary,
        ("serve",),
        environment,
        current_directory=directory,
    )
    client._initialize_and_list_tools()
    arguments.write_text("", encoding="utf-8")
    if resolver_record is not None:
        resolver_record.write_text("", encoding="utf-8")
    return client, inventories, arguments


def uv_python_row(
    version: str,
    *,
    path: str | Path | None = None,
    url: str | None = PYTHON_DOWNLOAD_URL,
    variant: str = "default",
    implementation: str = "cpython",
) -> dict[str, object]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None, version
    major, minor, patch = (int(part) for part in match.groups())
    return {
        "key": f"{implementation}-{version}-macos-aarch64-none",
        "version": version,
        "version_parts": {"major": major, "minor": minor, "patch": patch},
        "path": None if path is None else str(path),
        "symlink": None,
        "url": url,
        "variant": variant,
        "implementation": implementation,
    }


def write_uv_python_inventories(path: Path, inventories: dict[str, object]) -> None:
    path.write_text(json.dumps(inventories), encoding="utf-8")


def recorded_python_preferences(arguments: Path) -> list[str]:
    invocations = [
        json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()
    ]
    return [
        invocation[invocation.index("--python-preference") + 1]
        for invocation in invocations
        if invocation[:2] == ["python", "list"]
    ]


def recorded_tool_run_pythons(arguments: Path) -> list[str]:
    invocations = [
        json.loads(line) for line in arguments.read_text(encoding="utf-8").splitlines()
    ]
    return [
        invocation[invocation.index("--python") + 1]
        for invocation in invocations
        if invocation[:2] == ["tool", "run"] and "--python" in invocation
    ]


def read_uv_resolver_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def resolve_public_python_version(
    client: McpClient,
    constraints: list[str],
) -> str:
    constraints_r = (
        "character()"
        if not constraints
        else f"c({', '.join(json.dumps(value) for value in constraints)})"
    )
    # fmt: r
    r = code(rf"""
        reticulate::py_require(
          python_version = {
            constraints_r
          },
          action = "set"
        )
        result <- tryCatch(
          reticulate::py_write_requirements(
            NULL,
            NULL,
            freeze = FALSE,
            python = NULL
          )$python_version,
          error = conditionMessage
        )
        cat(result, "\n", sep = "")
        """)
    client.send(r=r)
    return last_result_text(client)


def write_python_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def recording_uv_environment(
    directory: Path,
    *,
    fail_requirement: str | None = None,
    substitute_requirement: tuple[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["RETICULATE_UV"] = str(FIXTURES / "record_uv_environment")
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv-environment.jsonl")
    arguments_record = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments_record)
    if fail_requirement is not None:
        failure_marker = directory / "uv-failure"
        failure_marker.touch()
        environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"] = str(failure_marker)
        environment["MCP_CONSOLE_TEST_UV_FAILURE_ARGUMENT"] = fail_requirement
    if substitute_requirement is not None:
        substitute, replacement = substitute_requirement
        environment["MCP_CONSOLE_TEST_UV_SUBSTITUTE_REQUIREMENT"] = substitute
        environment["MCP_CONSOLE_TEST_UV_REPLACEMENT_REQUIREMENT"] = replacement
    return environment, arguments_record


def uv_tool_run_requirements(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    arguments = [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]
    requirements = []
    for invocation in arguments:
        if invocation[:2] != ["tool", "run"]:
            continue
        separator = invocation.index("--")
        manifest = [
            invocation[index + 1]
            for index, argument in enumerate(invocation[:separator])
            if argument == "--with"
        ]
        requirements.append(manifest)
    return requirements


def initialize_python_and_record_baseline(client: McpClient, record: Path) -> int:
    client.send(python="None")
    assert last_result_text(client) == "[done]"
    return len(uv_tool_run_requirements(record))


def resolve_managed_python(binary: Path, directory: Path) -> Path:
    workspace = directory / "managed-python"
    workspace.mkdir()
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment.pop("UV_PYTHON", None)
    client = McpClient(
        binary,
        ("serve",),
        environment,
        current_directory=workspace,
    )
    client._initialize_and_list_tools()
    client.send(python='import sys\nprint(f"managed-python={sys.executable}")')
    output = last_result_text(client)
    client._finish()
    executable = Path(
        next(
            line for line in output.splitlines() if line.startswith("managed-python=")
        ).split("=", 1)[1]
    ).resolve()
    assert executable.is_file(), executable
    return executable


def send_and_collect_runtime_python_resolution(
    client: McpClient,
    **arguments: object,
) -> str:
    call_start = len(client.transcript)
    client.send(**arguments)
    chunks = []
    for attempt in range(8):
        output = last_result_text(client)
        if output.endswith("\n[running; poll with an empty send]"):
            chunks.append(output.removesuffix("\n[running; poll with an empty send]"))
            if attempt == 7:
                raise AssertionError(
                    "automatic Python resolution remained running after eight "
                    f"responses: collected={''.join(chunks)!r}, last={output!r}"
                )
            client.send(timeout_ms=30_000)
            continue

        if output != "[done]" or not chunks:
            chunks.append(output)
        collected = "".join(chunks)

        calls = client.transcript[call_start:]
        submitted = calls[0]
        final_result = calls[-1]["result"]
        content = final_result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        content[0]["text"] = collected
        submitted["result"] = final_result
        client.transcript[call_start:] = [submitted]
        return collected
    raise AssertionError("unreachable")
