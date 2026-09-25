#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite


def test_inspects_one_selected_executable(binary: Path) -> Transcript:
    selected = Path(sys.executable)
    result = subprocess.run(
        [binary, "inspect-python", selected],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stderr == "", result
    assert len(result.stdout.splitlines()) == 1, result.stdout
    configuration = json.loads(result.stdout)
    assert set(configuration) == {"python", "libpython", "python_home"}
    assert configuration["python"] == str(selected)
    assert Path(configuration["libpython"]).is_file()
    assert all(Path(root).is_dir() for root in configuration["python_home"].split(":"))
    return [
        {
            "command": "inspect-python",
            "fields": sorted(configuration),
            "selected_executable_preserved": True,
            "embedding_library_available": True,
        }
    ]


def test_rejects_missing_executable_without_output(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        missing = Path(temporary) / "missing-python"
        result = subprocess.run(
            [binary, "inspect-python", missing],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1 and result.stdout == "", result
        expected = f"selected Python executable is not an absolute file: {missing}\n"
        assert result.stderr == expected, result.stderr
    return [{"command": "inspect-python", "error": "selected executable is missing"}]


def test_interrupt_reaps_selected_executable(binary: Path) -> Transcript:
    fixture = Path(__file__).resolve().parents[2] / "fixtures"
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        venv = root / "venv"
        created = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert created.returncode == 0, created.stderr
        selected = venv / "bin" / "python"
        site = subprocess.run(
            [selected, fixture / "native_python_paths.py", "site-packages"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert site.returncode == 0, site.stderr
        site_packages = Path(site.stdout.strip())
        shutil.copyfile(
            fixture / "native_python_sitecustomize.py",
            site_packages / "sitecustomize.py",
        )
        (site_packages / "inspection-mode").write_text("wait", encoding="utf-8")
        address = root / "ready.sock"
        (site_packages / "inspection-socket").write_text(str(address), encoding="utf-8")

        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(address))
            listener.listen(1)
            listener.settimeout(10)
            process = subprocess.Popen(
                [binary, "inspect-python", selected],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                with listener.accept()[0] as connection:
                    selected_pid = int(connection.recv(32))
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 1 and stdout == "", (stdout, stderr)
                assert "cancelled" in stderr, stderr
                try:
                    os.kill(selected_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError("inspected executable survived interruption")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
    return [{"command": "inspect-python", "interrupted_child_reaped": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
