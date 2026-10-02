# Development

Run commands from the repository root.
Read [AGENTS.md](../AGENTS.md) for change and publishing rules, [architecture](ARCHITECTURE.md) for ownership, and the [boundary guide](../tests/boundaries/README.md) for test contracts.

## Setup

These workflow scripts target macOS and Linux.
For Windows local unsandboxed R/Python, use the [native setup and validation commands](WINDOWS.md); run them exclusively in the checkout.
Windows packaging skips companion staging and serializes with a blocking native lock outside `target`.

`scripts/preflight` inventories tools, runtimes, companion staging, and caches; `--json` produces structured output.
It does not install or build.
Optional provider probes may contact configured services.
Missing optional capabilities are skips; required probe failures fail the command.
Success is an inventory result, not proof that the project builds.

For direct development:

```sh
scripts/stage-sandbox-runner
scripts/with-checkout cargo build --release --target-dir target
```

For a source installation:

```sh
scripts/with-checkout uv tool install --reinstall .
```

The packaging backend stages the companion automatically.
Direct Cargo/Maturin builds need staging first.
See [release and build setup](../RELEASE.md) for prerequisites, companion pinning, and cache recovery.

### Windows development through WSL2

Use a Linux checkout in each distro, such as `~/src/mcp-console`, with Linux Git and LF line endings.
Keep the checkout, `target`, and runtime caches in the [WSL filesystem](https://learn.microsoft.com/en-us/windows/wsl/filesystems#file-storage-and-performance-across-file-systems).
A Windows Git worktree can contain CRLF scripts and a Git-directory pointer that Linux cannot resolve.
Independent Ubuntu and Fedora clones also keep their build outputs and checkout ownership separate; transfer committed changes through Git.
The distros test different userspaces but share the WSL kernel.

The following system prerequisites were exercised on Ubuntu 26.04 and Fedora 44 with R/Python/SQL development:

```sh
# Ubuntu
sudo apt-get update
sudo apt-get install -y \
    build-essential git curl ca-certificates pkg-config libcap-dev tar xz-utils \
    libcurl4-openssl-dev binutils python3 python3-dev python3-venv bubblewrap \
    ripgrep r-base-dev libuv1-dev libxml2-dev libssl-dev libcairo2-dev \
    libfontconfig1-dev libharfbuzz-dev libfribidi-dev \
    libjpeg-dev libpng-dev libtiff-dev

# Fedora
sudo dnf install -y \
    gcc gcc-c++ make git gawk tar xz ca-certificates pkgconf-pkg-config \
    libcap-devel libcurl-devel binutils python3 python3-devel bubblewrap \
    ripgrep R-devel libuv-devel libxml2-devel openssl-devel cairo-devel \
    fontconfig-devel harfbuzz-devel fribidi-devel \
    libjpeg-turbo-devel libpng-devel libtiff-devel
```

Install Linux rustup and uv, and ensure `curl` is available, before the build commands above.
Use Rust at least as new as `Cargo.toml` requires, with Clippy and rustfmt installed.
Run builds as your ordinary Linux user; use root only for system packages.
Install the resolver and formatting tools:

```sh
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
uv tool install r-lib-ir
uv tool install ruff
uv tool install yamark
uv tool install air-formatter
```

Use Python 3.13 for development scripts, matching CI.
Ubuntu's system Python 3.14 did not execute the isolated virtualenv `sitecustomize` hooks used by existing resolver fixtures.
Create the development environment once, then activate it in each development shell:

```sh
uv python install 3.13
uv venv --python 3.13 .dev-workflow/python-dev
source .dev-workflow/python-dev/bin/activate
export UV_PYTHON=3.13
export CARGO_BUILD_JOBS=4
export MAKEFLAGS=-j4
```

Prepare the default R packages serially before the first concurrent transcript run, as CI does.
This avoids multiple cold-cache sessions racing while bootstrapping the shared resolver tooling.
On Fedora, `export LIBARROW_BINARY=true` opts into [Arrow's compatible Linux C++ binaries](https://arrow.apache.org/docs/r/articles/install.html#r-source-package-with-libarrow-binary), avoiding a full C++ source build when one is available.
DuckDB's R package may still require a lengthy first source build.

```sh
scripts/with-checkout ir run \
    --with DBI --with arrow --with duckdb --with jsonlite \
    --with nanoarrow --with pillar --with reticulate --with tibble \
    --with tidyverse --with utf8 --with yyjsonr \
    --isolated --vanilla -e 'sessionInfo()'
scripts/preflight
scripts/check
```

Use preflight's capability results and [Linux compatibility](LINUX_COMPATIBILITY.md) to diagnose namespace or native-sandbox failures.
For an MCP client on Windows, launch the built server through WSL using its Linux workspace and environment; for example, from PowerShell:

```powershell
wsl -d Ubuntu-26.04 --cd /home/USER/src/mcp-console --exec bash -lc 'source .dev-workflow/python-dev/bin/activate && exec target/release/mcp-console serve'
```

Replace the distro, user, and workspace path for the session.
`serve` expects MCP protocol input over stdio.

## Validation ladder

| Command                               | Use                                                      |
| ------------------------------------- | -------------------------------------------------------- |
| `scripts/test --full --list`          | Discover cases without building the executable.          |
| `scripts/test --locate SELECTOR`      | Find case source and its snapshot.                       |
| `scripts/test BOUNDARY/SUITE[::CASE]` | Focused red/green loop.                                  |
| `scripts/test --update SELECTOR`      | Accept an intentional snapshot change.                   |
| `scripts/format`                      | Run all formatters; inspect their results and the diff.  |
| `scripts/check`                       | Ordinary final local gate.                               |
| `scripts/check --full`                | Exhaustive local validation when requested or warranted. |

Start a behavior change with a failing public regression; establish the existing public baseline for a refactor.
After implementation, rerun the focused case.
Regenerate only intentional snapshot changes, then rerun without `--update`.
Review the diff, embedded-program indentation, and `git diff --check` after formatting.
A shared fixture change may need snapshots from other platforms; a local skip does not validate them.

The default `scripts/check` stages the companion, validates extracted runtime sources and architecture, checks Rust formatting and Clippy, runs debug Rust tests, builds the release executable, and runs the explicit smoke transcript profile.
`--quick` is an alias for this default, not a narrower check.

The full gate adds repository-tooling self-tests, all capability-applicable transcripts, and source/wheel installation checks.
Installation checks run last because they temporarily replace the application `target` directory.
CI runs the full profiles and is the comprehensive merge gate.
Run the owning focused tests when changing tooling; the default gate does not cover all tooling regressions.

`scripts/test` without selectors runs the smoke profile in [`_profiles.py`](../tests/boundaries/_profiles.py); `--full` runs all applicable cases.
Explicit selectors keep their scope with either profile.
Only an unscoped full run audits orphan snapshots, and only a successful full update removes them.
Focused updates preserve unselected snapshots.

Set `MCP_CONSOLE_TEST_BINARY` to an absolute installed executable to skip the checkout build for transcripts; sandboxed cases still need its companion bundle.
Use `--jobs N` and `--timeout SECONDS` to control case concurrency and deadlines.
The default concurrency is twice the logical CPU count, with a minimum of four cases.

CI uses six transcript workers on Linux and macOS.
To compare concurrency on the same revision, dispatch the CI workflow with `transcript_jobs` set to `6` and `12`.
These runs have separate cancellation groups and retain the full integration and packaging gate.
Compare successful runs on the same runner image with similar cache hits; repeat the pair before drawing performance conclusions.
The job summary and `transcript-metrics-*` artifacts record the tested revision, worker count, runner image, CPU count, native `time` resource report, and per-execution timings.
Case timings overlap and must not be summed as elapsed time.
The resource report's maximum RSS is a per-process high-water mark, not the combined peak of concurrent workers; its units are bytes on macOS and KiB on Linux.
Use it as a diagnostic, not proof that aggregate memory use fits the runner.

## Find the public test

Start at the outermost boundary that observes the change.
Use `--full --list`, `--locate`, and scoped source searches rather than maintaining a second inventory of tests.
Runtime, output, recording, provider, and private-protocol cases live under their corresponding boundary subjects.
[Authoring](../tests/boundaries/AUTHORING.md) covers embedded programs and causal lifecycle fixtures.

## Review boundary

Before a cross-cutting change, identify the observable behavior, owning modules, public cases, expected snapshot/platform changes, and intended PR base.
Keep these task-specific notes in the checkpoint rather than permanent docs.

```sh
scripts/review-diff BASE
git diff --merge-base BASE
```

The report measures from the merge base with `HEAD`, includes tracked working tree edits, and separates production, tooling, tests, docs, and snapshots.
Stage intended new files before measuring.
For a stack, use the layer's intended parent, not `main`.
Line counts help review planning; they do not establish semantic size.

## Checkout ownership

Build, staging, validation, and packaging entry points share `.dev-workflow/checkout.lock`, outside `target`.
Use `scripts/with-checkout` for direct commands that mutate build state.
Do not delete locks to bypass a busy owner, start concurrent children under inherited ownership, or share application `target` / wheel staging between checkouts.

Separate worktrees may run concurrently.
The pinned companion's source and Cargo cache are shared under `${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/sandbox/` and serialized by `<source-checkout>.stage.lock` through preparation, build, and copying.
An explicit `MCP_CONSOLE_SANDBOX_SOURCE` uses the same ownership rules; an explicitly set `XDG_CACHE_HOME` must be absolute.

The wrapper sends `SIGTERM` on cancellation, then escalates after five seconds and retires its process group before releasing ownership.
Deliberately detached children remain their caller's responsibility.
These guarantees require the owner to survive: after a crash or `SIGKILL`, establish that surviving mutators have stopped before starting another.
Lock diagnostics can be stale.

## Resume from a small checkpoint

For a new task, copy [the template](templates/task-checkpoint.md) to the ignored `.dev-workflow/task.md`.
Record scope, branch/base, revision, working-tree edits, last validation and log, next action, and the requested stopping condition.
Update it before handoff; link evidence instead of pasting logs.

On resume, compare the checkpoint with `git status --short --branch`, `HEAD`, the intended base, staged/unstaged diffs, and untracked files.
Matching `dirty` labels do not identify the same edits.
Reconstruct stale or missing facts before relying on them; do not overwrite an existing checkpoint with the template.

Validation records live in `.dev-workflow/runs/<run>/result.json`, with phase logs and per-execution `case-timings.jsonl`.
Records describe the revision and worktree at admission.
Missing metadata stays unknown; an unfinished record is not proof that a process is running.
Return to its original execution handle or establish cleanup with its owner before rerunning.
A focused pass, old revision, or dirty tree is not evidence of a full pass on the current clean revision.

Report the commands and scope actually run, unavailable coverage, and observed hosted state separately.
Honor the requested stopping point: opening a PR does not imply waiting for CI or starting a watcher.
