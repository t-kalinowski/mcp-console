import subprocess
import sys
from pathlib import Path


directory = Path(__file__).resolve().parent.parent
root = directory.parents[1]
sys.path.insert(0, str(directory))


def run_this_case(case_path: str) -> None:
    case = Path(case_path)
    subprocess.run([root / "scripts" / "test", case.stem], check=True)
