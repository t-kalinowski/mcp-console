#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _sandbox_supervision_interrupt import test_delivers_terminal_interrupt_once
from _sandbox_supervision_job_control import (
    test_foregrounds_background_terminal_reader,
    test_stops_and_continues_foreground_sandbox_job,
)
from _sandbox_supervision_pipeline import test_preserves_foreground_pipeline_job_control
from _sandbox_supervision_process import (
    test_closes_unlisted_inherited_descriptors,
    test_relays_interrupt_then_retires_descendants,
    test_retires_processx_descendants_across_sessions,
    test_retires_same_group_child_forked_before_root_exit,
)
from _support import run_this_suite

PLATFORMS = {"darwin"}

if __name__ == "__main__":
    run_this_suite(__file__)
