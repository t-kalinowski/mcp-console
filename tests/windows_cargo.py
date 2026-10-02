"""Public Cargo build acceptance for Windows companion updates."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "native Windows packaging")
class WindowsCargo(unittest.TestCase):
    def test_rebuild_preserves_running_companion(self):
        with tempfile.TemporaryDirectory(prefix="console cargo ") as temporary:
            root = Path(temporary)
            (root / "src/windows").mkdir(parents=True)
            (root / "src/main.rs").write_text("fn main() {}\n")
            for source in (ROOT / "src").rglob("*.c"):
                destination = root / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch()
            for source in (
                "build.rs",
                "src/windows/console.rc",
                "src/windows/console.manifest",
            ):
                shutil.copyfile(ROOT / source, root / source)
            dependencies = tomllib.loads((ROOT / "Cargo.toml").read_text())[
                "build-dependencies"
            ]
            (root / "Cargo.toml").write_text(
                '[package]\nname = "windows-companion-build"\nversion = "0.0.0"\n'
                'edition = "2024"\n[build-dependencies]\n'
                + "".join(
                    f"{name} = {json.dumps(version)}\n"
                    for name, version in dependencies.items()
                )
            )
            shutil.copyfile(ROOT / "Cargo.lock", root / "Cargo.lock")
            helper_source = root / "helper.rs"
            helper_source.write_text("""fn main() {
    println!("ready");
    std::io::stdin().read_line(&mut String::new()).unwrap();
}
""")
            helper = root / "helper.exe"
            subprocess.run(
                ["rustc", "--edition=2024", str(helper_source), "-o", str(helper)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            target = subprocess.check_output(
                ["rustc", "--print", "host-tuple"], text=True
            ).strip()
            pin = json.loads((ROOT / "sandbox-runner.json").read_text())
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            (root / "target").mkdir()
            environment = dict(os.environ, MCP_CONSOLE_HOME=str(root / "home"))
            arguments = [
                "cargo",
                "build",
                "--target",
                target,
                "--target-dir",
                str(root / "target"),
            ]
            artifacts = {
                "mcp-console-sandbox.exe": helper.read_bytes(),
                "mcp-console-sandbox-setup.exe": helper.read_bytes(),
                "mcp-console-sandbox-runner.exe": helper.read_bytes(),
                "LICENSE": b"license",
                "NOTICE": b"notice",
            }

            def stage():
                for name, contents in artifacts.items():
                    directory = (
                        "libexec"
                        if name.endswith(".exe")
                        else "share/licenses/mcp-console"
                    )
                    path = root / "wheel-data/data" / directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(contents)
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

            def build(*extra):
                result = subprocess.run(
                    [*arguments, *extra],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            stage()
            build()
            name = "mcp-console-sandbox-runner.exe"
            installed = list((root / "target" / target).rglob(name))
            self.assertEqual(len(installed), 1, installed)
            previous = installed[0]
            original = previous.read_bytes()
            with subprocess.Popen(
                [str(previous)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as running:
                try:
                    self.assertEqual(running.stdout.readline().strip(), "ready")
                    artifacts[name] += b"updated companion"
                    stage()
                    build()
                    self.assertIsNone(running.poll(), "build retired the active helper")
                    self.assertEqual(previous.read_bytes(), original)
                    installed = list((root / "target" / target).rglob(name))
                    self.assertEqual(len(installed), 2, installed)
                    replacement = next(path for path in installed if path != previous)
                    self.assertEqual(replacement.read_bytes(), artifacts[name])
                    # Observe newly installed files, then require an unchanged
                    # build to reuse the executable while the old helper lives.
                    build()
                    result = build("--message-format=json")
                    executables = [
                        event
                        for line in result.stdout.splitlines()
                        if (event := json.loads(line)).get("executable")
                    ]
                    self.assertEqual(len(executables), 1, result.stdout)
                    self.assertTrue(executables[0]["fresh"], result.stderr)
                finally:
                    running.communicate("exit\n", timeout=10)
                self.assertEqual(running.returncode, 0)


if __name__ == "__main__":
    unittest.main()
