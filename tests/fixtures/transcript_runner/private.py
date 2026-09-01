import os
from pathlib import Path


Path(os.environ["PRIVATE_SUITE_LOADED"]).touch()


def test_is_not_discovered(binary: Path) -> list[dict[str, str]]:
    Path(os.environ["PRIVATE_SUITE_EXECUTED"]).touch()
    return [{"runner": "private"}]
