#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
STAGE_SCRIPT = ROOT / "scripts" / "stage-sandbox-runner"


def write_executable(path: Path, source: str) -> None:
    path.write_text(code(source), encoding="utf-8")
    path.chmod(0o755)


def bubblewrap_notice(
    pin: dict[str, object], toolchain: str = "fixture-sandbox"
) -> str:
    return (
        "Bubblewrap companion\n\n"
        f"Source archive: https://github.com/{pin['repository']}/archive/{pin['commit']}.tar.gz\n"
        "C source: codex-rs/vendor/bubblewrap\n"
        "Build integration: codex-rs/bwrap\n"
        "License text: bubblewrap-COPYING\n"
        f"Rust toolchain: {toolchain} (codex-rs/rust-toolchain.toml)\n\n"
        "/* Copyright fixture author; SPDX-License-Identifier: LGPL-2.0-or-later */\n"
    )


def helper_metadata(pin: dict[str, object], helper: bytes) -> dict[str, object]:
    return {
        "source_repository": pin["repository"],
        "source_revision": pin["commit"],
        "source_directory": "codex-rs/vendor/bubblewrap",
        "build_script": "codex-rs/bwrap/build.rs",
        "wrapper_directory": "codex-rs/bwrap",
        "sha256": hashlib.sha256(helper).hexdigest(),
        "elf_needed": ["libc.so.6", "libcap.so.2"],
        "libcap_linkage": "dynamic",
    }


def elf_fixture(**changes: object) -> bytes:
    fields = {
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
        "needed": ["libc.so.6"],
        "versions": ["GLIBC_2.34"],
        "runpath": None,
    }
    fields.update(changes)
    return b"\x7fELF" + json.dumps(fields).encode()


def rewrite_wheel(wheel: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(wheel, "w") as archive:
        for info, contents in entries:
            archive.writestr(info, replacements.get(info.filename, contents))


def write_readelf_fixture(commands: Path) -> None:
    write_executable(
        commands / "readelf",
        # fmt: python
        """
        #!/usr/bin/env python3
        import json
        import os
        import sys
        from pathlib import Path

        if sys.argv[1:3] == ["-s", "-W"]:
            assert Path(sys.argv[3]).read_bytes() == b"bwrap bytes with debug symbols"
            if not os.environ.get("FAKE_NO_LIBCAP"):
                print("12: 0000000000100 42 FUNC GLOBAL DEFAULT 15 cap_get_proc")
            raise SystemExit(0)
        blob = Path(sys.argv[-1]).read_bytes()
        if blob.startswith(b"\\x7fELF"):
            fields = json.loads(blob[4:])
            if sys.argv[1:-1] == ["-h", "-l", "-d", "-V", "-W"]:
                print("  Class: ELF64")
                print("  Data: 2's complement, little endian")
                print("  Machine:", fields["machine"])
                if fields["interpreter"]:
                    print("[Requesting program interpreter: " + fields["interpreter"] + "]")
            else:
                assert sys.argv[1:3] == ["-d", "-W"]
            for name in fields["needed"]:
                if name != "libcap.so.2" or not os.environ.get("FAKE_STATIC_LIBCAP"):
                    print(" (NEEDED) Shared library: [" + name + "]")
            if fields["runpath"]:
                print(" (RUNPATH) Library runpath: [" + fields["runpath"] + "]")
            print("Version needs section '.gnu.version_r':")
            for name in fields["versions"]:
                print("  Name: " + name + " Flags: none Version: 2")
        else:
            assert sys.argv[1:3] == ["-d", "-W"]
            assert blob in (b"fixture\\n", b"bwrap bytes")
            print(" (NEEDED) Shared library: [libc.so.6]")
            if not os.environ.get("FAKE_STATIC_LIBCAP"):
                print(" (NEEDED) Shared library: [libcap.so.2]")
    """,
    )


class ReleaseFixture(unittest.TestCase):
    def run_script(
        self,
        *arguments: str,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@unittest.skipUnless(os.name == "posix", "Unix executable and packaging fixtures")
class ReleaseScriptTests(ReleaseFixture):
    def validation_environment(self, directory: Path) -> dict[str, str]:
        (directory / "Cargo.toml").write_text(
            '[package]\nversion = "0.0.2"\n', encoding="utf-8"
        )
        commands = directory / "commands"
        commands.mkdir()
        write_executable(
            commands / "git",
            # fmt: python
            """
            #!/usr/bin/env python3
            import os
            import sys

            arguments = sys.argv[1:]
            if arguments == ["rev-parse", "refs/tags/v0.0.2^{commit}"]:
                print(os.environ["FAKE_RELEASE_COMMIT"])
            elif arguments == ["fetch", "--no-tags", "origin", "main"]:
                pass
            elif arguments == ["rev-parse", "FETCH_HEAD"]:
                print(os.environ["FAKE_MAIN_COMMIT"])
            elif arguments[:2] == ["merge-base", "--is-ancestor"]:
                raise SystemExit(int(os.environ.get("FAKE_ANCESTRY_STATUS", "0")))
            else:
                print(f"unexpected git arguments: {arguments}", file=sys.stderr)
                raise SystemExit(2)
            """,
        )
        write_executable(
            commands / "gh",
            # fmt: python
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            Path(os.environ["FAKE_GH_ARGUMENTS"]).write_text(
                json.dumps(sys.argv[1:]), encoding="utf-8"
            )
            print(os.environ["FAKE_GH_RESPONSE"])
            """,
        )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_TYPE": "tag",
                "GITHUB_REF_NAME": "v0.0.2",
                "GITHUB_REPOSITORY": "t-kalinowski/mcp-console",
                "FAKE_RELEASE_COMMIT": "release-commit",
                "FAKE_MAIN_COMMIT": "main-commit",
                "FAKE_GH_ARGUMENTS": str(directory / "gh-arguments.json"),
            }
        )
        return environment

    def test_validate_publish_requires_successful_main_push_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["FAKE_GH_RESPONSE"] = json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": "release-commit",
                            "event": "push",
                            "head_branch": "main",
                            "conclusion": "success",
                        }
                    ]
                }
            )

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            gh_arguments = json.loads((directory / "gh-arguments.json").read_text())
            self.assertIn(
                "/repos/t-kalinowski/mcp-console/actions/workflows/ci.yaml/runs",
                gh_arguments,
            )
            self.assertIn("head_sha=release-commit", gh_arguments)
            self.assertIn("event=push", gh_arguments)
            self.assertIn("branch=main", gh_arguments)

    def test_validate_publish_rejects_successful_pull_request_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["FAKE_GH_RESPONSE"] = json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": "release-commit",
                            "event": "pull_request",
                            "head_branch": "feature",
                            "conclusion": "success",
                        }
                    ]
                }
            )

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lacks successful CI from a push to main", result.stderr)

    def test_validate_publish_rejects_manual_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
            environment["FAKE_GH_RESPONSE"] = '{"workflow_runs": []}'

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires a push event", result.stderr)

    def test_linux_floor_refreshes_expired_r_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            commands = directory / "commands"
            commands.mkdir()
            # Model Docker's persistent RUN cache and IR's 24-hour expiry at
            # the driver boundary; real cold R preparation takes many minutes.
            write_executable(
                commands / "docker",
                # fmt: python
                """
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                from pathlib import Path

                arguments = sys.argv[1:]
                state_path = Path(os.environ["FLOOR_PREPARATION_STATE"])
                now = int(os.environ["FLOOR_NOW"])
                if arguments[0] == "info":
                    print(os.environ["FLOOR_ARCHITECTURE"])
                elif arguments[0] == "pull":
                    pass  # Pulling an unchanged base does not expire RUN caches.
                elif arguments[0] == "build":
                    mode = arguments[arguments.index("--target") + 1]
                    if mode == "with-r":
                        recipe = (Path(arguments[-1]) / "Dockerfile").read_text()
                        declared = re.findall(r"(?m)^ARG (\\w+)", recipe)
                        key = [
                            arguments[index + 1]
                            for index, argument in enumerate(arguments)
                            if argument == "--build-arg"
                            and arguments[index + 1].split("=", 1)[0] in declared
                        ]
                        state = json.loads(state_path.read_text()) if state_path.exists() else {}
                        if state.get("key") != key or "--no-cache" in arguments:
                            state_path.write_text(json.dumps({"key": key, "prepared": now}))
                elif arguments[:2] == ["image", "inspect"]:
                    print("[]")
                elif arguments[0] == "run":
                    if "--with-r" in arguments:
                        state = json.loads(state_path.read_text())
                        if now - state["prepared"] >= 86400:
                            print(
                                "expired R resolution needs a source build; no compiler in runtime",
                                file=sys.stderr,
                            )
                            raise SystemExit(1)
                else:
                    raise SystemExit(f"unexpected Docker command: {arguments}")
                """,
            )
            wheel = directory / "wheel.whl"
            wheel.touch()
            for architecture in ("x86_64", "aarch64"):
                environment = os.environ | {
                    "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
                    "FLOOR_ARCHITECTURE": architecture,
                    "FLOOR_PREPARATION_STATE": str(directory / f"{architecture}.json"),
                }
                # The wheel and base images are unchanged on the second run;
                # only the cached latest-package resolution has expired.
                for now in (0, 86401):
                    with self.subTest(architecture=architecture, now=now):
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(ROOT / "scripts/check-linux-wheel"),
                                str(wheel),
                                "--target",
                                f"{architecture}-unknown-linux-gnu",
                                "--evidence",
                                str(directory / architecture),
                            ],
                            env=environment | {"FLOOR_NOW": str(now)},
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)

    def smoke_environment(self, directory: Path) -> tuple[dict[str, str], Path, Path]:
        (directory / "Cargo.toml").write_text(
            '[package]\nversion = "0.0.2"\n', encoding="utf-8"
        )
        commands = directory / "commands"
        commands.mkdir()
        tool_directory = directory / "tool" / "mcp-console" / "bin"
        tool_directory.mkdir(parents=True)
        tool_bin = directory / "bin"
        tool_bin.mkdir()

        # fmt: python
        executable_source = """
            #!/usr/bin/env python3
            import hashlib
            import json
            import os
            import signal
            import shutil
            import sys
            from pathlib import Path

            if record := os.environ.get("FAKE_MCP_ARGUMENTS"):
                with open(record, "a") as stream:
                    stream.write(json.dumps(sys.argv[1:]) + "\\n")

            if record := os.environ.get("FAKE_MCP_LOCATIONS"):
                if sys.argv[1] in ("serve", "sandbox"):
                    with open(record, "a") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "command": sys.argv[1],
                                    "cwd": str(Path.cwd()),
                                    "home": os.environ.get("HOME"),
                                    "console": os.environ.get("MCP_CONSOLE_HOME"),
                                    "r_home": os.environ.get("R_HOME"),
                                    "r": shutil.which("R"),
                                    "rscript": shutil.which("Rscript"),
                                    "python": (
                                        str(Path(python).resolve())
                                        if (python := shutil.which("python3"))
                                        else None
                                    ),
                                }
                            )
                            + "\\n"
                        )

            if sys.argv[1:] == ["--version"]:
                print("mcp-console 0.0.2")
            elif sys.argv[1:] == ["--help"]:
                print("mcp-console help")
            elif sys.argv[1:3] == ["sandbox", "--"]:
                if os.environ.get("FAKE_SANDBOX_FAILURE"):
                    print("private sandbox runner failed", file=sys.stderr)
                    raise SystemExit(1)
                Path(os.environ["FAKE_SANDBOX_PATH_RECORD"]).write_text(
                    os.environ.get("PATH", ""), encoding="utf-8"
                )
            elif sys.argv[1:] in (["serve"], ["serve", "--no-sandbox"]):
                initialize = json.loads(sys.stdin.readline())
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": initialize["id"],
                            "result": {
                                "protocolVersion": "2025-11-25",
                                "capabilities": {"tools": {}},
                                "serverInfo": {
                                    "name": "mcp-console",
                                    "version": "0.0.2",
                                },
                            },
                        }
                    ),
                    flush=True,
                )
                json.loads(sys.stdin.readline())
                startup = json.loads(sys.stdin.readline())
                assert startup["params"]["name"] == "send"
                assert set(startup["params"]["arguments"]) == {"timeout_ms"}
                assert startup["params"]["arguments"]["timeout_ms"] > 0
                if os.environ.get("FAKE_MCP_STARTUP_HANG"):
                    signal.pause()
                if failed := os.environ.get("FAKE_MCP_STARTUP_RESULT"):
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": startup["id"],
                                "result": json.loads(failed),
                            }
                        ),
                        flush=True,
                    )
                    signal.pause()
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": startup["id"],
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "\\n[idle]",
                                    }
                                ],
                                "isError": False,
                            },
                        }
                    ),
                    flush=True,
                )
                evaluations = [("python", "42\\n")]
                if not os.environ.get("FAKE_NO_R"):
                    evaluations.append(("r", "[1] 42\\n"))
                for language, output in evaluations:
                    evaluation = json.loads(sys.stdin.readline())
                    assert evaluation["params"] == {
                        "name": "send",
                        "arguments": {language: "6 * 7"},
                    }
                    if os.environ.get("FAKE_MCP_EVALUATION_HANG"):
                        signal.pause()
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": evaluation["id"],
                                "result": {
                                    "content": [{"type": "text", "text": output}],
                                    "isError": False,
                                },
                            }
                        ),
                        flush=True,
                    )
            else:
                raise SystemExit(2)
        """
        executable_source = executable_source.replace(
            "#!/usr/bin/env python3", f"#!{sys.executable}"
        )
        cargo_directory = directory / "cargo-target"
        (cargo_directory / "release").mkdir(parents=True)
        cargo_bin = cargo_directory / "release" / "mcp-console"
        write_executable(cargo_bin, executable_source)
        installed = tool_directory / "mcp-console"
        write_executable(installed, executable_source)
        (tool_bin / "mcp-console").symlink_to(installed)

        write_executable(
            commands / "uv",
            r"""
            #!/bin/sh
            if test "$1 $2 $3 $5" = "tool run --from mcp-console"; then
              case "$6" in
                --version) echo 'mcp-console 0.0.2' ;;
                --help)
                  printf 'mcp-console help\n'
                  if test "${FAKE_UV_HELP_EXTRA_NEWLINE:-}" = 1; then
                    printf '\n'
                  fi
                  ;;
                *) exit 2 ;;
              esac
            elif test "$1 $2" = "tool install"; then
              exit 0
            else
              exit 2
            fi
            """,
        )
        write_executable(
            commands / "R",
            """
            #!/bin/sh
            test "$1" = RHOME
            echo /fake/R
            """,
        )

        write_readelf_fixture(commands)
        shutil.copyfile(ROOT / "sandbox-runner.json", directory / "sandbox-runner.json")
        wheel = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
        self.write_wheel(wheel)
        environment = os.environ.copy()
        environment.pop("R_HOME", None)
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "UV_TOOL_DIR": str(directory / "tool"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
                "FAKE_SANDBOX_PATH_RECORD": str(directory / "sandbox-path.txt"),
            }
        )
        return environment, wheel, cargo_bin

    def write_wheel(
        self,
        wheel: Path,
        *,
        omit: str | None = None,
        executable: bool = True,
        glibc: str = "2.34",
    ) -> None:
        with zipfile.ZipFile(wheel, "w") as archive:
            linux = "linux" in wheel.name
            elf_fields = {"versions": [f"GLIBC_{glibc}"]}
            if linux and "aarch64" in wheel.name:
                elf_fields.update(
                    machine="AArch64", interpreter="/lib/ld-linux-aarch64.so.1"
                )
            archive.writestr(
                "mcp_console-0.0.2.data/scripts/mcp-console",
                elf_fixture(**elf_fields) if linux else b"fixture\n",
            )
            archive.writestr(
                "mcp_console-0.0.2.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: mcp-console\nVersion: 0.0.2\n",
            )
            tag = wheel.name.removesuffix(".whl").split("-", 2)[2]
            archive.writestr(
                "mcp_console-0.0.2.dist-info/WHEEL",
                f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n",
            )
            names = ("mcp-console-sandbox", "LICENSE", "NOTICE")
            if "linux" in wheel.name:
                names += (
                    "bwrap",
                    "bubblewrap-COPYING",
                    "bubblewrap-NOTICE",
                    "bubblewrap-SOURCE.json",
                )
            for name in names:
                if name == omit:
                    continue
                directory = (
                    "libexec"
                    if name in ("mcp-console-sandbox", "bwrap")
                    else "share/licenses/mcp-console"
                )
                info = zipfile.ZipInfo(
                    f"mcp_console-0.0.2.data/data/{directory}/{name}"
                )
                mode = 0o755 if directory == "libexec" and executable else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                contents: str | bytes = "fixture\n"
                if linux and name in ("mcp-console-sandbox", "bwrap"):
                    contents = elf_fixture(
                        **elf_fields,
                        needed=["libc.so.6", "libcap.so.2"]
                        if name == "bwrap"
                        else ["libc.so.6"],
                    )
                if name == "bubblewrap-NOTICE":
                    contents = bubblewrap_notice(
                        json.loads((ROOT / "sandbox-runner.json").read_text())
                    )
                if name == "bubblewrap-SOURCE.json":
                    pin = json.loads((ROOT / "sandbox-runner.json").read_text())
                    contents = json.dumps(
                        helper_metadata(
                            pin,
                            elf_fixture(
                                **elf_fields, needed=["libc.so.6", "libcap.so.2"]
                            ),
                        )
                    )
                archive.writestr(info, contents)

    def test_inspect_wheel_reports_every_shipped_elf_and_matching_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            self.write_wheel(wheel)
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr("mcp_console/extra.so", elf_fixture(interpreter=None))
            report = directory / "abi.json"
            result = self.run_script(
                "inspect-wheel",
                str(wheel),
                "--release",
                "--target",
                "x86_64-unknown-linux-gnu",
                "--report",
                str(report),
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["tags"], ["py3-none-manylinux_2_35_x86_64"])
            self.assertEqual(len(evidence["elf"]), 4)
            self.assertEqual(evidence["elf"]["mcp_console/extra.so"]["glibc"], ["2.34"])
            self.assertIn(
                "libcap.so.2",
                evidence["elf"]["mcp_console-0.0.2.data/data/libexec/bwrap"]["needed"],
            )

    def test_inspect_wheel_rejects_incompatible_private_and_extra_elf(self) -> None:
        defects = (
            ({"versions": ["GLIBC_2.36"]}, "GLIBC_2.36"),
            ({"versions": ["GLIBCXX_3.4.31"]}, "GLIBCXX_3.4.31"),
            ({"versions": ["CXXABI_1.3.14"]}, "CXXABI_1.3.14"),
            ({"machine": "AArch64"}, "machine"),
            ({"interpreter": "/opt/builder/ld-linux.so"}, "interpreter"),
            ({"runpath": "/opt/builder/lib"}, "RUNPATH"),
            ({"needed": ["libssl.so.3"]}, "undeclared dependency"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            for member in (
                "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox",
                "mcp_console/extra.so",
            ):
                for fields, diagnostic in defects:
                    with self.subTest(member=member, defect=fields):
                        self.write_wheel(wheel)
                        if member.endswith("extra.so"):
                            with zipfile.ZipFile(wheel, "a") as archive:
                                archive.writestr(member, elf_fixture(**fields))
                        else:
                            rewrite_wheel(wheel, {member: elf_fixture(**fields)})
                        result = self.run_script(
                            "inspect-wheel",
                            str(wheel),
                            "--release",
                            cwd=directory,
                            env=environment,
                        )
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(diagnostic, result.stderr)
                        self.assertIn(member, result.stderr)

    def test_inspect_wheel_rejects_false_or_inconsistent_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            for platform, metadata_tag, diagnostic in (
                ("manylinux_2_28_x86_64", None, "GLIBC_2.34"),
                ("manylinux_2_39_x86_64", None, "release floor"),
                ("manylinux_2_33_x86_64", None, "unsupported manylinux C++ policy"),
                (
                    "manylinux_2_35_x86_64",
                    "py3-none-manylinux_2_35_aarch64",
                    "WHEEL tags",
                ),
                ("manylinux_2_35_x86_64.manylinux_2_28_x86_64", None, "GLIBC_2.34"),
            ):
                with self.subTest(platform=platform, metadata=metadata_tag):
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    self.write_wheel(wheel)
                    if metadata_tag:
                        rewrite_wheel(
                            wheel,
                            {
                                "mcp_console-0.0.2.dist-info/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {metadata_tag}\n".encode()
                            },
                        )
                    result = self.run_script(
                        "inspect-wheel",
                        str(wheel),
                        "--release",
                        cwd=directory,
                        env=environment,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(diagnostic, result.stderr)

    def test_inspect_wheel_checks_cpp_symbols_against_every_advertised_policy(
        self,
    ) -> None:
        for architecture in ("x86_64", "aarch64"):
            policies = [
                ("manylinux2014", 19, 7),
                (
                    "manylinux_2_26",
                    22 if architecture == "x86_64" else 24,
                    10 if architecture == "x86_64" else 11,
                ),
                ("manylinux_2_31", 28, 12),
                ("manylinux_2_34", 29, 13),
                ("manylinux_2_35", 30, 13),
                ("manylinux_2_35.manylinux_2_34", 29, 13),
                ("manylinux_2_39", 33, 15),
            ]
            if architecture == "x86_64":
                policies[:0] = [("manylinux1", 8, 1), ("manylinux2010", 13, 3)]
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, _, _ = self.smoke_environment(directory)
                for policy, glibcxx, cxxabi in policies:
                    platform = ".".join(
                        f"{tag}_{architecture}" for tag in policy.split(".")
                    )
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    for symbols, rejected in (
                        ([f"GLIBCXX_3.4.{glibcxx}", f"CXXABI_1.3.{cxxabi}"], None),
                        ([f"GLIBCXX_3.4.{glibcxx + 1}"], f"GLIBCXX_3.4.{glibcxx + 1}"),
                        ([f"CXXABI_1.3.{cxxabi + 1}"], f"CXXABI_1.3.{cxxabi + 1}"),
                    ):
                        self.write_wheel(
                            wheel, glibc="2.5" if architecture == "x86_64" else "2.17"
                        )
                        member = "mcp_console/extra.so"
                        with zipfile.ZipFile(wheel, "a") as archive:
                            archive.writestr(
                                member,
                                elf_fixture(
                                    machine="AArch64"
                                    if architecture == "aarch64"
                                    else "Advanced Micro Devices X86-64",
                                    interpreter=None,
                                    needed=["libstdc++.so.6"],
                                    versions=symbols,
                                ),
                            )
                        for release in (False, True):
                            with self.subTest(
                                platform=platform, symbols=symbols, release=release
                            ):
                                result = self.run_script(
                                    "inspect-wheel",
                                    str(wheel),
                                    *(["--release"] if release else []),
                                    cwd=directory,
                                    env=environment,
                                )
                                if release and policy == "manylinux_2_39":
                                    self.assertNotEqual(
                                        result.returncode, 0, result.stdout
                                    )
                                    self.assertIn("release floor", result.stderr)
                                elif rejected:
                                    self.assertNotEqual(
                                        result.returncode, 0, result.stdout
                                    )
                                    self.assertIn(rejected, result.stderr)
                                    self.assertIn(member, result.stderr)
                                else:
                                    self.assertEqual(
                                        result.returncode, 0, result.stderr
                                    )

    def test_smoke_wheel_requires_a_private_companion_bundle(self) -> None:
        for defect in (
            "missing runner",
            "missing notice",
            "not executable",
            "public wheel",
            "public uv",
        ):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                environment, wheel, cargo_bin = self.smoke_environment(directory)
                if defect in {"missing runner", "missing notice", "not executable"}:
                    self.write_wheel(
                        wheel,
                        omit={
                            "missing runner": "mcp-console-sandbox",
                            "missing notice": "NOTICE",
                        }.get(defect),
                        executable=defect != "not executable",
                    )
                elif defect == "public wheel":
                    with zipfile.ZipFile(wheel, "a") as archive:
                        archive.writestr(
                            "mcp_console-0.0.2.data/scripts/mcp-console-sandbox",
                            "fixture\n",
                        )
                else:
                    (
                        Path(environment["UV_TOOL_BIN_DIR"]) / "mcp-console-sandbox"
                    ).touch()
                result = self.run_script(
                    "smoke-wheel",
                    str(wheel),
                    str(cargo_bin),
                    cwd=directory,
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("private sandbox runner", result.stderr)

    def test_smoke_wheel_isolates_console_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            record = directory / "locations.jsonl"
            ambient = directory / "ambient-console"
            ambient.mkdir()
            (directory / ".agents/console").mkdir(parents=True)
            environment |= {
                "FAKE_MCP_LOCATIONS": str(record),
                "MCP_CONSOLE_HOME": str(ambient),
            }
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            launches = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertEqual(
                [item["command"] for item in launches], ["sandbox", "sandbox", "serve"]
            )
            for launch in launches:
                self.assertEqual(launch["home"], environment.get("HOME"))
                self.assertNotEqual(launch["cwd"], str(directory.resolve()))
                self.assertNotEqual(launch["console"], str(ambient))
                self.assertTrue(Path(launch["console"]).is_absolute())
                self.assertFalse(Path(launch["cwd"]).exists())
                self.assertFalse(Path(launch["console"]).exists())

    def test_smoke_wheel_requires_runnable_cargo_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            environment["FAKE_SANDBOX_FAILURE"] = "1"

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("private sandbox runner failed", result.stderr)

    def test_smoke_installed_wheel_needs_no_checkout_or_build_output(self) -> None:
        for without_r in (False, True):
            with (
                self.subTest(without_r=without_r),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                environment, _, cargo_bin = self.smoke_environment(directory)
                wheel = (
                    directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
                )
                self.write_wheel(wheel)
                cargo_bin.unlink()
                (directory / "Cargo.toml").unlink()
                empty = directory / "empty-workspace"
                empty.mkdir()
                record = directory / "arguments.jsonl"
                environment["FAKE_MCP_ARGUMENTS"] = str(record)
                if without_r:
                    environment["FAKE_NO_R"] = "1"
                result = self.run_script(
                    "smoke-wheel",
                    str(wheel),
                    "--installed-only",
                    "--sandbox-pin",
                    str(directory / "sandbox-runner.json"),
                    *(["--without-r"] if without_r else []),
                    cwd=empty,
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in record.read_text().splitlines()]
                self.assertEqual(calls.count(["sandbox", "--", "/usr/bin/true"]), 1)
                self.assertIn(["serve"], calls)
                self.assertEqual(list(empty.iterdir()), [])

    def test_smoke_wheel_evaluates_peers_and_bounds_response_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            sandbox_path = Path(environment["FAKE_SANDBOX_PATH_RECORD"]).read_text()
            self.assertEqual(len(sandbox_path.split(os.pathsep)), 1)
            self.assertNotEqual(sandbox_path, environment["PATH"])
            environment["FAKE_UV_HELP_EXTRA_NEWLINE"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("installed and Cargo help output differ", result.stderr)
            del environment["FAKE_UV_HELP_EXTRA_NEWLINE"]

            environment["FAKE_MCP_EVALUATION_HANG"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "0.01",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out after 0.01 seconds", result.stderr)

    def test_smoke_wheel_evaluates_r_selected_only_by_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            commands = directory / "commands"
            r_home = directory / "selected-R"
            (r_home / "bin").mkdir(parents=True)
            (commands / "R").rename(r_home / "bin" / "R")
            environment.update(PATH=str(commands), R_HOME=str(r_home))
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_wheel_accepts_a_host_without_r(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            commands = directory / "commands"
            (commands / "R").unlink()
            environment.update(PATH=str(commands), FAKE_NO_R="1")
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_wheel_without_r_hides_host_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            record = directory / "launches.jsonl"
            environment.update(
                R_HOME="/inherited/R",
                FAKE_NO_R="1",
                FAKE_MCP_LOCATIONS=str(record),
            )
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--without-r",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            launch = json.loads(record.read_text().splitlines()[-1])
            self.assertEqual(launch["command"], "serve")
            self.assertIsNone(launch["r_home"])
            self.assertIsNone(launch["r"])
            self.assertIsNone(launch["rscript"])
            self.assertEqual(launch["python"], str(Path(sys.executable).resolve()))

    def test_smoke_wheel_reports_startup_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            payload = {
                "content": [{"type": "text", "text": "startup failed: café"}],
                "isError": True,
            }
            environment["FAKE_MCP_STARTUP_RESULT"] = json.dumps(payload)
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 1)
            response = {"jsonrpc": "2.0", "id": 2, "result": payload}
            self.assertIn(
                "unexpected runtime startup response: "
                + json.dumps(response, ensure_ascii=False),
                result.stderr,
            )

    def test_smoke_wheel_bounds_runtime_startup_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            environment["FAKE_MCP_STARTUP_HANG"] = "1"

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "0.01",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out after 1 seconds", result.stderr)

    def test_smoke_linux_wheel_requires_sandbox_and_bundled_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            linux_wheel = wheel.with_name(
                "mcp_console-0.0.2-py3-none-manylinux_2_34_x86_64.whl"
            )
            self.write_wheel(linux_wheel)
            wheel.unlink()
            record = directory / "arguments.jsonl"
            environment["FAKE_MCP_ARGUMENTS"] = str(record)
            command = (
                "smoke-wheel",
                str(linux_wheel),
                str(cargo_bin),
                "--target",
                "x86_64-unknown-linux-gnu",
            )
            result = self.run_script(
                *command,
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            invocations = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertIn(["serve"], invocations)
            self.assertTrue(any(call[:1] == ["sandbox"] for call in invocations))

            for missing in (
                "mcp-console-sandbox",
                "bwrap",
                "bubblewrap-COPYING",
                "bubblewrap-NOTICE",
                "bubblewrap-SOURCE.json",
            ):
                with self.subTest(missing=missing):
                    self.write_wheel(linux_wheel, omit=missing)
                    result = self.run_script(*command, cwd=directory, env=environment)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("private sandbox runner", result.stderr)

            for field, value in (
                ("source_revision", "b" * 40),
                ("source_repository", "wrong/source"),
                ("source_directory", "old/bubblewrap"),
                ("build_script", "old/build.rs"),
                ("wrapper_directory", "old/wrapper"),
                ("sha256", "0" * 64),
                ("elf_needed", ["libc.so.6"]),
                ("libcap_linkage", "static"),
            ):
                with self.subTest(field=field):
                    self.write_wheel(linux_wheel)
                    with zipfile.ZipFile(linux_wheel) as archive:
                        entries = [
                            (info, archive.read(info)) for info in archive.infolist()
                        ]
                    with zipfile.ZipFile(linux_wheel, "w") as archive:
                        for info, contents in entries:
                            if info.filename.endswith("bubblewrap-SOURCE.json"):
                                metadata = json.loads(contents)
                                metadata[field] = value
                                contents = json.dumps(metadata).encode()
                            archive.writestr(info, contents)
                    result = self.run_script(*command, cwd=directory, env=environment)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("provenance", result.stderr)

            for notice_name, replacement in (
                ("bubblewrap-NOTICE", b"notice for obsolete source"),
                ("bubblewrap-NOTICE", b""),
                ("bubblewrap-COPYING", b""),
            ):
                self.write_wheel(linux_wheel)
                with zipfile.ZipFile(linux_wheel) as archive:
                    entries = [
                        (info, archive.read(info)) for info in archive.infolist()
                    ]
                with zipfile.ZipFile(linux_wheel, "w") as archive:
                    for info, contents in entries:
                        if info.filename.endswith("/" + notice_name):
                            contents = replacement
                        archive.writestr(info, contents)
                result = self.run_script(*command, cwd=directory, env=environment)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn(notice_name, result.stderr)

            for missing_notice in (True, False):
                self.write_wheel(linux_wheel)
                with zipfile.ZipFile(linux_wheel) as archive:
                    entries = [
                        (info, archive.read(info)) for info in archive.infolist()
                    ]
                with zipfile.ZipFile(linux_wheel, "w") as archive:
                    for info, contents in entries:
                        if info.filename.endswith("bubblewrap-SOURCE.json"):
                            metadata = json.loads(contents)
                            metadata.update(
                                libcap_linkage="static", elf_needed=["libc.so.6"]
                            )
                            contents = json.dumps(metadata).encode()
                        archive.writestr(info, contents)
                    if not missing_notice:
                        archive.writestr(
                            "mcp_console-0.0.2.data/data/share/licenses/mcp-console/libcap-NOTICE",
                            "fixture libcap notice",
                        )
                result = self.run_script(
                    *command,
                    cwd=directory,
                    env=environment | {"FAKE_STATIC_LIBCAP": "1"},
                )
                if missing_notice:
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("libcap-NOTICE", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                # The same notice must not describe a dynamic dependency as bundled.
                result = self.run_script(*command, cwd=directory, env=environment)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_stage_runner_exports_source_pin_without_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            script = root / "scripts" / STAGE_SCRIPT.name
            shutil.copyfile(STAGE_SCRIPT, script)
            shutil.copyfile(
                ROOT / "scripts/checkout_workflow.py",
                root / "scripts/checkout_workflow.py",
            )
            pin = {"repository": "fixture/runner", "commit": "a" * 40}
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            output = root / "github-output"
            result = subprocess.run(
                [sys.executable, str(script), "--github-output", str(output)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(),
                "".join(f"{key}={value}\n" for key, value in pin.items()),
            )

    def test_stage_runner_builds_the_pin_and_records_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "project"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(STAGE_SCRIPT, scripts / STAGE_SCRIPT.name)
            shutil.copyfile(
                ROOT / "scripts/checkout_workflow.py",
                root / "scripts/checkout_workflow.py",
            )
            placeholder = root / "wheel-data/data/.gitignore"
            placeholder.parent.mkdir(parents=True)
            placeholder.write_text("/*\n!/.gitignore\n")
            # The toolchain below is fake; exercise its macOS staging contract
            # on every test host without building or executing a native runner.
            launcher = scripts / "macos-stage.py"
            write_executable(
                launcher,
                # fmt: python
                """
                import runpy
                import sys

                sys.platform = "darwin"
                sys.argv.pop(0)
                runpy.run_path(sys.argv[0], run_name="__main__")
                """,
            )
            pin = {
                "repository": "t-kalinowski/codex",
                "release": "rust-v0.150.1",
                "commit": "a" * 40,
                "protocol_version": 2,
            }
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            checkout = directory / "source"
            crate = checkout / "codex-rs" / "mcp-console-sandbox"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").touch()
            toolchain_file = checkout / "codex-rs/rust-toolchain.toml"
            toolchain_file.write_text('[toolchain]\nchannel = "fixture-sandbox"\n')
            (checkout / "LICENSE").write_text("license\n")
            (checkout / "NOTICE").write_text("notice\n")
            vendor = checkout / "codex-rs/vendor/bubblewrap"
            vendor.mkdir(parents=True)
            (vendor / "COPYING").write_text("bwrap license\n")
            (vendor / "bubblewrap.c").write_text(
                "/* Copyright fixture author; SPDX-License-Identifier: LGPL-2.0-or-later */\n"
            )
            commands = directory / "commands"
            commands.mkdir()
            write_executable(
                commands / "git",
                # fmt: python
                """
                #!/usr/bin/env python3
                import os
                import sys

                if sys.argv[1:] == ["rev-parse", "HEAD"]:
                    print(os.environ["FAKE_SOURCE_REVISION"])
                elif sys.argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
                    print(os.environ.get("FAKE_SOURCE_DIRTY", ""), end="")
                else:
                    raise SystemExit(2)
                """,
            )
            write_executable(
                commands / "rustc",
                # fmt: python
                """
                #!/usr/bin/env python3
                import sys

                assert sys.argv[1:] == ["+fixture-sandbox", "--print", "host-tuple"]
                print("aarch64-apple-darwin")
                """,
            )
            write_executable(
                commands / "cargo",
                # fmt: python
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                with Path(os.environ["FAKE_CARGO_ARGUMENTS"]).open("a") as record:
                    record.write(
                        json.dumps(
                            {
                                "arguments": sys.argv[1:],
                                "helper_sha256": os.environ.get("CODEX_BWRAP_SHA256"),
                            }
                        )
                        + "\\n"
                    )
                target = (
                    sys.argv[sys.argv.index("--target") + 1]
                    if "--target" in sys.argv
                    else os.environ["CARGO_BUILD_TARGET"]
                )
                output = Path(os.environ["CARGO_TARGET_DIR"]) / target / "release"
                output.mkdir(parents=True, exist_ok=True)
                (output / "mcp-console-sandbox").write_bytes(b"runner bytes with debug symbols")
                (output / "bwrap").write_bytes(b"bwrap bytes with debug symbols")
                """,
            )
            write_executable(
                commands / "rustup",
                # fmt: python
                """
                #!/usr/bin/env python3
                import os
                import sys

                assert sys.argv[1:4] == ["run", "--install", "fixture-sandbox"]
                os.execvp(sys.argv[4], [sys.argv[4], "+fixture-sandbox", *sys.argv[5:]])
                """,
            )
            write_readelf_fixture(commands)
            for name in ("xcrun", "strip"):
                write_executable(
                    commands / name,
                    # fmt: python
                    """
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    executable = Path(sys.argv[-1])
                    original = executable.read_bytes()
                    assert original.endswith(b" with debug symbols")
                    executable.write_bytes(original.removesuffix(b" with debug symbols"))
                    """,
                )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "FAKE_SOURCE_REVISION": pin["commit"],
                    "FAKE_CARGO_ARGUMENTS": str(directory / "cargo.json"),
                    "CARGO_BUILD_TARGET": "x86_64-apple-darwin",
                    "RUSTUP_TOOLCHAIN": "fixture-console",
                    "LIBCAP_STATIC": "1",  # Requested linkage is not ELF evidence.
                }
            )
            command = [
                sys.executable,
                str(launcher),
                str(scripts / STAGE_SCRIPT.name),
                str(checkout),
            ]

            def reject_build_overrides() -> None:
                for name, value in (
                    ("CODEX_BWRAP_SOURCE_DIR", ""),
                    ("CODEX_BWRAP_SOURCE_DIR", str(vendor)),
                    ("CODEX_BWRAP_SOURCE_DIR", str(directory / "alternative")),
                    ("CODEX_SKIP_BWRAP_BUILD", "1"),
                    ("CODEX_SKIP_BWRAP_BUILD", ""),
                ):
                    with self.subTest(override=name, value=value):
                        log = directory / "cargo.json"
                        log.unlink(missing_ok=True)
                        result = subprocess.run(
                            command + ["--target", "x86_64-unknown-linux-gnu"],
                            env=environment | {name: value},
                            capture_output=True,
                            text=True,
                        )
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertIn(name, result.stderr)
                        self.assertFalse(
                            log.exists(), "must reject before Cargo can reuse a helper"
                        )

            (directory / "alternative").mkdir()
            reject_build_overrides()  # Clean staging, no helper exists yet.
            for arguments, target in (
                ([], "aarch64-apple-darwin"),
                (["--target", "x86_64-unknown-linux-gnu"], "x86_64-unknown-linux-gnu"),
                (
                    ["--target", "aarch64-unknown-linux-gnu"],
                    "aarch64-unknown-linux-gnu",
                ),
                (["--target", "x86_64-apple-darwin"], "x86_64-apple-darwin"),
            ):
                with self.subTest(target=target):
                    (directory / "cargo.json").unlink(missing_ok=True)
                    result = subprocess.run(
                        command + arguments,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(placeholder.read_text(), "/*\n!/.gitignore\n")
                    packages = [("codex-mcp-console-sandbox", "mcp-console-sandbox")]
                    if "linux" in target:
                        packages.insert(0, ("codex-bwrap", "bwrap"))
                    self.assertEqual(
                        [
                            json.loads(line)
                            for line in (directory / "cargo.json")
                            .read_text()
                            .splitlines()
                        ],
                        [
                            {
                                "arguments": [
                                    "+fixture-sandbox",
                                    "build",
                                    "--locked",
                                    "--release",
                                    "-p",
                                    package,
                                    "--bin",
                                    binary,
                                    "--target",
                                    target,
                                ],
                                "helper_sha256": hashlib.sha256(
                                    b"bwrap bytes"
                                ).hexdigest()
                                if "linux" in target and binary == "mcp-console-sandbox"
                                else None,
                            }
                            for package, binary in packages
                        ],
                    )
                    self.assertEqual(
                        json.loads(
                            (root / "target/sandbox-runner-build.json").read_text()
                        ),
                        {
                            "source_revision": pin["commit"],
                            "target": target,
                            "artifacts": {
                                "LICENSE": hashlib.sha256(b"license\n").hexdigest(),
                                "NOTICE": hashlib.sha256(b"notice\n").hexdigest(),
                                "mcp-console-sandbox": hashlib.sha256(
                                    b"runner bytes"
                                ).hexdigest(),
                                **(
                                    {
                                        "bubblewrap-SOURCE.json": hashlib.sha256(
                                            (
                                                json.dumps(
                                                    helper_metadata(
                                                        pin, b"bwrap bytes"
                                                    ),
                                                    indent=2,
                                                )
                                                + "\n"
                                            ).encode()
                                        ).hexdigest(),
                                        "bubblewrap-NOTICE": hashlib.sha256(
                                            bubblewrap_notice(pin).encode()
                                        ).hexdigest(),
                                        "bubblewrap-COPYING": hashlib.sha256(
                                            b"bwrap license\n"
                                        ).hexdigest(),
                                        "bwrap": hashlib.sha256(
                                            b"bwrap bytes"
                                        ).hexdigest(),
                                    }
                                    if "linux" in target
                                    else {}
                                ),
                            },
                        },
                    )
                    data = root / "wheel-data/data"
                    if "linux" in target:
                        reject_build_overrides()  # Incremental staging with a finished helper.
                    unchanged = [
                        root / "target/sandbox-runner-build.json",
                        *(path for path in data.rglob("*") if path.is_file()),
                    ]
                    for path in unchanged:
                        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                    obsolete = data / "libexec/obsolete-runner"
                    obsolete.write_bytes(b"obsolete")
                    if "linux" in target:
                        notice = (
                            data / "share/licenses/mcp-console/bubblewrap-SOURCE.json"
                        )
                        notice.write_text('{"source_revision": "stale"}')
                        unchanged.remove(notice)
                    result = subprocess.run(
                        command + arguments,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(obsolete.exists())
                    if "linux" in target:
                        self.assertEqual(
                            json.loads(notice.read_text()),
                            helper_metadata(pin, b"bwrap bytes"),
                        )
                    self.assertEqual(
                        {
                            path.relative_to(root): path.stat().st_mtime_ns
                            for path in unchanged
                        },
                        {path.relative_to(root): 1_000_000_000 for path in unchanged},
                    )
            data = root / "wheel-data" / "data"
            runner = data / "libexec" / "mcp-console-sandbox"
            self.assertEqual(runner.read_bytes(), b"runner bytes")
            self.assertTrue(os.access(runner, os.X_OK))
            self.assertEqual(
                (data / "share/licenses/mcp-console/LICENSE").read_text(),
                "license\n",
            )
            self.assertEqual(
                (
                    checkout
                    / "codex-rs/target/x86_64-apple-darwin/release/mcp-console-sandbox"
                ).read_bytes(),
                b"runner bytes with debug symbols",
            )
            (directory / "cargo.json").unlink()
            for changes in (
                {"FAKE_SOURCE_REVISION": "b" * 40},
                {"FAKE_SOURCE_DIRTY": " M Cargo.lock"},
            ):
                result = subprocess.run(
                    command, env=environment | changes, capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "cargo.json").exists())
            static_notice = directory / "libcap notice"
            static_notice.write_text("fixture libcap redistribution notice\n")
            for changes, expected in (
                ({}, "MCP_CONSOLE_LIBCAP_NOTICE"),
                (
                    {
                        "MCP_CONSOLE_LIBCAP_NOTICE": str(static_notice),
                        "FAKE_NO_LIBCAP": "1",
                    },
                    "cannot establish libcap linkage",
                ),
                ({"MCP_CONSOLE_LIBCAP_NOTICE": str(static_notice)}, None),
            ):
                with self.subTest(static=changes):
                    result = subprocess.run(
                        command + ["--target", "x86_64-unknown-linux-gnu"],
                        env=environment | {"FAKE_STATIC_LIBCAP": "1"} | changes,
                        capture_output=True,
                        text=True,
                    )
                    if expected:
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertIn(expected, result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        notice = data / "share/licenses/mcp-console/libcap-NOTICE"
                        self.assertEqual(
                            notice.read_bytes(), static_notice.read_bytes()
                        )
                        metadata = json.loads(
                            (
                                data
                                / "share/licenses/mcp-console/bubblewrap-SOURCE.json"
                            ).read_text()
                        )
                        self.assertEqual(metadata["libcap_linkage"], "static")
                        self.assertEqual(metadata["elf_needed"], ["libc.so.6"])
            # A later dynamic build must remove the static library notice.
            result = subprocess.run(
                command + ["--target", "x86_64-unknown-linux-gnu"],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(
                (data / "share/licenses/mcp-console/libcap-NOTICE").exists()
            )
            (directory / "cargo.json").unlink()
            toolchain_file.unlink()
            result = subprocess.run(
                command, env=environment, capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FileNotFoundError", result.stderr)
            self.assertIn("rust-toolchain.toml", result.stderr)
            self.assertFalse((directory / "cargo.json").exists())

    def test_cargo_rejects_changed_staged_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src/main.rs").write_text("fn main() {}\n")
            # Exercise the build script with empty native sources at their real paths.
            for native_source in (ROOT / "src").rglob("*.c"):
                destination = root / native_source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch()
            shutil.copyfile(ROOT / "build.rs", root / "build.rs")
            dependencies = tomllib.loads((ROOT / "Cargo.toml").read_text())[
                "build-dependencies"
            ]
            (root / "Cargo.toml").write_text(
                """[package]
name = "sandbox-artifact-build"
version = "0.0.0"
"""
                """edition = "2024"
[build-dependencies]
"""
                + "".join(
                    f"{name} = {json.dumps(version)}\n"
                    for name, version in dependencies.items()
                )
            )
            shutil.copyfile(ROOT / "Cargo.lock", root / "Cargo.lock")
            target = subprocess.check_output(
                ["rustc", "--print", "host-tuple"], text=True
            ).strip()
            pin = json.loads((ROOT / "sandbox-runner.json").read_text())
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            artifacts = {
                "mcp-console-sandbox": b"runner bytes",
                "LICENSE": b"license",
                "NOTICE": b"notice",
            }
            if sys.platform == "linux":
                artifacts["bwrap"] = b"bwrap bytes"
                artifacts["bubblewrap-COPYING"] = b"bwrap license"
                artifacts["bubblewrap-NOTICE"] = bubblewrap_notice(pin).encode()
                artifacts["bubblewrap-SOURCE.json"] = json.dumps(
                    helper_metadata(pin, b"bwrap bytes")
                ).encode()
            staged = root / "wheel-data/data"
            staged_files = {}
            for name, contents in artifacts.items():
                relative = (
                    "libexec"
                    if name in ("mcp-console-sandbox", "bwrap")
                    else "share/licenses/mcp-console"
                )
                staged_files[name] = staged / relative / name
                staged_files[name].parent.mkdir(parents=True, exist_ok=True)
                staged_files[name].write_bytes(contents)
                staged_files[name].chmod(0o755)
            (root / "target").mkdir()
            (root / "target/sandbox-runner-build.json").write_text(
                json.dumps(
                    {
                        "source_revision": pin["commit"],
                        "target": target,
                        "artifacts": {
                            name: hashlib.sha256(contents).hexdigest()
                            for name, contents in artifacts.items()
                        },
                    }
                )
            )
            arguments = [
                "cargo",
                "build",
                "--target",
                target,
                "--target-dir",
                str(root / "target"),
            ]
            result = subprocess.run(arguments, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Let Cargo observe companions created by the initial build, then
            # require the next unchanged invocation to reuse the executable.
            result = subprocess.run(arguments, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [*arguments, "--message-format=json"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            executable = [
                event
                for line in result.stdout.splitlines()
                if (event := json.loads(line)).get("executable")
            ]
            self.assertEqual(len(executable), 1, result.stdout)
            self.assertTrue(executable[0]["fresh"], result.stderr)
            prefix = root / "target" / target
            for name, contents in artifacts.items():
                with self.subTest(artifact=name):
                    relative = (
                        "libexec"
                        if name in ("mcp-console-sandbox", "bwrap")
                        else "share/licenses/mcp-console"
                    )
                    installed = prefix / relative / name
                    self.assertEqual(installed.read_bytes(), contents)
                    self.assertTrue(os.access(installed, os.X_OK))
                    staged_files[name].write_bytes(b"replaced artifact")
                    result = subprocess.run(
                        arguments, cwd=root, capture_output=True, text=True
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"artifact {name} changed", result.stderr)
                    self.assertEqual(installed.read_bytes(), contents)
                    staged_files[name].write_bytes(contents)


class PortableReleaseTests(ReleaseFixture):
    def test_verify_wheel_set_requires_macos_and_linux_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            arm64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
            x86_64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_x86_64.whl"
            arm64.touch()
            x86_64.touch()
            for architecture in ("aarch64", "x86_64"):
                (
                    directory
                    / f"mcp_console-0.0.2-py3-none-manylinux_2_39_{architecture}.whl"
                ).touch()

            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)

            x86_64.unlink()
            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected exactly four wheels", result.stderr)


@unittest.skipUnless(os.name == "posix", "Unix Rscript executable fixture")
class RuntimeSourceValidationTests(unittest.TestCase):
    def test_r_home_selects_the_syntax_checker_without_r_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "src", root / "src")
            (root / "scripts").mkdir()
            script = root / "scripts/validate_runtime_sources.py"
            shutil.copyfile(ROOT / "scripts/validate_runtime_sources.py", script)
            shutil.copyfile(
                ROOT / "scripts/checkout_workflow.py",
                root / "scripts/checkout_workflow.py",
            )
            r_home = root / "selected-R"
            (r_home / "bin").mkdir(parents=True)
            write_executable(
                r_home / "bin/Rscript",
                # fmt: python
                f"""
                #!{sys.executable}
                import sys

                assert sys.argv[1:3] == ["--vanilla", "-e"]
                print("selected R syntax checker rejected source", file=sys.stderr)
                raise SystemExit(1)
                """,
            )
            environment = {**os.environ, "PATH": str(root), "R_HOME": str(r_home)}
            result = subprocess.run(
                [sys.executable, str(script)],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(
                "src/python/bridge.R: selected R syntax checker rejected source",
                result.stderr,
            )
            self.assertNotIn("checks skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
