#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.installation import native_console
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def environment(root: Path) -> dict[str, str]:
    return os.environ | {
        "CODEX_HOME": str(root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(root / "claude-home"),
        "MCP_CONSOLE_HOME": str(root / "console-home"),
        "NO_COLOR": "1",
    }


def configure(
    binary: Path,
    root: Path,
    workspace: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "configure-client", *arguments],
        cwd=workspace,
        env=environment(root) | (extra_environment or {}),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


def test_multiple_clients_pins_names_and_serve_arguments(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        name = 'console.test "custom"'
        for pin in (None, "latest", "0.0.5", "v0.0.5"):
            arguments = [] if pin is None else ["--pin-version", pin]
            result = configure(
                binary,
                root,
                root,
                "codex",
                "claude",
                "codex",
                "--scope",
                "global",
                "--name",
                name,
                *arguments,
                "--",
                "--no-project-config",
                "--writable-root",
                "a path",
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert result.stdout.count("Configuration:") == 2, result
            expected = [
                "mcp-console" + (f"@{pin}" if pin else ""),
                "serve",
                "--no-project-config",
                "--writable-root",
                "a path",
            ]
            for client, path in (
                ("codex", root / "codex-home/config.toml"),
                ("claude", root / "claude-home/.claude.json"),
            ):
                config = read_config(path, client)
                mapping = config["mcp_servers" if client == "codex" else "mcpServers"]
                assert list(mapping) == [name], config
                assert mapping[name]["command"] == "uvx"
                assert mapping[name]["args"] == expected
            records.append({"version_suffix": pin, "clients": 2, "args": expected})
    return records


def test_all_clients_are_validated_before_changes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        path = root / "claude-home/.claude.json"
        path.parent.mkdir()
        path.write_text("[]", encoding="utf-8")
        result = configure(binary, root, root, "codex", "claude", "--scope", "global")
        assert result.returncode == 1 and not result.stdout, result
        assert str(path) in result.stderr, result
        assert not (root / "codex-home").exists()
        assert path.read_text(encoding="utf-8") == "[]"
        result = configure(
            binary, root, root, "codex", "--no-uvx", "--pin-version", "latest"
        )
        assert result.returncode == 2, result
        assert "--no-uvx" in result.stderr and "--pin-version" in result.stderr, result
        assert not (root / "codex-home").exists()
    return [{"invalid_client": "no files written", "conflicting_options": "rejected"}]


def test_direct_executable_finds_itself(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        prefix = root / "custom location"
        executable = prefix / "bin" / native_console(binary).name
        executable.parent.mkdir(parents=True)
        shutil.copy2(native_console(binary), executable)
        # uv-created persistent virtualenvs have cache markers too.
        (prefix / "pyvenv.cfg").write_text("uv = fixture\n", encoding="utf-8")
        (prefix / "CACHEDIR.TAG").write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8"
        )
        result = configure(
            executable,
            root,
            root,
            "codex",
            "claude",
            "--scope",
            "global",
            "--no-uvx",
            extra_environment={"PATH": str(root / "empty-path")},
        )
        assert result.returncode == 0 and result.stderr == "", result
        for client, path in (
            ("codex", root / "codex-home/config.toml"),
            ("claude", root / "claude-home/.claude.json"),
        ):
            entry = server(read_config(path, client), client)
            assert Path(entry["command"]).is_absolute()
            assert Path(entry["command"]).samefile(executable)
            assert entry["args"] == ["serve"]
    return [{"launch": "absolute path to the running executable", "clients": 2}]


def test_ephemeral_uvx_installs_persistent_executable(binary: Path) -> Transcript:
    # A local binary-only wheel exercises real uv tool run/install without
    # registry access or a production-only test hook. Serving is not needed.
    uv = shutil.which("uv")
    assert uv is not None, "uv is required"
    version = subprocess.check_output([binary, "--version"], text=True).split()[1]
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        wheels = root / "wheels"
        wheels.mkdir()
        stem = f"mcp_console-{version}"
        wheel = wheels / f"{stem}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.write(
                native_console(binary),
                f"{stem}.data/scripts/{native_console(binary).name}",
            )
            archive.writestr(
                f"{stem}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: mcp-console\nVersion: {version}\n",
            )
            archive.writestr(
                f"{stem}.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{stem}.dist-info/RECORD", "")
        extra = {
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_TOOL_DIR": str(root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(root / "uv-bin"),
            "UV_CONFIG_FILE": str(root / "uv.toml"),
            "UV_OFFLINE": "true",
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_DOWNLOADS": "never",
        }
        config = Path(extra["UV_CONFIG_FILE"])
        config.write_text(
            f"no-index = true\nfind-links = [{json.dumps(str(wheels))}]\n",
            encoding="utf-8",
        )
        command = [
            uv,
            "tool",
            "run",
            "--isolated",
            "--no-cache",
            "mcp-console",
            "configure-client",
            "codex",
            "claude",
            "--scope",
            "global",
            "--no-uvx",
        ]

        def invoke(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*command, *args],
                cwd=root,
                env=environment(root) | extra,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )

        result = invoke("--dry-run")
        assert result.returncode == 0, result
        assert f"uv tool install mcp-console@{version}" in result.stdout, result
        assert not (root / "uv-bin").exists()
        assert (
            not (root / "codex-home").exists() and not (root / "claude-home").exists()
        )
        # Invalid configuration must also prevent a persistent installation.
        bad = root / "claude-home/.claude.json"
        bad.parent.mkdir()
        bad.write_text("[]", encoding="utf-8")
        result = invoke()
        assert result.returncode == 1 and not (root / "uv-bin").exists(), result
        bad.unlink()
        # A resolver failure must leave both client configurations untouched.
        config.write_text("no-index = true\n", encoding="utf-8")
        command[command.index("mcp-console")] = str(wheel)
        result = invoke()
        assert result.returncode != 0 and "uv tool install" in result.stdout, result
        assert not (root / "codex-home").exists() and not bad.exists()
        config.write_text(
            f"no-index = true\nfind-links = [{json.dumps(str(wheels))}]\n",
            encoding="utf-8",
        )
        result = invoke()
        assert result.returncode == 0, result
        for client, path in (
            ("codex", root / "codex-home/config.toml"),
            ("claude", bad),
        ):
            entry = server(read_config(path, client), client)
            installed = root / "uv-bin" / native_console(binary).name
            assert Path(entry["command"]) == installed and installed.is_file(), entry
            assert entry["args"] == ["serve"]
            assert (
                subprocess.check_output([installed, "--version"], text=True).split()[1]
                == version
            )
        # Once installed, --no-uvx should use itself without calling uv again.
        result = configure(
            installed,
            root,
            root,
            "codex",
            "--scope",
            "global",
            "--no-uvx",
            extra_environment=extra,
        )
        assert result.returncode == 0 and "tool install" not in result.stdout, result
    return [
        {
            "fixture": "Local binary-only Console wheel, real uv, isolated tools and cache, no registry access",
            "temporary_run": "promoted to a persistent executable",
            "dry_run": "no install or configuration writes",
            "installation_failure": "no configuration writes",
        }
    ]


def read_config(path: Path, client: str) -> dict:
    if client == "codex":
        return tomllib.loads(path.read_text(encoding="utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def server(config: dict, client: str) -> dict:
    return config["mcp_servers" if client == "codex" else "mcpServers"]["console"]


def test_global_install_preserves_configuration_and_is_repeatable(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            home = root / f"{client}-home"
            home.mkdir()
            path = home / ("config.toml" if client == "codex" else ".claude.json")
            if client == "codex":
                source = (
                    '# Keep this comment and model setting.\nmodel = "example"\n\n'
                    '[mcp_servers.other]\ncommand = "other-server" # Keep this too.\n\n'
                    '[mcp_servers.console]\ncommand = "mcp-console"\nargs = ["serve"]\n'
                    "enabled = false\nstartup_timeout_sec = 30\n"
                    '[mcp_servers.console.env]\nKEEP = "value"\n'
                )
            else:
                source = json.dumps(
                    {
                        "theme": "dark",
                        "projects": {"example": {"allowedTools": []}},
                        "mcpServers": {
                            "other": {"command": "other-server"},
                            "console": {
                                "type": "stdio",
                                "command": "mcp-console",
                                "args": ["serve"],
                                "env": {"KEEP": "value"},
                            },
                        },
                    }
                )
            path.write_text(source, encoding="utf-8")
            before = read_config(path, client)
            result = configure(binary, root, root, client, "--scope", "global")
            assert result.returncode == 0 and result.stderr == "", result
            assert str(path) in result.stdout and "uvx" in result.stdout, result
            config = read_config(path, client)
            entry = server(config, client)
            assert entry["command"] == "uvx" and entry["args"] == [
                "mcp-console",
                "serve",
            ]
            assert entry["env"] == {"KEEP": "value"}
            if client == "codex":
                assert "[mcp_servers.console]\n" in result.stdout, result
                assert (
                    entry["omit_tools_from"] == ["deferred"]
                    and entry["enabled"] is True
                )
                assert entry["startup_timeout_sec"] == 30
                assert (
                    '# Keep this comment and model setting.\nmodel = "example"'
                    in path.read_text()
                )
                assert 'command = "other-server" # Keep this too.' in path.read_text()
                before["mcp_servers"]["console"] = entry
            else:
                assert entry["type"] == "stdio" and entry["alwaysLoad"] is True
                before["mcpServers"]["console"] = entry
            assert config == before
            written = path.read_bytes()
            again = configure(binary, root, root, client, "--scope", "global")
            assert again.returncode == 0 and again.stderr == "", again
            assert path.read_bytes() == written
            records.append(
                {
                    "client": client,
                    "console": entry,
                    "stdout": result.stdout.replace(str(path), "<CONFIG>"),
                    "repeat": "unchanged",
                }
            )
    return records


def test_scope_defaults_and_explicit_overrides(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            project = root / client
            nested = project / "src" / "nested"
            nested.mkdir(parents=True)
            # Git worktrees use a .git file rather than a directory.
            (project / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
            relative = ".codex/config.toml" if client == "codex" else ".mcp.json"
            project_config = project / relative
            global_config = (
                root
                / f"{client}-home"
                / ("config.toml" if client == "codex" else ".claude.json")
            )
            result = configure(binary, root, nested, client)
            assert result.returncode == 0 and result.stderr == "", result
            assert str(project_config) in result.stdout, result
            assert not global_config.exists() and not (nested / relative).exists()
            assert (
                server(read_config(project_config, client), client)["command"] == "uvx"
            )
            result = configure(binary, root, nested, client, "--scope", "global")
            assert result.returncode == 0 and str(global_config) in result.stdout, (
                result
            )
            outside = root / f"outside-{client}"
            outside.mkdir()
            result = configure(binary, root, outside, client)
            assert result.returncode == 0 and str(global_config) in result.stdout, (
                result
            )
            assert not (outside / relative).exists()
            result = configure(binary, root, outside, client, "--scope", "project")
            assert (
                result.returncode == 0 and str(outside / relative) in result.stdout
            ), result
            assert (
                server(read_config(outside / relative, client), client)["command"]
                == "uvx"
            )
            records.append(
                {"client": client, "automatic": "Git root or global", "overrides": True}
            )
    return records


def test_dry_run_and_auto_load_opt_out(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client in ("codex", "claude"):
            path = (
                root
                / f"{client}-home"
                / ("config.toml" if client == "codex" else ".claude.json")
            )
            result = configure(
                binary, root, root, client, "--scope", "global", "--dry-run"
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert str(path) in result.stdout and "uvx" in result.stdout, result
            assert not path.parent.exists()
            preview = result.stdout.replace(str(path), "<CONFIG>")
            result = configure(binary, root, root, client, "--scope", "global")
            assert result.returncode == 0, result
            written = path.read_bytes()
            result = configure(
                binary,
                root,
                root,
                client,
                "--scope",
                "global",
                "--no-auto-load",
                "--dry-run",
            )
            assert result.returncode == 0 and path.read_bytes() == written, result
            result = configure(
                binary, root, root, client, "--scope", "global", "--no-auto-load"
            )
            assert result.returncode == 0 and result.stderr == "", result
            entry = server(read_config(path, client), client)
            if client == "codex":
                assert entry["omit_tools_from"] == []
            else:
                assert entry["alwaysLoad"] is False
            records.append(
                {
                    "client": client,
                    "console": entry,
                    "dry_run": preview,
                    "opt_out": result.stdout.replace(str(path), "<CONFIG>"),
                }
            )
    return records


def test_inline_toml_and_existing_project_json(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, source in (
            (
                "codex",
                '# Keep inline tables.\nmcp_servers = { other = { command = "other" }, '
                'console = { command = "old", env = { KEEP = "value" } } }\n',
            ),
            (
                "claude",
                '{"mcpServers": {"other": {"command": "other"}, '
                '"console": {"command": "old", "env": {"KEEP": "value"}}}}',
            ),
        ):
            path = root / (".codex/config.toml" if client == "codex" else ".mcp.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            result = configure(binary, root, root, client, "--scope", "project")
            assert result.returncode == 0 and result.stderr == "", result
            config = read_config(path, client)
            entry = server(config, client)
            assert entry["command"] == "uvx" and entry["args"] == [
                "mcp-console",
                "serve",
            ]
            assert entry["env"] == {"KEEP": "value"}
            mapping = config["mcp_servers" if client == "codex" else "mcpServers"]
            assert mapping["other"] == {"command": "other"}
            records.append({"client": client, "console": entry})
    return records


def test_invalid_configuration_is_not_overwritten(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, invalid in (
            (
                "codex",
                (
                    "model = [",
                    'mcp_servers = "wrong"',
                    '[mcp_servers]\nconsole = "wrong"',
                    '[mcp_servers.console]\nurl = "https://example.com/mcp"',
                ),
            ),
            (
                "claude",
                (
                    '{"mcpServers":',
                    "[]",
                    '{"mcpServers": []}',
                    '{"mcpServers": {"console": 1}}',
                    '{"mcpServers": {"console": {"type": "http", "url": "https://example.com/mcp"}}}',
                ),
            ),
        ):
            home = root / f"{client}-home"
            home.mkdir()
            path = home / ("config.toml" if client == "codex" else ".claude.json")
            for source in invalid:
                path.write_text(source, encoding="utf-8")
                result = configure(binary, root, root, client, "--scope", "global")
                assert result.returncode == 1 and result.stdout == "", result
                assert str(path) in result.stderr, result
                assert path.read_text(encoding="utf-8") == source
            records.append(
                {"client": client, "invalid_configurations_preserved": len(invalid)}
            )
    return records


@requires(POSIX)
def test_linked_configuration_preserves_target_and_permissions(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for client, source in (
            ("codex", "# Keep dotfile comments.\n"),
            ("claude", "{}\n"),
        ):
            home = root / f"{client}-home"
            home.mkdir()
            path = home / ("config.toml" if client == "codex" else ".claude.json")
            target = root / f"{client}-dotfile"
            target.write_text(source, encoding="utf-8")
            target.chmod(0o640)
            path.symlink_to(target)
            result = configure(binary, root, root, client, "--scope", "global")
            assert result.returncode == 0 and result.stderr == "", result
            assert path.is_symlink() and path.resolve() == target
            assert target.stat().st_mode & 0o777 == 0o640
            assert server(read_config(target, client), client)["command"] == "uvx"
            records.append(
                {"client": client, "symlink": "preserved", "permissions": "0640"}
            )
        # Both files parse before writes. The broken second link fails only
        # during atomic replacement, after the first client's update succeeds.
        target.unlink()
        result = configure(
            binary,
            root,
            root,
            "codex",
            "claude",
            "--scope",
            "global",
            "--pin-version",
            "latest",
        )
        first = root / "codex-home/config.toml"
        assert result.returncode == 1 and str(path) in result.stderr, result
        assert str(first) in result.stdout and str(path) not in result.stdout, result
        assert server(read_config(first, "codex"), "codex")["args"] == [
            "mcp-console@latest",
            "serve",
        ]
        assert path.is_symlink() and not target.exists()
        records.append({"later_write_failure": "earlier change remains reported"})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
