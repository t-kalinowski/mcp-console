"""Verify fresh public-index installations of mcp-console 0.0.4."""

import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile

VERSION = "0.0.4"
ROOT = Path(sys.argv[1]).resolve()
smoke_mcp = runpy.run_path(str(ROOT / "scripts/release.py"))["smoke_mcp"]
uv = shutil.which("uv")
uvx = shutil.which("uvx")
assert uv and uvx
flags = ["--no-cache", "--no-sources", "--default-index", "https://pypi.org/simple"]


def run(command: list[str], env: dict[str, str]) -> str:
    print("+ " + shlex.join(command), flush=True)
    result = subprocess.run(
        command, env=env, text=True, stdout=subprocess.PIPE, check=True
    )
    print(result.stdout, end="", flush=True)
    return result.stdout


with tempfile.TemporaryDirectory(prefix="mcp-console-public-0.0.4-") as directory:
    work = Path(directory)
    os.chdir(work)
    for label, requirement in (
        ("exact", f"mcp-console=={VERSION}"),
        ("latest", "mcp-console"),
    ):
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "UV_INDEX",
                "UV_INDEX_URL",
                "UV_EXTRA_INDEX_URL",
                "UV_DEFAULT_INDEX",
            }
        }
        for variable, name in (
            ("UV_CACHE_DIR", "cache"),
            ("UV_TOOL_DIR", "tools"),
            ("UV_TOOL_BIN_DIR", "bin"),
        ):
            path = work / label / name
            path.mkdir(parents=True)
            env[variable] = str(path)
        env["UV_NO_CONFIG"] = "1"
        command = [uvx, "--isolated", *flags, requirement]
        assert run([*command, "--version"], env).strip() == f"mcp-console {VERSION}"
        expected_help = run([*command, "--help"], env)
        run([uv, "tool", "install", *flags, requirement], env)
        installed = Path(env["UV_TOOL_BIN_DIR"]) / "mcp-console"
        assert (
            run([str(installed), "--version"], env).strip() == f"mcp-console {VERSION}"
        )
        assert run([str(installed), "--help"], env) == expected_help
        run([str(installed), "sandbox", "--", "/usr/bin/true"], env)
        if label == "exact":
            empty_path = work / "empty-path"
            empty_path.mkdir()
            run(
                [str(installed), "sandbox", "--", "/usr/bin/true"],
                {**env, "PATH": str(empty_path)},
            )
            launcher = work / "public-uvx"
            launcher.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
            launcher.chmod(0o755)
            print(
                "Checking public uvx MCP initialization, worker startup, and R 6 * 7",
                flush=True,
            )
            smoke_mcp(launcher, VERSION, env, 1200.0, 30.0)
            print("Public uvx returned [1] 42", flush=True)
    print("Exact and unqualified public PyPI installations verified", flush=True)
