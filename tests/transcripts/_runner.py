import subprocess
from pathlib import Path


root = Path(__file__).resolve().parents[2]


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path)
    subprocess.run([root / "scripts" / "test", suite.stem], check=True)
