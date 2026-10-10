# Managed R graphics callbacks

Retain the managed PNG device's C page and close callbacks.
R's base and grid page hooks do not observe the same boundaries: multiple base plots can share a page, initial grid drawing can start a page without a hook, and replay can finalize pages without running either hook.
A hook-and-replay replacement would need additional interception and rendering state to preserve the current behavior.
This assessment found no smaller replacement that covers those paths.

The user contract remains [cell-finalized PNG pages, language-error cleanup, and explicit user-device ownership](BUILTIN_RUNTIME.md#plots-and-images).
The callbacks publish completed pages when the backend finalizes them; they do not capture incremental drawing or reconstruct a global timeline across independent output streams.

## Where the callbacks run

The R bridge opens an ordinary `grDevices::png()` only when R needs its default device during an active evaluation.
[`managed_device()`](../src/r_graphics/bridge.R) records that device's output path and registers its native device pointer with [`r_graphics.rs`](../src/r_graphics.rs).
Registration replaces only that device's `newPage` and `close` callbacks and saves the originals.
Explicit user devices retain their own callbacks.

These R entry points reach the installed callbacks:

| Entry point                                            | R engine path                                                                                      | Device event                                                                                                                          |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Base `plot.new()`, normally called by `plot.default()` | `C_plot_new` → `GNewPlot` → `GENewPage`                                                            | First page, or the next physical page when the figure layout requires it. A panel or `par(new = TRUE)` overlay need not start a page. |
| `grid::grid.newpage()`                                 | `L_newpage` / `dirtyGridDevice` → `GENewPage`                                                      | First page on an unused device, or the next page on a dirty device.                                                                   |
| First grid drawing, such as `grid::grid.rect()`        | `dirtyGridDevice` → `GENewPage`                                                                    | Starts the first page if another graphics system has not already done so; no `grid.newpage()` R hook is required.                     |
| `replayPlot()` and display-list copying                | `GEplaySnapshot` / `GEcopyDisplayList` → `GEplayDisplayList` → recorded native graphics operations | Replayed operations can start another device page without calling the R `plot.new()` or `grid.newpage()` wrappers.                    |
| `dev.off()`                                            | `C_devoff` → `killDevice` → `removeDevice`                                                         | Calls the device's `close` before destroying its descriptor.                                                                          |
| `graphics.off()`                                       | Repeated `dev.off()` calls                                                                         | Uses the same close path for each open device.                                                                                        |
| Console cell finalization                              | Bridge `finish()` → `dev.off(which = ...)`                                                         | Closes the still-open managed devices belonging to that evaluation.                                                                   |

The upstream implementations are [base page selection][r-base], [grid initialization and pages][r-grid], [engine page/replay dispatch][r-engine], and [device removal][r-devices].
Switching the current device with `dev.set()` alone neither starts a page nor closes one.

### PNG completion and publication

The saved backend callback runs first, in [`r_graphics.c`](../src/r_graphics.c).
For [Cairo PNG][r-cairo], `BM_NewPage` writes the previous bitmap and closes its file before opening the next page's file; `BM_Close` writes the final bitmap and closes its file.
The `cairo-png` variant writes by filename through Cairo.
For [Quartz PNG][r-quartz], `QuartzBitmap_NewPage` writes the previous bitmap before advancing its page counter, and `QuartzBitmap_Close` writes the final bitmap.
The outer `RQuartz_NewPage` / `RQuartz_Close` callbacks delegate to those bitmap operations.

Only after the original callback returns normally does C notify Rust:

- The first successful `newPage` advances the managed counter from zero to one and publishes nothing.
- Each subsequent successful `newPage` publishes the previous page's PNG and advances the counter.
- Successful `close` removes the managed pointer and publishes its last page, if any.

Rust reads and removes the completed file, then sends the image through the worker's existing output transport.
Later `lines()`, grid drawing, and other same-page operations remain part of the current page until a real page transition or close.
Text produced after a successful transition follows the image of the page just finalized in the tested R output path.
Text produced while that page is still open can precede its image.

### Error unwinding and ownership

An R graphics callback can raise an error and longjmp.
The trampoline obtains the saved function pointer from Rust, returns from that lookup, and invokes the backend from C.
No Rust lookup frame or state lock remains live across that backend call.
If the backend unwinds, the post-callback Rust notification does not run.
Calling the potentially unwinding backend directly from a Rust callback would lose this boundary.

After an ordinary language error, the protected evaluation returns to the [coordinator](../src/worker/coordinator.rs), which still finishes graphics.
That cleanup closes and publishes the unfinished managed page.
The coordinator brackets Python cells as well as R cells because Python can enter R through reticulate; SQL retains its exclusion.
Late R initialization attaches the bridge within the active evaluation.

The R bridge closes only devices whose current registry path still matches the path it recorded when opening them.
R can reuse a device number after `dev.off()`; a new user device in that slot must survive cell cleanup.
Rust tracks the managed native pointer for page/close events, while the R path check protects cleanup ownership.
When user code closes all devices, R's persistent default-device option still opens a fresh managed device on the next plot.

## Comparison with R-only mechanisms

The minimal replay proposal is to enable display-list recording on the managed file device, use `before.plot.new` and `before.grid.newpage` to record the preceding page, replay it onto a temporary PNG device, and flush the final recording at cell end.
It would also need to observe user-initiated closure before the display list disappears, preserve user devices, restore the current device after rendering, and prevent capture of its own replay.

The before hooks run too early to read the original PNG directly.
In an explicit Quartz probe with `plot(1:3); lines(3:1, col = "red"); plot(3:1)`, neither before hook saw a completed file.
The after hook on the second `plot.new()` saw the first completed PNG; closing the device produced the second.
Cairo can create an empty file earlier, so file existence alone is also insufficient.

Bounded probes used 400 × 300 PNGs, restored the previous hooks, closed their own devices, and checked completed PNG files after closure.
These counts were identical on installed R 4.6.1 with `quartz`, `cairo`, and `cairo-png`:

| Program on one explicit PNG device                                     | `before.plot.new` calls | `before.grid.newpage` calls | Completed pages |
| ---------------------------------------------------------------------- | ----------------------: | --------------------------: | --------------: |
| Two base plots, with `lines()` before the second plot                  |                       2 |                           0 |               2 |
| Three base plots in a two-panel layout                                 |                       3 |                           0 |               2 |
| Two base plots with `par(new = TRUE)` before the second                |                       2 |                           0 |               1 |
| `grid.rect(); grid.newpage(); grid.circle()`                           |                       0 |                           1 |               2 |
| Base plot, grid overlay, then a new grid page                          |                       1 |                           1 |               2 |
| Base plot, `recordPlot()`, then two `replayPlot()` calls               |                       1 |                           0 |               3 |
| Grid page and rectangle, `recordPlot()`, then two `replayPlot()` calls |                       0 |                           1 |               3 |

Replay probes explicitly enabled the display list before drawing.
Their two replay transitions bypassed all four before/after R page hooks.
For example, run this on a PNG device with those hooks installed:

```r
dev.control(displaylist = "enable")
plot(1:3)
saved <- recordPlot()
replayPlot(saved)
cat("after first replay\n")
replayPlot(saved)
cat("after second replay\n")
```

The managed-device probe returned image, first text, image, second text, final image, with each PNG matching a native R reference.
A design observing only the page hooks and cell end cannot publish at the two replay boundaries.
Deduplicating identical recordings would also suppress repeated physical pages in this example.

Base's `par("page")` can help distinguish panels from physical pages, but it cannot supply the missing replay events.
After hooks plus a completed-file scan have the same missing-event problem.
Cell-end scanning alone would delay those pages past intervening text.
R wrappers for `dev.off()` can cover ordinary R closure, including `graphics.off()`, but are not a device-close callback: native device removal also exists, and wrappers must preserve namespace references and user ownership.
Both base and grid run page hooks inside `try()`, so errors in a hook are reported without necessarily aborting the plotting operation.
Those additional responsibilities do not establish a simpler replacement for the existing managed-pointer callbacks.

## Which device tests carry a contract

The existing [R plot tests](../tests/boundaries/client_server/r/test_plots.py) distinguish these contracts.
The focused baseline executed the first five rows; the worker-isolation row was inspected in source.

| Coverage                                                                           | Distinct invariant                                                                                                     |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `returns_cell_scoped_plots`                                                        | Actual reference images retain same-expression `lines()`; a later cell cannot modify a finalized page.                 |
| `emits_managed_plots_when_pages_finalize`                                          | A real page transition publishes the preceding image before subsequent R console text.                                 |
| `returns_plots_after_r_errors`                                                     | A language error still delivers the unfinished plot, and the next plotting cell works.                                 |
| `leaves_explicit_plot_devices_user_controlled`                                     | User devices remain open and uncaptured across cells; managed plotting resumes after `dev.off()` and `graphics.off()`. |
| [Python-to-R plotting](../tests/boundaries/client_server/python/test_runtime.py)   | Python-entered R uses the same managed finalization lifecycle.                                                         |
| [Worker plot isolation](../tests/boundaries/client_server/r/test_plot_sessions.py) | Concurrent workers sharing an inherited temporary root do not share plot files.                                        |

Retain ownership, closure/resumption, and isolation coverage even when the examples require manual device management.
Device-number reuse is another distinct ownership case; its useful assertion is that a user device replacing a closed managed device survives cleanup.
An exact order among pages from deliberately switched devices is incidental unless a documented contract requires it.
There is no need to restore the removed permutation-heavy `dev.set()` transcript or a cell-wide text barrier to prove these invariants.
This assessment leaves test consolidation to the existing coverage work.

## Evidence limits

The assessment used Console checkout `19e9b8ce2c59569b7862c7e11f17d4328e831657` on macOS arm64 and installed R 4.6.1 (2026-06-24).
The five focused baseline cases passed with their declared runtime modes, including direct and sandbox execution for the grouped plots, user devices, and Python bridge.
Additional temporary public MCP probes compared native reference PNGs and page/text order for panels, implicit grid drawing, mixed graphics, base/grid replay, and managed closure/resumption in both modes.
The hook probes also ran through the installed MCP Console, separately from the checkout acceptance tests.

The linked upstream source was read locally at `0fa000308ea74ee25671d3ab2475d63ea9ef19a0` (R 4.7.0 under development), which is newer than the installed runtime.
Executed probes establish the reported installed behavior; source inspection explains the dispatch and backend paths.
Linux, Windows, other device backends, backend callback failures, and interrupt/restart behavior were not exercised by this assessment.
The conclusion requires no production bridge or graphics changes and no new ordering contract.

[r-base]: https://github.com/wch/r-source/blob/0fa000308ea74ee25671d3ab2475d63ea9ef19a0/src/library/graphics/src/graphics.c
[r-grid]: https://github.com/wch/r-source/blob/0fa000308ea74ee25671d3ab2475d63ea9ef19a0/src/library/grid/src/grid.c
[r-engine]: https://github.com/wch/r-source/blob/0fa000308ea74ee25671d3ab2475d63ea9ef19a0/src/main/engine.c
[r-devices]: https://github.com/wch/r-source/blob/0fa000308ea74ee25671d3ab2475d63ea9ef19a0/src/main/devices.c
[r-cairo]: https://github.com/wch/r-source/blob/0fa000308ea74ee25671d3ab2475d63ea9ef19a0/src/library/grDevices/src/cairo/cairoBM.c
[r-quartz]: https://github.com/wch/r-source/blob/0fa000308ea74ee25671d3ab2475d63ea9ef19a0/src/library/grDevices/src/qdBitmap.c
