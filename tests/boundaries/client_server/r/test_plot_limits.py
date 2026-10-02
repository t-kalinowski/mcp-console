#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment, reference_plots
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_checks_plot_limits_before_allocating(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code(r"""
                local({
                  # Stop at the public PNG entry point, before native allocation.
                  invisible(suppressMessages(trace(
                    "png",
                    where = asNamespace("grDevices"),
                    tracer = quote({
                      stopifnot(
                        identical(units, "in"),
                        identical(width, getOption("console.plot.width", 800 / 96)),
                        identical(height, getOption("console.plot.height", 600 / 96)),
                        identical(res, getOption("console.plot.dpi", 96))
                      )
                      stop("PNG allocation intercepted", call. = FALSE)
                    }),
                    print = FALSE
                  )))
                  on.exit(suppressMessages(untrace(
                    "png",
                    where = asNamespace("grDevices")
                  )))
                  check_plot <- function(label, size, accepted) {
                    options(
                      console.plot.width = size[[1]],
                      console.plot.height = size[[2]],
                      console.plot.dpi = size[[3]]
                    )
                    message <- tryCatch(plot(1:3), error = conditionMessage)
                    cat(label, ": ", message, "\n", sep = "")
                    if (accepted) {
                      stopifnot(identical(message, "PNG allocation intercepted"))
                    } else {
                      stopifnot(startsWith(message, "managed R plot exceeds limit:"))
                    }
                    stopifnot(grDevices::dev.cur() == 1L)
                  }
                  rejected <- list(
                    mistaken_pixels = c(1600, 1050, 100),
                    wide = c(16385, 1, 1),
                    tall = c(1, 16385, 1),
                    area = c(4097, 4096, 1),
                    fractional_width = c(16384.25, 1, 1),
                    fractional_area = c(4096.25, 4096, 1),
                    dpi = c(1, 1, 16385),
                    dimension_overflow = c(1e308, 1, 100),
                    height_overflow = c(1, 1e308, 100),
                    area_overflow = c(1e200, 1e200, 1)
                  )
                  for (label in names(rejected)) {
                    check_plot(label, rejected[[label]], FALSE)
                  }
                  accepted <- list(
                    defaults = list(NULL, NULL, NULL),
                    corrected_inches = c(16, 10.5, 100),
                    wide_boundary = c(16384, 1024, 1),
                    tall_boundary = c(1024, 16384, 1),
                    square_boundary = c(4096, 4096, 1),
                    scaled_boundary = c(16, 16, 256)
                  )
                  for (label in names(accepted)) {
                    check_plot(label, accepted[[label]], TRUE)
                  }
                  check_plot("rejection_before_recovery", rejected$mistaken_pixels, FALSE)
                  cat("plot limits checked before allocation\n")
                })
                """),
        )
        assert last_tool_text(client).endswith(
            "plot limits checked before allocation\n"
        ), last_tool_text(client)

        # Rejection must leave the managed device usable in the next cell.
        client.send(
            # fmt: r
            r=code(r"""
                options(
                  console.plot.width = 16,
                  console.plot.height = 10.5,
                  console.plot.dpi = 100
                )
                plot(3:1)
                """),
        )
        expected = reference_plots(
            rscript, environment, "plot(3:1)", width=16, height=10.5, dpi=100, pages=1
        )
        assert_result_content(client, expected)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_invalid_plot_options(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code(r"""
                local({
                  invalid <- list(0, -1, Inf, -Inf, NaN, NA_real_, numeric(), c(1, 2), "4")
                  for (option in c(
                    "console.plot.width",
                    "console.plot.height",
                    "console.plot.dpi"
                  )) {
                    for (value in invalid) {
                      options(
                        console.plot.width = 4,
                        console.plot.height = 3,
                        console.plot.dpi = 100
                      )
                      options(setNames(list(value), option))
                      message <- tryCatch(plot(1:3), error = conditionMessage)
                      stopifnot(
                        identical(message, paste(option, "must be one positive finite number")),
                        grDevices::dev.cur() == 1L
                      )
                    }
                    cat(option, ": invalid values rejected\n", sep = "")
                  }
                })
                """),
        )
        assert last_tool_text(client) == (
            "console.plot.width: invalid values rejected\n"
            "console.plot.height: invalid values rejected\n"
            "console.plot.dpi: invalid values rejected\n"
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
