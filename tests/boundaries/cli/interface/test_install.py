#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def environment(root: Path) -> dict[str, str]:
    return os.environ | {
        "CODEX_HOME": str(root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(root / "claude-home"),
        "MCP_CONSOLE_HOME": str(root / "console-home"),
        "NO_COLOR": "1",
    }


def install(
    binary: Path, root: Path, workspace: Path, client: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "install", client, *arguments],
        cwd=workspace,
        env=environment(root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


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
            result = install(binary, root, root, client, "--scope", "global")
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
            again = install(binary, root, root, client, "--scope", "global")
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
            result = install(binary, root, nested, client)
            assert result.returncode == 0 and result.stderr == "", result
            assert str(project_config) in result.stdout, result
            assert not global_config.exists() and not (nested / relative).exists()
            assert (
                server(read_config(project_config, client), client)["command"] == "uvx"
            )
            result = install(binary, root, nested, client, "--scope", "global")
            assert result.returncode == 0 and str(global_config) in result.stdout, (
                result
            )
            outside = root / f"outside-{client}"
            outside.mkdir()
            result = install(binary, root, outside, client)
            assert result.returncode == 0 and str(global_config) in result.stdout, (
                result
            )
            assert not (outside / relative).exists()
            result = install(binary, root, outside, client, "--scope", "project")
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
            result = install(
                binary, root, root, client, "--scope", "global", "--dry-run"
            )
            assert result.returncode == 0 and result.stderr == "", result
            assert str(path) in result.stdout and "uvx" in result.stdout, result
            assert not path.parent.exists()
            preview = result.stdout.replace(str(path), "<CONFIG>")
            result = install(binary, root, root, client, "--scope", "global")
            assert result.returncode == 0, result
            written = path.read_bytes()
            result = install(
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
            result = install(
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
            result = install(binary, root, root, client, "--scope", "project")
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
                result = install(binary, root, root, client, "--scope", "global")
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
            result = install(binary, root, root, client, "--scope", "global")
            assert result.returncode == 0 and result.stderr == "", result
            assert path.is_symlink() and path.resolve() == target
            assert target.stat().st_mode & 0o777 == 0o640
            assert server(read_config(target, client), client)["command"] == "uvx"
            records.append(
                {"client": client, "symlink": "preserved", "permissions": "0640"}
            )
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
