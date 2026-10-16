"""Create shared Console configuration through the public CLI."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from yaml12 import read_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.suites import run_this_suite


def invoke(
    binary: Path,
    root: Path,
    directory: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *arguments],
        cwd=directory,
        env=os.environ
        | {
            "MCP_CONSOLE_HOME": str(root / "console-home"),
            "NO_COLOR": "1",
            "PATH": str(root / "empty-path"),
        }
        | (extra_environment or {}),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_defaults_use_cwd_without_git_or_global_inference(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "console-home"
        home.mkdir()
        (home / "config.yaml").write_text("[]\n", encoding="utf-8")
        repository = root / "repository"
        repository.mkdir()
        (repository / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
        for relative in ("outside-git", "repository/nested"):
            directory = root / relative
            directory.mkdir()
            result = invoke(binary, root, directory, "config", "init")
            assert result.returncode == 0 and not result.stderr, result
            path = directory / ".agents/console/config.yaml"
            assert str(path) in result.stdout, result
            assert read_yaml(path) == {}
            assert "#" in path.read_text(encoding="utf-8")
            assert not (repository / ".agents").exists()
            records.append({"directory": relative, "configuration": {}})
        assert (home / "config.yaml").read_text(encoding="utf-8") == "[]\n"
    return records


def test_directory_and_global_destinations(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        caller = root / "caller"
        caller.mkdir()
        for selection in ("relative", "absolute", "global"):
            project = root / f"{selection} project"
            project.mkdir()
            if selection == "global":
                arguments = ["--global"]
                extra = {}
                path = root / "console-home/config.yaml"
            else:
                arguments = [
                    "--directory",
                    str(project) if selection == "absolute" else f"../{project.name}",
                ]
                # Local initialization must not inspect global configuration.
                extra = {"MCP_CONSOLE_HOME": "relative-home"}
                path = project / ".agents/console/config.yaml"
            result = invoke(
                binary,
                root,
                caller,
                "config",
                "init",
                *arguments,
                extra_environment=extra,
            )
            assert result.returncode == 0 and not result.stderr, result
            assert str(path) in result.stdout, result
            assert read_yaml(path) == {}
            assert not (caller / ".agents").exists()
            records.append({"destination": selection, "configuration": {}})
    return records


def test_directory_validates_and_retains_relative_settings(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project = root / "project"
        project.mkdir()
        executable = project / Path(sys.executable).name
        # A real executable is present only in the selected project. Validation
        # may inspect its path, but initialization must not execute Python.
        shutil.copy2(sys.executable, executable)
        result = invoke(
            binary,
            root,
            root,
            "config",
            "init",
            "--directory",
            str(project),
            "-c",
            f"python={executable.name}",
        )
        assert result.returncode == 0 and not result.stderr, result
        assert read_yaml(project / ".agents/console/config.yaml") == {
            "python": executable.name
        }
        assert not (root / ".agents").exists()
    return [
        {
            "relative_settings": "validated in the selected project and saved without absolute path expansion"
        }
    ]


def test_ordered_settings_are_saved_without_copying_discovered_values(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "console-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "environment: {INHERITED: global}\nr: {vanilla: false}\n", encoding="utf-8"
        )
        result = invoke(
            binary,
            root,
            root,
            "-c",
            "environment={KEEP: explicit, VALUE: first}",
            "-c",
            "r.packages=[stats]",
            "config",
            "init",
            "--config",
            "environment.VALUE=last",
            "-c",
            "r.packages=[]",
            "-c",
            "r.vanilla=true",
            "-c",
            "startup=null",
        )
        assert result.returncode == 0 and not result.stderr, result
        path = root / ".agents/console/config.yaml"
        expected = {
            "environment": {"KEEP": "explicit", "VALUE": "last"},
            "r": {"packages": [], "vanilla": True},
            "startup": None,
        }
        assert read_yaml(path) == expected
        # The new file remains one layer: normal launch discovery still merges
        # the global defaults rather than freezing them into the project file.
        resolved = invoke(binary, root, root, "config", "resolve", "--format", "json")
        assert resolved.returncode == 0 and not resolved.stderr, resolved
        value = json.loads(resolved.stdout)
        assert value["environment"] == expected["environment"] | {"INHERITED": "global"}
        assert value["r"]["packages"] == [] and value["r"]["vanilla"] is True
    return [
        {"saved": expected, "global_values": "remain a separate configuration layer"}
    ]


def test_dry_run_previews_the_created_contents(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        arguments = ["config", "init", "-c", "sandbox.profile=workspace"]
        preview = invoke(binary, root, root, *arguments, "--dry-run")
        assert preview.returncode == 0 and not preview.stderr, preview
        assert not (root / ".agents").exists()
        assert not (root / "console-home").exists()
        result = invoke(binary, root, root, *arguments)
        assert result.returncode == 0 and not result.stderr, result
        path = root / ".agents/console/config.yaml"
        contents = path.read_text(encoding="utf-8")
        assert contents in preview.stdout and contents in result.stdout
        assert read_yaml(path) == {"sandbox": {"profile": "workspace"}}
        for output in (preview.stdout, result.stdout):
            assert str(path) in output, output
    return [
        {"preview": preview.stdout.replace(str(root), "<WORKSPACE>")},
        {"created": result.stdout.replace(str(root), "<WORKSPACE>")},
    ]


def test_existing_configuration_is_never_replaced(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        path = root / ".agents/console/config.yaml"
        path.parent.mkdir(parents=True)
        contents = "# Existing file, even when it is not valid configuration.\n[]\n"
        path.write_text(contents, encoding="utf-8")
        identity = path.stat().st_ino
        for extra in ((), ("--dry-run",), ("-c", "r.vanilla=true")):
            result = invoke(binary, root, root, "config", "init", *extra)
            assert result.returncode == 1 and not result.stdout, result
            assert "already exists" in result.stderr and str(path) in result.stderr, (
                result
            )
            assert path.read_text(encoding="utf-8") == contents
            assert path.stat().st_ino == identity
    return [
        {
            "existing_configuration": "reported and preserved without parsing or replacement"
        }
    ]


def test_invalid_settings_and_destinations_do_not_create_files(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        file = root / "file"
        file.write_text("not a directory", encoding="utf-8")
        for scenario, arguments, status, diagnostic in (
            ("missing assignment", ("-c", "environment"), 1, "KEY=VALUE"),
            ("unknown field", ("-c", "unknown_setting=true"), 1, "unknown_setting"),
            (
                "environment value type",
                ("-c", "environment.COUNT=42"),
                1,
                "environment",
            ),
            ("unknown profile", ("-c", "sandbox.profile=unknown"), 1, "profile"),
            ("directory is a file", ("--directory", str(file)), 1, "directory"),
            (
                "missing directory",
                ("--directory", str(root / "missing")),
                1,
                "directory",
            ),
            (
                "conflicting destinations",
                ("--global", "--directory", str(root)),
                2,
                "--global",
            ),
        ):
            result = invoke(binary, root, root, "config", "init", *arguments)
            assert result.returncode == status and not result.stdout, result
            assert diagnostic in result.stderr, result
            assert not (root / ".agents").exists()
            assert not (root / "console-home").exists()
            records.append({"rejected": scenario, "files_created": False})
        for flag in ("--no-config", "--no-global-config", "--no-project-config"):
            result = invoke(binary, root, root, flag, "config", "init")
            assert result.returncode == 1 and not result.stdout, result
            assert flag in result.stderr, result
            assert not (root / ".agents").exists()
        result = invoke(
            binary, root, root, "--config-file", str(file), "config", "init"
        )
        assert result.returncode == 1 and not result.stdout, result
        assert "--config-file" in result.stderr, result
        assert not (root / ".agents").exists()
    return records + [{"configuration_source_flags": "rejected before writing"}]


if __name__ == "__main__":
    run_this_suite(__file__)
