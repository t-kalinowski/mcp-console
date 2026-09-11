#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite

CONFIG = ".agents/console/config.yaml"
PROTECTED = (".git", ".agents", ".codex", ".claude")


def configure(host: Path, source: str) -> None:
    config = host / CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(source, encoding="utf-8")


def run(
    binary: Path,
    host: Path,
    exercise: str,
    *options: str,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    result = subprocess.run(
        [binary, "sandbox", *options, "--", sys.executable, "-c", exercise],
        cwd=host,
        env=os.environ | (environment or {}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result
    assert result.stderr == "", result
    return {"stdout": result.stdout, "stderr": result.stderr}


@requires(SANDBOX)
def test_workspace_yaml_forms_protect_metadata_and_private_temporary_storage(
    binary: Path,
) -> Transcript:
    exercise = code(r"""
        import errno
        import os
        from pathlib import Path
        import subprocess

        def denied(operation, *args, **kwargs):
            try:
                operation(*args, **kwargs)
            except OSError as error:
                # Native Linux mounts can deny replacement across a mount boundary.
                assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.EBUSY, errno.EXDEV), error
            else:
                raise AssertionError(f"unexpected success: {operation} {args}")

        ordinary = Path("ordinary")
        _ = ordinary.write_text("created")
        _ = ordinary.write_text("edited")
        assert ordinary.read_text() == "edited"
        ordinary.unlink()
        for name in (".git", ".agents", ".codex", ".claude"):
            protected = Path(name)
            keep = protected / "keep"
            assert keep.read_text() == "readable"
            denied(keep.write_text, "changed")
            denied((protected / "new").write_text, "created")
            denied(keep.unlink)
            denied(protected.rename, Path(name + "-moved"))
            replacement = Path("replacement")
            _ = replacement.write_text("replacement")
            denied(os.replace, replacement, keep)
            replacement.unlink()
            empty = protected / "empty"
            denied(empty.rmdir)
            replacement.mkdir()
            denied(os.replace, replacement, empty)
            replacement.rmdir()
            for action in ("write", "delete", "rename"):
                child = subprocess.run([os.environ["TEST_PYTHON"], "-c", '''
        from pathlib import Path
        import sys
        p = Path(sys.argv[1])
        if sys.argv[2] == "write": p.write_text("child")
        if sys.argv[2] == "delete": p.unlink()
        if sys.argv[2] == "rename": p.parent.rename(p.parent.with_name("moved"))
        ''', str(keep), action], capture_output=True, text=True)
                assert child.returncode != 0, (name, action, child)
                assert "PermissionError" in child.stderr or "OSError" in child.stderr, child
            assert keep.read_text() == "readable"
        for root in (os.environ["INHERITED_TEMP"], os.environ["SHARED_TEMP"]):
            denied((Path(root) / "outside").write_text, "blocked")
        private = Path(os.environ["TMPDIR"])
        _ = (private / "private").write_text("writable")
        print("project edits; readable metadata; denied writes, creation, deletion, replacement and child mutations; private-only temp")
        """)
    transcript = []
    for quoted in (True, False):
        with (
            TemporaryDirectory() as directory,
            TemporaryDirectory() as inherited,
            TemporaryDirectory(dir="/tmp") as shared,
        ):
            host = Path(directory).resolve() / "workspace"
            host.mkdir()
            for name in PROTECTED:
                (host / name / "empty").mkdir(parents=True)
                (host / name / "keep").write_text("readable")
            source = 'extends: ":workspace"\n' if quoted else "extends: :workspace\n"
            # Both existing writable-root interfaces must preserve exclusions.
            options = ("--writable-root", "..") if quoted else ()
            if not quoted:
                source += code("""
                    sandbox:
                      filesystem:
                        entries:
                          - path: {type: path, path: ..}
                            access: write
                    """)
            configure(host, source)
            result = run(
                binary,
                host,
                exercise,
                *options,
                environment={
                    "TEST_PYTHON": sys.executable,
                    "TMPDIR": inherited,
                    "INHERITED_TEMP": inherited,
                    "SHARED_TEMP": shared,
                },
            )
            transcript.append(
                {"configuration": source, "cli_writable_parent": quoted, **result}
            )
    return transcript


@requires(SANDBOX)
def test_absent_metadata_stays_absent_and_git_worktree_indirection_is_native(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        configure(host, 'extends: ":workspace"\n')
        absent = run(
            binary,
            host,
            code(r"""
            import errno
            from pathlib import Path
            for name in (".git", ".codex", ".claude"):
                try:
                    (Path(name) / "new").mkdir(parents=True)
                except OSError as error:
                    # A native Linux placeholder can represent an absent path as a file.
                    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTDIR), error
                else:
                    raise AssertionError(name)
            print("absent metadata cannot be created")
            """),
        )
        assert all(not (host / name).exists() for name in (".git", ".codex", ".claude"))
        metadata = host / "worktree-metadata"
        metadata.mkdir()
        (metadata / "HEAD").write_text("ref: refs/heads/main\n")
        (host / ".git").write_text("gitdir: worktree-metadata\n")
        (host / "alias").symlink_to(metadata, target_is_directory=True)
        indirect = run(
            binary,
            host,
            code(r"""
            import errno
            from pathlib import Path
            for path in (Path(".git"), Path("worktree-metadata/HEAD"), Path("alias/HEAD")):
                assert path.read_text()
                try:
                    _ = path.write_text("changed")
                except OSError as error:
                    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                else:
                    raise AssertionError(path)
            print("git pointer and referenced metadata remain readable and protected through aliases")
            """),
        )
        return [{"absent": absent, "worktree": indirect}]


@requires(SANDBOX)
def test_profile_adjustments_keep_native_write_exceptions_and_read_denials(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        for name in PROTECTED:
            (host / name / "allowed").mkdir(parents=True)
            (host / name / "keep").write_text("readable")
        source = code("""
            extends: ":workspace"
            sandbox:
              filesystem:
                kind: {restricted: null}
                future_native_field: true
                entries:
                  - path: {type: path, path: .claude}
                    access: write
                  - path: {type: path, path: .agents/allowed}
                    access: write
                  - path: {type: path, path: .git/keep}
                    access: deny
                  - path: {type: special, value: {kind: unknown, path: ":future"}}
                    access: read
            """)
        configure(host, source)
        result = run(
            binary,
            host,
            code(r"""
            import errno
            from pathlib import Path
            for name in (".claude/new", ".agents/allowed/new", ".codex/new"):
                _ = Path(name).write_text("deliberate exception")
            try:
                _ = Path(".git/keep").read_text()
            except OSError as error:
                assert error.errno in (errno.EACCES, errno.EPERM), error
            else:
                raise AssertionError("read denial ignored")
            assert Path(".agents/keep").read_text() == "readable"
            print("equal-path and descendant write exceptions, native field forwarding and read denial verified")
            """),
            "--writable-root",
            ".codex",
        )
        return [{"configuration": source, **result}]


@requires(SANDBOX)
def test_read_only_and_explicit_filesystem_kinds_preserve_their_meaning(
    binary: Path,
) -> Transcript:
    transcript = []
    for selector in ('":read-only"', ":read-only"):
        with TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            configure(host, f"extends: {selector}\n")
            transcript.append(
                run(
                    binary,
                    host,
                    code(r"""
                import errno
                import os
                from pathlib import Path
                try:
                    _ = Path("blocked").write_text("no")
                except OSError as error:
                    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                else:
                    raise AssertionError("read-only allowed project write")
                _ = (Path(os.environ["TMPDIR"]) / "allowed").write_text("private")
                print("read-only workspace and writable private temp")
                """),
                )
            )
    for kind in ("unrestricted", "external-sandbox"):
        with TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            source = (
                f'extends: ":workspace"\nsandbox:\n  filesystem: {{kind: {kind}}}\n'
            )
            configure(host, source)
            transcript.append(
                {
                    "configuration": source,
                    **run(
                        binary,
                        host,
                        code(r"""
                from pathlib import Path
                _ = Path(".agents/console/explicit").write_text("full filesystem access")
                print("explicit filesystem kind replaced the profile restrictions")
                """),
                    ),
                }
            )
    return transcript


@requires(SANDBOX)
def test_unsupported_profile_names_use_native_diagnostics(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        for name in (":unknown", "workspace", "workspace_write", "workspace-write"):
            configure(host, f"extends: {json.dumps(name)}\n")
            result = subprocess.run(
                [binary, "sandbox", "--", "/bin/echo", "must not run"],
                cwd=host,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert (
                f'unsupported built-in profile {json.dumps(name)}; expected ":workspace" or ":read-only"'
                in result.stderr
            ), result
            transcript.append({"selector": name, "stderr": result.stderr})
    return transcript


@requires(SANDBOX)
def test_empty_protected_directories_cannot_be_removed_or_replaced(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        configure(host, 'extends: ":workspace"\n')
        for name in (".git", ".codex", ".claude"):
            (host / name).mkdir()
        return [
            run(
                binary,
                host,
                code(r"""
            import errno
            import os
            from pathlib import Path
            for name in (".git", ".codex", ".claude"):
                protected = Path(name)
                replacement = Path("replacement")
                replacement.mkdir()
                for operation, args in ((os.rmdir, (protected,)), (os.replace, (replacement, protected)), (os.rename, (protected, Path("moved")))):
                    try:
                        operation(*args)
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.EBUSY, errno.EXDEV), error
                    else:
                        raise AssertionError((operation, name))
                replacement.rmdir()
                assert protected.is_dir()
            print("empty protected directories resist removal, replacement and rename")
            """),
            )
        ]


@requires(SANDBOX)
def test_complete_policy_selector_bypasses_project_and_console_adjustments(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        configure(host, "invalid: [")
        (host / ".claude").mkdir()
        policy = {
            "version": 2,
            "extends": ":workspace",
            "workspace_options": {
                "exclude_tmpdir_env_var": True,
                "exclude_slash_tmp": True,
            },
        }
        return [
            run(
                binary,
                host,
                code(r"""
            from pathlib import Path
            _ = Path("ordinary").write_text("native workspace")
            _ = Path(".claude/native").write_text("no Console adjustment")
            print("complete native selector bypasses project discovery and application adjustments")
            """),
                "--config-env",
                "TEST_POLICY",
                environment={"TEST_POLICY": json.dumps(policy)},
            )
        ]


@requires(SANDBOX)
def test_workspace_options_preserve_explicit_native_values(binary: Path) -> Transcript:
    transcript = []
    for options, inherited_write, shared_write in (
        ({"exclude_tmpdir_env_var": False, "exclude_slash_tmp": True}, True, False),
        ({"exclude_tmpdir_env_var": True, "exclude_slash_tmp": False}, False, True),
        (None, True, True),
    ):
        with (
            TemporaryDirectory() as directory,
            TemporaryDirectory(dir="/var/tmp") as inherited,
            TemporaryDirectory(dir="/tmp") as shared,
        ):
            host = Path(directory).resolve()
            source = json.dumps(
                {"extends": ":workspace", "sandbox": {"workspace_options": options}}
            )
            configure(host, source)
            result = run(
                binary,
                host,
                code(r"""
                import errno
                import json
                import os
                from pathlib import Path
                for key, allowed in json.loads(os.environ["EXPECTED_WRITES"]).items():
                    try:
                        _ = (Path(os.environ[key]) / "explicit").write_text("native option")
                    except OSError as error:
                        assert not allowed and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    else:
                        assert allowed, key
                print("explicit native temporary-directory options preserved")
                """),
                environment={
                    "TMPDIR": inherited,
                    "INHERITED_TEMP": inherited,
                    "SHARED_TEMP": shared,
                    "EXPECTED_WRITES": json.dumps(
                        {"INHERITED_TEMP": inherited_write, "SHARED_TEMP": shared_write}
                    ),
                },
            )
            transcript.append({"configuration": source, **result})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
