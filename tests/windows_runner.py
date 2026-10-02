"""Discover or run native Windows acceptance through the checkout workflow."""

import argparse
import inspect
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser(
    prog="scripts/test",
    description="Run native Windows acceptance (preinstalled R/Python; no sandbox or remote controllers).",
)
actions = parser.add_mutually_exclusive_group()
actions.add_argument("--list", action="store_true")
actions.add_argument("--locate", metavar="CLASS[.CASE]")
profiles = parser.add_mutually_exclusive_group()
profiles.add_argument(
    "--full", action="store_true", help="all native cases (also the Windows default)"
)
profiles.add_argument(
    "--quick", action="store_true", help="alias for the Windows default"
)
parser.add_argument("selectors", nargs="*", metavar="CLASS[.CASE]")
options = parser.parse_args()
if options.locate and options.selectors:
    parser.error("--locate does not accept additional selectors")

import windows

loader = unittest.TestLoader()
selectors = [options.locate] if options.locate else options.selectors
suite = (
    loader.loadTestsFromNames(selectors, windows)
    if selectors
    else loader.loadTestsFromModule(windows)
)
if loader.errors:
    parser.error("\n".join(loader.errors))


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


if options.list or options.locate:
    for case in cases(suite):
        method = getattr(type(case), case._testMethodName)
        label = f"{type(case).__name__}.{case._testMethodName}"
        print(
            f"{label}: {inspect.getsourcefile(method)}:{inspect.getsourcelines(method)[1]}"
            if options.locate
            else label
        )
else:
    from checkout_workflow import main

    sys.argv.insert(1, "test")
    main()
