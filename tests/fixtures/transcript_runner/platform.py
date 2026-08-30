from pathlib import Path


CASE_PLATFORMS = {
    "platform_inapplicable": {"never"},
}


def test_platform_applicable(binary: Path) -> list[dict[str, str]]:
    return [{"runner": "applicable"}]


def test_platform_inapplicable(binary: Path) -> list[dict[str, str]]:
    raise AssertionError("platform-inapplicable case ran")
