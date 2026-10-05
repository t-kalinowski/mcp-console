"""Cache selection and permissions through public local sessions."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SANDBOX, command, requires
from support.r import r_test_environment


CACHE_VARIABLES = (
    "XDG_DATA_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "IR_CACHE_DIR",
    "IR_LIBRARY_ROOT",
    "R_USER_CACHE_DIR",
    "R_USER_DATA_DIR",
    "RENV_PATHS_ROOT",
    "RENV_PATHS_CACHE",
    "RENV_PATHS_SOURCE",
    "RENV_PATHS_BINARY",
    "PKG_CACHE_DIR",
    "R_PKG_CACHE_DIR",
    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY",
    "PYTHONPYCACHEPREFIX",
    "PYTHONUSERBASE",
)


@requires(SANDBOX)
def test_default_caches_are_console_owned(binary: Path) -> Transcript:
    return cache_locations(
        binary, host=False, sources=("default", "platform", "isolated")
    )


@requires(SANDBOX)
def test_absolute_xdg_cache_starts_without_home(binary: Path) -> Transcript:
    return cache_locations(binary, host=False, sources=("xdg_without_home",))


@requires(SANDBOX)
def test_console_root_is_created_with_explicit_entries(binary: Path) -> Transcript:
    return cache_locations(binary, host=False, sources=("explicit_entries",))


@requires(SANDBOX)
def test_host_cache_opt_out(binary: Path) -> Transcript:
    return cache_locations(binary, host=True, sources=("direct", "config", "cli"))


def test_console_cache_rejects_unsandboxed_execution(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        result = subprocess.run(
            [binary, "serve", "--no-sandbox", "-c", "cache=console"],
            cwd=root,
            env=dict(
                os.environ,
                XDG_CACHE_HOME=str(root / "cache-base"),
                MCP_CONSOLE_HOME=str(root / "home"),
            ),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, result
        assert (
            result.stderr
            == "cache: console requires sandboxing; use cache: host with --no-sandbox\n"
        )
        assert not result.stdout
        assert not (root / "cache-base").exists()
    return [{"unsandboxed_console_cache_rejected": True}]


@requires(SANDBOX, R, command("ir"), command("uv"))
def test_uses_reticulate_managed_uv_inside_console_cache(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env, _ = r_test_environment()
        env.update(
            XDG_CACHE_HOME=str(root / "cache-base"),
            RETICULATE_UV="managed",
            MCP_CONSOLE_LANGUAGES="r",
        )
        uv = root / "cache-base/mcp-console/R/reticulate/uv/bin/uv"
        if sys.platform == "darwin":
            # macOS's system shell writes heredoc files outside TMPDIR. Seed the
            # tool here; Linux exercises the cold managed installer as well.
            uv.parent.mkdir(parents=True)
            shutil.copy2(shutil.which("uv"), uv)
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "reticulate managed uv readiness")
            client.expect("[prepared]", requirements={"r": ["DBI"]})
            client.expect(
                "managed uv is Console owned\n",
                # fmt: r
                r=code(r"""
                    uv <- get("uv_binary", asNamespace("reticulate"))()
                    root <- paste0(normalizePath(Sys.getenv("R_USER_CACHE_DIR")), "/")
                    stopifnot(startsWith(normalizePath(uv), root))
                    cat("managed uv is Console owned\n")
                    """),
            )
            client.finish()
        assert uv.is_file(), uv
    return [{"reticulate_managed_uv_is_console_owned": True}]


@requires(SANDBOX)
def test_managed_python_and_duckdb_stay_in_console_cache(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        env = environment(tools)
        env.pop("UV_PYTHON", None)
        env["UV_PYTHON_PREFERENCE"] = "only-managed"
        env["XDG_CACHE_HOME"] = str(root / "cache-base")
        env["CACHE_TEST_ROOT"] = str(root / "cache-base/mcp-console")
        for name in CACHE_VARIABLES:
            env[name] = str(root / "host" / name)
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                # fmt: python
                check = code("""
                    import os
                    import sys
                    from pathlib import Path

                    root = Path(os.environ["CACHE_TEST_ROOT"])
                    assert Path(sys.executable).resolve().is_relative_to(root)
                    assert Path(sys.base_prefix).resolve().is_relative_to(root)
                    assert Path(sys.pycache_prefix).is_relative_to(root)
                    import numpy, pandas, duckdb

                    for package in (numpy, pandas, duckdb):
                        assert Path(package.__file__).resolve().is_relative_to(root)
                    print("managed installation is Console owned")
                    """)
                client.expect("managed installation is Console owned\n", python=check)
                result = client.send(
                    sql="SELECT current_setting('extension_directory') AS directory"
                )
                assert not result.get("isError"), result
                assert str(
                    root / "cache-base/mcp-console/duckdb/extensions"
                ) in json.dumps(result), result
            client.send(requirements={"python": ["six"]})
            client.expect("six\n", python="import six; print(six.__name__)")
            result = client.send(sql="LOAD sqlite")
            assert not result.get("isError"), result
            client.finish()
        cache = root / "cache-base/mcp-console"
        assert list(
            (cache / "duckdb/extensions").glob("v*/**/sqlite_scanner.duckdb_extension")
        )
        assert not (root / "host").exists()
    return [
        {
            "managed_python": True,
            "live_requirements": True,
            "duckdb": True,
            "host_caches_untouched": True,
        }
    ]


def cache_locations(
    binary: Path, *, host: bool, sources: tuple[str, ...]
) -> Transcript:
    for source in sources:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tools = root / "bin"
            tools.mkdir()
            selected = root / "python"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", selected],
                check=True,
                capture_output=True,
            )
            python = selected / "bin/python3"
            site = Path(
                subprocess.check_output(
                    [
                        python,
                        "-I",
                        "-c",
                        "import site; print(site.getsitepackages()[0])",
                    ],
                    text=True,
                ).strip()
            )
            env = environment(tools)
            env["RETICULATE_PYTHON"] = str(python)
            env["XDG_CACHE_HOME"] = str(root / "cache-base")
            env["CACHE_TEST_ROOT"] = str(root)
            for name in CACHE_VARIABLES:
                env[name] = str(root / "host" / name)
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            settings = {}
            console_root = root / "cache-base/mcp-console"
            if source == "platform":
                env.pop("XDG_CACHE_HOME")
                home = root / "account"
                settings["resolver"] = {"environment": {"HOME": str(home)}}
                console_root = home / (
                    "Library/Caches/mcp-console"
                    if sys.platform == "darwin"
                    else ".cache/mcp-console"
                )
            if source == "config":
                settings["cache"] = "host"
            if source == "isolated":
                settings = {
                    policy: {
                        "inherit_environment": False,
                        "environment": dict(env),
                    }
                    for policy in ("resolver", "sandbox")
                }
            if source == "xdg_without_home":
                env.pop("HOME", None)
            if source == "explicit_entries":
                settings["resolver"] = {
                    "filesystem": {
                        "entries": [
                            {
                                "path": {"type": "special", "value": {"kind": "root"}},
                                "access": "read",
                            },
                            {
                                "path": {"type": "path", "path": str(console_root)},
                                "access": "write",
                            },
                        ]
                    }
                }
                assert not console_root.exists()
            config.write_text(json.dumps(settings))
            expected = {
                name: env[name] if host else str(console_root)
                for name in CACHE_VARIABLES
            }
            env["CACHE_TEST_EXPECTED"] = json.dumps(expected)
            if source == "isolated":
                for policy in ("resolver", "sandbox"):
                    settings[policy]["environment"]["CACHE_TEST_EXPECTED"] = env[
                        "CACHE_TEST_EXPECTED"
                    ]
                config.write_text(json.dumps(settings))
            # Selected Python inspection runs in the resolver before the worker.
            # The hook tests real cache permissions, without requiring downloads.
            # fmt: python
            probe = code(f"""
                import json
                import os
                from pathlib import Path

                if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ:
                    expected = json.loads(os.environ["CACHE_TEST_EXPECTED"])
                    for name, value in expected.items():
                        actual = Path(os.environ[name])
                        assert actual == Path(value) if {host!r} else actual.is_relative_to(value), (name, actual, value)
                    cache = Path(os.environ["UV_CACHE_DIR"])
                    cache.mkdir(parents=True, exist_ok=True)
                    cache.joinpath("resolver-probe").write_text("prepared")
                    if not {host!r}:
                        try:
                            Path(os.environ["CACHE_TEST_ROOT"]).joinpath("host-write").write_text("escaped")
                        except PermissionError:
                            pass
                        else:
                            raise AssertionError("resolver wrote outside Console caches")
                """)
            (site / "sitecustomize.py").write_text(probe)
            arguments = ["serve"]
            if source == "direct":
                arguments.append("--no-sandbox")
            elif source == "cli":
                arguments.extend(["-c", "cache=host"])
            with McpClient(binary, arguments, env, root) as client:
                client.initialize_and_list_tools()
                if not host:
                    assert console_root.is_dir(), console_root
                for restart in (False, True):
                    if restart:
                        config.write_text(
                            "cache: host\n" if not host else "cache: console\n"
                        )
                        client.send(control="restart")
                    # fmt: python
                    check = code(f"""
                        import json
                        import os
                        from pathlib import Path

                        expected = json.loads(os.environ["CACHE_TEST_EXPECTED"])
                        # Explicit Python uses its preinstalled DuckDB extensions.
                        expected.pop("MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY")
                        for name, value in expected.items():
                            actual = Path(os.environ[name])
                            assert actual == Path(value) if {host!r} else actual.is_relative_to(value), (name, actual, value)
                        assert Path(os.environ["UV_CACHE_DIR"]).joinpath("resolver-probe").read_text() == "prepared"
                        print("cache selection retained")
                        """)
                    client.expect("cache selection retained\n", python=check)
                client.finish()
            assert not (root / "host-write").exists()
            if not host:
                assert not (root / "host").exists()
    return [
        {
            "host_caches" if host else "console_caches": True,
            "restart_retains_selection": True,
        }
    ]
