# Managed R graphics interruption

An interrupt can remain pending while a native graphics callback runs.
Console also defers R interrupts during automatic cell-end device closure.
If an interrupted cell still reports `[running; poll with an empty send]`, collect it with an empty `send`.
Use `send(control="restart")` when recovery requires retiring the worker and losing its language state.
See [Calls, input, and control](SEND_OPERATIONS.md) for the full control contract.

The bounded investigation below found delayed interruption during Quartz drawing and successful restart recovery.
It did not reproduce the original large-PNG finalization stall or establish a Console defect.
The recommendation is to retain cooperative interruption and restart, with the recovery tests described below.
Prompt cancellation of arbitrary native libraries is not promised.

## Native Mac measurements, 2026-10-10 UTC

Measurements used checkout `ccbb6ac9da2f432b15dd5ad0d363b0e79d8cb929`, its release executable and pinned sandbox companion, on an arm64 Mac running macOS 26.7 (25G229).
Other workloads were running on this host; these observations are not latency guarantees.
The worker reported R 4.6.1 (2026-06-24), `aarch64-apple-darwin23`, Quartz as its default bitmap backend, and Cairo availability.
Cairo was selected explicitly for its comparison trial.
Installed graphics-library metadata reported Cairo 1.17.6 and libpng 1.6.56.
Linux and Windows native graphics timing was not measured.

All controls went through public MCP `send` calls after interpreter warmup.
The client recorded calls/results, worker PID plus start time, phase markers, monotonic timings, sampled stacks, RSS, and subsequent plotting/state checks.
"Control response" measures the entire control call; it is not a measurement of signal-handler entry.
"Settlement" measures collection of the final evaluation result from control-call entry, including any subsequent empty poll.
Natural-completion rows instead measure admission through final collection.

| Workload / mode                       | Action             | Control response (ms) | Settlement (ms) |
| ------------------------------------- | ------------------ | --------------------: | --------------: |
| R `Sys.sleep`, direct                 | interrupt          |                 110.7 |           110.7 |
| R `Sys.sleep`, direct                 | restart            |                1196.8 |          1196.8 |
| Managed plot then `Sys.sleep`, direct | interrupt          |                 104.0 |           104.0 |
| Managed plot then `Sys.sleep`, direct | restart            |                1779.8 |          1779.8 |
| Quartz polyline, direct               | natural completion |                     — |           645.0 |
| Quartz polyline, direct, trial 1      | interrupt          |                 113.8 |           716.7 |
| Quartz polyline, direct, trial 2      | interrupt          |                 106.2 |           633.1 |
| Quartz polyline, direct, trial 3      | interrupt          |                 111.0 |           630.7 |
| Quartz polyline, direct, trial 1      | restart            |                 777.0 |           777.0 |
| Quartz polyline, direct, trial 2      | restart            |                 761.5 |           761.6 |
| Quartz polyline, direct, trial 3      | restart            |                 755.0 |           755.0 |
| Quartz polyline, sandbox              | interrupt          |                 155.9 |          1382.2 |
| Quartz polyline, sandbox              | restart            |                 996.5 |           996.5 |
| Quartz raster, direct                 | natural completion |                     — |           116.0 |
| Quartz raster automatic close, direct | interrupt          |                 109.5 |           109.5 |
| Quartz raster automatic close, direct | restart            |                 207.6 |           207.6 |
| Quartz raster explicit close, direct  | interrupt          |                 115.5 |           115.5 |
| Cairo raster automatic close, direct  | interrupt          |                 111.1 |           111.1 |

Polyline trials used one 2048 × 2048 managed surface and one deterministic 65,536-vertex path.
Raster trials used a 1024 × 1024 deterministic color input on the same surface size.
The natural Quartz polyline spent 599.3 ms drawing and 35.7 ms inside traced `dev.off()`, with 587 ms of worker user-plus-system CPU between drawing entry and closure exit.
The natural raster spent 38.6 ms drawing and 59.5 ms in `dev.off()`, with 88 ms of worker CPU over those phases.
The Cairo automatic-close interrupt trial spent 79.9 ms in `dev.off()`.
These closure intervals include the R wrapper, native callback, and Console's synchronous image publication; they do not isolate encoding time.

Interrupt requests followed the entry marker by roughly 1–3 ms.
The marker precedes the native call, so timing alone does not prove that the call was active at delivery.
For the delayed polyline trials, sampled stacks include `RQuartz_Polyline`, `CGContextDrawPath`, and CoreGraphics rasterization.
The post-`lines()` marker was absent after interruption: the pending interrupt took effect before the next expression.
Automatic closure still completed and produced one image.
The explicit `grDevices::dev.off()` trial produced an image but did not reach its exit tracer after interruption.
Bounded native closure finished within the control call's observation interval; no long output stall was demonstrated.

After each successful interrupt, the original worker identity and R marker object survived, `dev.list()` was empty, managed PNG files had been removed, and another plot succeeded.
Every successful restart changed the worker identity, confirmed old-worker exit and removal of its R temporary directory, lost the marker object, and allowed another plot.
Some short native operations completed and delivered an image during orderly restart; restart is not a guarantee that already produced output disappears.
Recordings retained their separate artifact copies until the isolated experiment workspace was removed.
All observed experiment-owned processes had exited before that removal.

## Resource bounds and reproduction

The [measurement harness](benchmarks/graphics_interrupt.py) runs one trial per invocation on macOS, using the repository's recording MCP client.
It preserves `HOME` and tool caches, excludes personal R startup files, and isolates its workspace, `MCP_CONSOLE_HOME`, and `TMPDIR`.
Run invocations sequentially; warmup and input preparation are outside the measured interval.

- A surface is at most 2048² pixels: 16 MiB nominal RGBA, with additional backend copies and encoding allocations.
- Two polyline coordinate vectors are at most 65,536 doubles each, totaling 1 MiB; a raster input is at most 1024² pixels.
- The launcher sets inherited per-process CPU limits of 30/35 seconds and a 64 MiB per-file size limit.
  The worker verifies these through a public Python cell.
- Before input preparation, a host watchdog begins tracking process identities and native RSS.
  It aborts at 1 GiB per process, 1.5 GiB for the owned tree, or 45 seconds for the warmed workload and recovery.
  Observation failure also aborts the trial.
- The watcher targets 20 ms samples.
  Successful reported trials observed mean intervals of 24–28 ms and a largest gap of 39 ms.
  This is a sampled threshold, not a hard instantaneous memory cap.

The largest observed process/tree RSS in reported trials was 300.0/340.1 MiB.
No reported trial hit a CPU, file-size, RSS, or wall limit.
Initial harness attempts aborted on exiting-process observations before a workload, and one retirement trial was censored for the same harness error.
An early marker-formatting trial and two unqualified `dev.off()` trials with missing trace markers were excluded from phase attribution.
The retained harness accounts for exiting tasks and traces the qualified explicit close.
Emergency watchdog kills are excluded from successful restart evidence.

From the repository root:

```sh
scripts/stage-sandbox-runner
scripts/test client_server/r/test_plots::returns_cell_scoped_plots \
  client_server/r/test_lifecycle::interrupts_running_r_evaluation
audit_output=$(mktemp -d)
python3 docs/benchmarks/graphics_interrupt.py "$audit_output" natural lines complete \
  --pixels 2048 --vertices 65536
python3 docs/benchmarks/graphics_interrupt.py "$audit_output" interrupt lines interrupt \
  --pixels 2048 --vertices 65536
python3 docs/benchmarks/graphics_interrupt.py "$audit_output" restart lines restart \
  --pixels 2048 --vertices 65536
python3 docs/benchmarks/graphics_interrupt.py "$audit_output" close raster interrupt \
  --phase close --pixels 2048
python3 docs/benchmarks/graphics_interrupt.py "$audit_output" explicit raster-explicit interrupt \
  --phase close --pixels 2048
python3 docs/benchmarks/graphics_interrupt.py "$audit_output" cairo raster interrupt \
  --phase close --pixels 2048 --backend cairo
```

Use `sleep` or `plot-sleep` instead of `lines` for the interruptible wait comparisons, and `--sandbox` for the native sandbox launch.
Use fresh trial labels for repetitions, with at most three repetitions per comparison.
Each invocation retains `<label>.json`, `<label>.calls.json`, and `<label>.sample.txt` in the chosen output directory.
The report includes per-trial limits, phase CPU/wall markers, timing, RSS, sampling gaps, recovery state, PNG cleanup, and process exit.
Its image count and settled text include admission, control, and polling responses, each retained once.
It intentionally rejects another host instead of substituting a different observation method.

## Where interruption waits

The [relay supervisor](../src/worker_relay/supervisor.rs) signals the direct worker with `SIGINT`.
A successful syscall establishes delivery acceptance, not interpreter completion.
The [worker signal handler](../src/worker/interrupt.c) records pending state and wakes waits; it does not enter R.
The [C R REPL](../src/r_repl.c) checks pending interruption at interpreter operation boundaries.

R 4.6.1's [Quartz drawing](https://github.com/wch/r-source/blob/0b08a502b1303fc0d0a38f71c9deed71baf266c2/src/library/grDevices/src/devQuartz.c) and [bitmap output](https://github.com/wch/r-source/blob/0b08a502b1303fc0d0a38f71c9deed71baf266c2/src/library/grDevices/src/qdBitmap.c) wrappers enter Apple drawing/image-output APIs without an R interrupt checkpoint in those regions.
The version-matched Cairo bitmap output and `R_SaveAsPng` paths likewise contain no such checkpoint in their inspected output regions.
The correlated drawing samples and absent post-draw marker support a native-region delay followed by an R checkpoint, rather than a lost interrupt.
They do not identify signal-handler entry time or every phase inside the proprietary framework.

During automatic cell-end closure, [embedded R](../src/worker/embedded_r.rs) suspends R interruption around `graphics.finish()`, restores the previous state, and checks pending interruption afterward.
That deliberate Console deferral includes device closure and synchronous PNG reading, removal, and encoding.
The [C graphics trampoline](../src/r_graphics.c) invokes callbacks that can raise R errors and longjmp, then notifies Rust only after normal return.
Adding a checkpoint in the Rust callback or calling interpreter APIs from a signal handler would not preserve those unwind and interpreter-thread constraints.
The investigation makes no changes to these production paths.

## Recovery coverage

```sh
scripts/test client_server/r/test_graphics_interrupts
python3 tests/graphics_interrupt_benchmark.py
```

The public regression module uses a small plot and an R tracer with a native FIFO gate during automatic closure.
The gate has no R checkpoint and stays blocked until explicitly released; it is synchronization coverage, not a native PNG timing measurement.
One case verifies pending interruption, completed image delivery, preserved objects, device/file cleanup, and another plot after release.
The other leaves the gate blocked and verifies public restart, confirmed old-worker retirement, temporary-directory cleanup, state loss, and plotting in the replacement.
Both cases run in direct and sandbox modes on supported POSIX hosts with native fixture capability.

The Mac benchmark check runs the documented entry point against real MCP responses, holding admission open until a small plot and text marker complete.
It verifies that completion, interrupt, and restart reports retain that admission output exactly once; it does not measure native interruption latency.
