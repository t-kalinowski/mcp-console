import os
from pathlib import Path


def test_is_discovered(binary: Path) -> list[dict[str, str]]:
    Path(os.environ["NESTED_SUITE_EXECUTED"]).touch()
    return [{"runner": "nested"}]
