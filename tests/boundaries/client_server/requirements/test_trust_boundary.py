"""Worker-controlled resolver inputs remain within the captured native policy."""

import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, SANDBOX, requires
from boundaries.client_server.python.test_without_r import environment


PROBE = Path(__file__).resolve().parents[3] / "fixtures/resolver/trust_probe.py"
EVIDENCE = {
    "host_read": True,
    "direct_write_denied": True,
    "symlink_write_denied": True,
    "download_denied": True,
}


def workspace(root: Path) -> tuple[Path, dict[str, str]]:
    working = root / "workspace"
    tools = working / "bin"
    tools.mkdir(parents=True)
    uv = shutil.which("uv")
    assert uv is not None
    (tools / "uv").symlink_to(uv)
    protected = root / "protected"
    protected.mkdir()
    (protected / "canary").write_text("preserved")
    env = dict(
        environment(tools),
        XDG_CACHE_HOME=str(root / "cache"),
        MCP_CONSOLE_TEST_PROTECTED=str(protected),
        MCP_CONSOLE_TEST_ALIAS=str(working / "escape"),
        MCP_CONSOLE_TEST_UV=uv,
        UV_HTTP_TIMEOUT="37",
        UV_INDEX_URL="https://pypi.org/simple",
        UV_NO_CONFIG="1",
        UV_PYTHON_PREFERENCE="only-managed",
    )
    return working, env


@requires(POSIX, SANDBOX)
def test_worker_replaces_selected_uv_wrapper(binary: Path) -> Transcript:
    # A home-relative fixture stays outside macOS's writable user temp directory.
    with TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        working, env = workspace(root)
        replacement = working / "replacement-uv"
        replacement.write_text(f"#!{sys.executable}\n" + PROBE.read_text())
        replacement.chmod(0o755)
        with McpClient(
            binary, ("serve", "--writable-root", str(working)), env, working
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker replaced selected uv\n",
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    Path("escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    selected = Path("bin/uv")
                    selected.unlink()
                    selected.symlink_to(Path("replacement-uv").resolve())
                    os.environ["UV_HTTP_TIMEOUT"] = "1"
                    os.environ["UV_CONFIG_FILE"] = "/missing/worker-config"
                    print("worker replaced selected uv")
                    """),
            )
            prepared = client.send(control="restart", requirements={"python": ["six"]})
            assert not prepared.get("isError"), prepared
            client.expect(
                "prepared after replacement\n",
                python="import six; print('prepared after replacement')",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [{"worker_replaced_selected_wrapper": True, **EVIDENCE}]


@requires(POSIX, SANDBOX)
def test_worker_replaces_uv_configuration_and_wheel(binary: Path) -> Transcript:
    with TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        working, env = workspace(root)
        (working / "probe-source.py").write_text(PROBE.read_text())
        config = working / "uv.toml"
        config.write_text("")
        env.pop("UV_NO_CONFIG")
        env.pop("UV_FIND_LINKS", None)
        env["UV_CONFIG_FILE"] = str(config)
        with McpClient(
            binary, ("serve", "--writable-root", str(working)), env, working
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker supplied package startup code\n",
                # fmt: python
                python=code("""
                    import json
                    import os
                    from pathlib import Path
                    import zipfile

                    Path("escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    wheels = Path("wheels")
                    wheels.mkdir()
                    dist = "mcp_console_trust_probe-1.0.0.dist-info"
                    entries = {
                        "mcp_console_trust_probe.py": Path("probe-source.py").read_text(),
                        "mcp_console_trust_probe.pth": "import mcp_console_trust_probe\\n",
                        f"{dist}/METADATA": "Metadata-Version: 2.3\\nName: mcp-console-trust-probe\\nVersion: 1.0.0\\n",
                        f"{dist}/WHEEL": "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
                    }
                    entries[f"{dist}/RECORD"] = "\\n".join(f"{entry},," for entry in entries) + "\\n"
                    with zipfile.ZipFile(
                        wheels / "mcp_console_trust_probe-1.0.0-py3-none-any.whl", "w"
                    ) as wheel:
                        for name, contents in entries.items():
                            wheel.writestr(name, contents)
                    Path("uv.toml").write_text(
                        "find-links = [" + json.dumps(str(wheels.resolve())) + "]\\n"
                    )
                    os.environ["UV_CONFIG_FILE"] = "/missing/worker-config"
                    print("worker supplied package startup code")
                    """),
            )
            prepared = client.send(
                control="restart",
                requirements={"python": ["mcp-console-trust-probe==1.0.0"]},
            )
            assert not prepared.get("isError"), prepared
            client.expect(
                "worker package prepared\n",
                python="import mcp_console_trust_probe; print('worker package prepared')",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [{"worker_replaced_configuration_and_wheel": True, **EVIDENCE}]


@requires(POSIX, SANDBOX)
def test_worker_poisons_explicitly_writable_python_cache(binary: Path) -> Transcript:
    with TemporaryDirectory(prefix="resolver-trust-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        working, env = workspace(root)
        (working / "probe-source.py").write_text(PROBE.read_text())
        caches = root / "cache/mcp-console/dependencies"
        caches.mkdir(parents=True)
        env["MCP_CONSOLE_TEST_ALIAS"] = str(caches / "uv/cache/escape")
        with McpClient(
            binary,
            ("serve", "--writable-root", str(working), "--writable-root", str(caches)),
            env,
            working,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "worker poisoned explicitly writable Python cache\n",
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path
                    import sys
                    import sysconfig

                    cache = Path(os.environ["UV_CACHE_DIR"])
                    assert Path(sys.base_prefix).is_relative_to(cache.parent)
                    (cache / "escape").symlink_to(os.environ["MCP_CONSOLE_TEST_PROTECTED"])
                    site = Path(
                        sysconfig.get_path(
                            "purelib", vars={"base": sys.base_prefix, "platbase": sys.base_prefix}
                        )
                    )
                    (site / "sitecustomize.py").write_text(Path("probe-source.py").read_text())
                    print("worker poisoned explicitly writable Python cache")
                    """),
            )
            prepared = client.send(control="restart", requirements={"python": ["six"]})
            assert not prepared.get("isError"), prepared
            client.expect(
                "poisoned cache remained contained\n",
                python="import six; print('poisoned cache remained contained')",
            )
            client.finish()
        assert (
            json.loads(next((root / "cache").rglob("trust-probe.json")).read_text())
            == EVIDENCE
        )
        assert not (root / "protected/escaped").exists()
    return [
        {
            "explicit_worker_cache_write_grant": True,
            "poisoned_python_startup": True,
            **EVIDENCE,
        }
    ]
