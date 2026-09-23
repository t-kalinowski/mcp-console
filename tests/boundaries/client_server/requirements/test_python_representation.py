#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_round_trips_python_requirement_attributes_and_copies(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        initial <- reticulate::py_require()
        latin1 <- rawToChar(as.raw(0xe9))
        Encoding(latin1) <- "latin1"
        bytes <- rawToChar(as.raw(0xff))
        Encoding(bytes) <- "bytes"
        packages <- structure(
          c("zeta", "alpha", NA_character_, "alpha", "", latin1, bytes),
          names = c("last", "first", "missing", "again", "empty", latin1, bytes),
          class = "AsIs",
          comment = "package request",
          detail = list(values = c(1L, NA_integer_), nested = list("original"))
        )
        cutoff <- structure(
          "2026-01-01",
          names = "cutoff",
          class = "AsIs",
          detail = list("original")
        )
        reticulate::py_require(packages, exclude_newer = cutoff, action = "set")
        expected <- reticulate::py_require()
        stopifnot(
          identical(expected$packages, packages),
          identical(expected$exclude_newer, cutoff),
          identical(Encoding(expected$packages), Encoding(packages)),
          identical(tail(expected$history, 1L)[[1L]]$packages, packages),
          identical(tail(expected$history, 1L)[[1L]]$exclude_newer, cutoff),
          identical(head(expected$history, -1L), initial$history)
        )
        packages[1L] <- "changed input"
        names(packages)[2L] <- "changed name"
        attr(packages, "detail")$nested[[1L]] <- "changed input attribute"
        cutoff[1L] <- "2026-02-01"
        attr(cutoff, "detail")[[1L]] <- "changed cutoff attribute"
        invisible(gc())
        stopifnot(identical(reticulate::py_require(), expected))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        detached <- reticulate::py_require()
        detached$packages[1L] <- "changed result"
        attr(detached$packages, "detail")$nested[[1L]] <- "changed result attribute"
        attr(detached$exclude_newer, "detail")[[1L]] <- "changed cutoff result"
        attr(detached$history[[length(detached$history)]]$packages, "detail") <- NULL
        detached$history[[length(detached$history)]]$packages[1L] <- "changed history"
        invisible(gc())
        stopifnot(identical(reticulate::py_require(), expected))
        empty <- structure(character(), class = "AsIs", detail = list(1L))
        reticulate::py_require(empty, exclude_newer = "", action = "set")
        cleared <- reticulate::py_require()
        stopifnot(
          identical(cleared$packages, empty),
          identical(cleared["exclude_newer"], list(exclude_newer = NULL)),
          identical(names(cleared), names(expected)),
          identical(head(cleared$history, -1L), expected$history),
          identical(tail(cleared$history, 1L)[[1L]]$packages, empty),
          !reticulate::py_available(initialize = FALSE)
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
