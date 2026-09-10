# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI wheels.
The release workflow publishes native Apple Silicon and Intel macOS wheels and ARM64 and x86-64 Linux wheels.
Linux wheels are built on Ubuntu 24.04 and require glibc 2.39 or later.
Wheel builds require Maturin 1.15 or later.
It does not publish a source distribution, Windows wheels, or GitHub release archives.

`Cargo.toml` is the package-version source of truth.
Keep the root `mcp-console` entry in `Cargo.lock` synchronized with it.

## Private sandbox executable

`sandbox-runner.json` pins the runner source repository, release, commit, protocol, and Rust toolchain.
`uv tool install --reinstall .` prepares the native companion automatically before Maturin compiles MCP Console and assembles the wheel.
Editable source installations (`uv tool install --reinstall --editable .`) use the same preparation and packaging lock.
Source builds require Python 3, Git, and rustup; rustup installs the pinned toolchain if needed.
The packaging backend calls `scripts/stage-sandbox-runner`, which fetches the exact revision into `target/sandbox-runner-cache/<commit>` within the source checkout.
The default build does not inspect or change other working checkouts.
To use a dedicated clean checkout at the pin, explicitly set `MCP_CONSOLE_SANDBOX_SOURCE`; CI and releases use a checkout within their own workspace.

Every source installation invokes the runner's Cargo build with its pinned toolchain and lockfile.
Cargo reuses its build intermediates under the runner checkout's `codex-rs/target` and checks inputs tracked by Cargo and dependency build scripts.
Source builds use the caller's normal Cargo configuration and download cache.
There is no separate local cache of finished runners that bypasses Cargo's freshness checks.
The runner build finishes before the application's Cargo build starts.

Build reuse has the same limits as an ordinary Cargo build: Cargo may not detect a different compiler or linker selected through `PATH`, changes to the tools themselves, or changes to an SDK or system library.
After changing those external build inputs, clean the affected Cargo build directories, including the runner checkout's `codex-rs/target`, before reinstalling.
For the default source checkout, that directory is `target/sandbox-runner-cache/<commit>/codex-rs/target`.
Automatic detection of those environment changes is outside the source installer's contract.

For direct Cargo builds or direct Maturin wheel builds, first run `scripts/stage-sandbox-runner`.
The script stages companions under `wheel-data/data` and records their digests in `target/sandbox-runner-build.json`.
Use `--target` with `aarch64-apple-darwin`, `x86_64-apple-darwin`, `aarch64-unknown-linux-gnu`, or `x86_64-unknown-linux-gnu` for an explicit target.
Without that option, the script selects the pinned compiler's native target.
The automatic uv installation path builds for the native target; use explicit staging and Maturin's target options for other targets.

Staging strips the distributed executables with `xcrun strip -S -x` on macOS and `strip --strip-unneeded` on Linux before computing their digests; the original executable remains in the nested Cargo build directory for debugging.
MCP Console verifies the runner's source revision, target, and SHA-256 of each bundled file before packaging the executable, upstream license, and notice.
The wheel installs this relocatable layout:

```text
bin/mcp-console
libexec/mcp-console-sandbox
share/licenses/mcp-console/LICENSE
share/licenses/mcp-console/NOTICE
```

The main executable resolves the runner relative to its own canonical path and verifies it and license files using streaming SHA-256 with a bounded buffer on every sandbox launch.
Linux helper verification belongs to native selection: a suitable trusted host helper takes precedence, while a selected bundled helper is hashed and executed through the same open descriptor.
Missing or modified selected files fail before target execution.
An unused bundled helper does not block a suitable host helper.
Move the complete bundle when relocating it; a symlink to `bin/mcp-console` also works.
There is no embedded payload, extraction step, or runtime runner cache.
Sandbox launches do not download anything or search PATH for the runner.
Native Cargo builds put the companions under the target prefix, alongside the `debug` and `release` directories.
This native bundle requires Cargo's default shared build/target layout.
Use `CARGO_TARGET_DIR` or `--target-dir` to move it; running the native Cargo output with a separate intermediate directory (`CARGO_BUILD_BUILD_DIR` or `build.build-dir`) is unsupported.
Cargo does not expose the invoking command's final `--target-dir` to build scripts, so the build script cannot reliably find that destination independently of `OUT_DIR`.
Wheel installations use the staged wheel data independently of the native bundle layout.
Use `uv tool install --reinstall .` to install a development checkout: `cargo install` copies only the main executable and cannot install the companion bundle.

The current protocol-2 pin also supplies standalone supervision.
Console selects application policy in one immutable environment value and execs `mcp-console-sandbox --config-env MCP_CONSOLE_SANDBOX_CONFIG -- COMMAND [ARG]...`.
The runner consumes and removes that variable; ordinary arguments, cwd, environment, and standard streams carry the target inputs.
No writer process or path-based setup handoff is needed.
The runner also retains `--bootstrap-fd <N>` for direct executable callers: a readable descriptor greater than 2 carries one four-byte big-endian length and UTF-8 JSON frame.
That interface closes setup after the frame without waiting for EOF or reading target stdin.
When advancing the pin, inspect the package's `PROTOCOL.md`, implementation, executable contract tests, and `rust-toolchain.toml`; update all callers together.
Release smoke exercises the installed runner directly with a non-default descriptor and open, idle stdin, then checks the public launcher and artifact verification.

The tracked `wheel-data/data` directory lets Maturin prepare metadata before the first build.
`build_backend.py` owns companion staging through wheel creation and holds a checkout-local lock until Maturin finishes writing the archive.
Each source build replaces the generated `libexec` and `share` trees, including when Cargo reuses its compiled output.
`build.rs` verifies the prepared manifest and files and copies them beside native Cargo output; it neither builds the runner nor modifies wheel staging.
Direct staging, Cargo, and Maturin commands require exclusive use of their source checkout; release matrix jobs use separate checkouts.
Source distributions include the packaging backend, staging script, source pin, and data-directory marker, and omit generated companions.

CI separately caches completed staged runners and Cargo dependencies for both workspaces.
PR and main runs save a newly built runner before tests, and Cargo dependencies can be saved when later checks fail.
Source installation checks still invoke Cargo and can reuse the prepared runner workspace.
Installation checks cover unstaged sources, compiler-flag changes between reinstalls, relocated bundles, bounded verification allocations, and rejection of missing or modified companions.
Linux staging first builds and strips the private bubblewrap helper, embeds that exact SHA-256 in the runner build, installs `libexec/bwrap`, and includes its license at `share/licenses/mcp-console/bubblewrap-COPYING`.
Rebuilding the helper therefore invalidates the runner's tracked digest input.
Builds require a C compiler, `pkg-config`, and libcap development files (`build-essential pkg-config libcap-dev` on Ubuntu); installations require `libcap.so.2`.
Linux smoke tests exercise the bundled helper with an empty `PATH` and evaluate R through default sandboxed `serve`.
CI permits unprivileged namespace setup on its disposable Ubuntu runners by disabling their AppArmor user-namespace restriction.
A local rehearsal must likewise run in an environment whose policy permits the bundled helper's namespace operations; an approved system `bwrap` alone does not verify that installation path.

## One-time PyPI setup

Before the first release:

1. Create a GitHub Actions environment named `pypi`.
2. In the existing PyPI project `mcp-console`, add a GitHub Trusted Publisher with:
   - owner `t-kalinowski`;
   - repository `mcp-console`;
   - workflow `release.yml`; and
   - environment `pypi`.
3. Do not add a `PYPI_TOKEN` repository secret.

The publication job is the only job granted an OpenID Connect token.

## Prepare and rehearse

Version `0.0.3` records the final release with the in-project sandbox implementation.
For a new release, choose an unpublished `X.Y.Z` version after checking the public PyPI project and remote tags.
Update `Cargo.toml` and the root `mcp-console` entry in `Cargo.lock`; `pyproject.toml` derives the version dynamically.

A version bump also changes CLI and MCP snapshots.
Regenerate affected snapshots with `scripts/test --update ...`, updating the full handshake snapshot before abbreviated transcripts as described in `tests/boundaries/README.md`.
Review the diffs for version-only changes, then run `scripts/format` and `scripts/check` before opening the release PR.

Rehearse the Release workflow on the release branch before tagging, replacing `release/X.Y.Z` with that branch:

```sh
gh workflow run release.yml --ref release/X.Y.Z
```

Wait for all four native wheel builds and smoke tests to pass.
Manual dispatch does not publish; the publication job should be skipped.
The rehearsal exercises installation and runtime setup on fresh runners, which ordinary CI with cached dependencies can miss.

Runtime preparation is lazy: MCP initialization alone does not start the worker.
The wheel smoke test explicitly calls `send(control="restart")` under the startup timeout before evaluating R under the response timeout.
Keep these phases separate when changing the test; cold dependency setup must not consume the ordinary evaluation budget.

## Publish

Merge the release PR only after CI passes and the GPT connector reviewer has given an actual thumbs-up for the current PR head, as required by `AGENTS.md`.
Then wait for successful push CI on `main` for the exact merged commit.
Confirm its tree matches the rehearsed commit; if it differs, rehearse the merged tree before tagging.

Create the tag from a clean checkout of that verified commit.
Replace the placeholders below with the chosen version and full merged commit SHA; keep that SHA fixed if `main` advances:

```sh
release_version=X.Y.Z
release_sha=FULL_VERIFIED_MERGE_SHA

git switch --detach "$release_sha"
git tag -a "v$release_version" "$release_sha"
git push origin "refs/tags/v$release_version"
```

The tag command opens an editor for the release annotation; use `-F <message-file>` when running noninteractively.
The tag-triggered workflow verifies the version match, ancestry on `main`, and successful push CI for the exact release SHA.
It builds all four native wheels, install-tests them with `uv`, checks that the artifact set contains exactly those four wheels, and publishes through PyPI Trusted Publishing.

## Verify the publication

Read `https://pypi.org/pypi/mcp-console/X.Y.Z/json` for the published version.
Confirm that all four expected macOS and Linux wheels are present and unyanked, and compare their SHA-256 digests with the artifacts from the tag-triggered workflow.

Use the chosen `$release_version`, clean `uv` directories, and the public index when testing consumer installation:

```sh
export UV_CACHE_DIR="$(mktemp -d)"
export UV_TOOL_DIR="$(mktemp -d)"
export UV_TOOL_BIN_DIR="$(mktemp -d)"
export UV_NO_CONFIG=1

uvx --isolated --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console@$release_version" --version

uvx --isolated --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console@$release_version" --help

uv tool install --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console==$release_version"

"$UV_TOOL_BIN_DIR/mcp-console" --version
"$UV_TOOL_BIN_DIR/mcp-console" --help
"$UV_TOOL_BIN_DIR/mcp-console" sandbox -- /usr/bin/true
```

Verify these commands on Apple Silicon and Intel macOS and on ARM64 and x86-64 Linux.
Also start

```sh
uvx --isolated --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console@$release_version" serve
```

through an MCP client, explicitly start the worker, and verify that R evaluates `6 * 7` to `42`.
`serve` waits for protocol input; waiting is not an interactive-command failure.

After exact-version verification, test unqualified resolution in fresh `uv` directories:

```sh
export UV_CACHE_DIR="$(mktemp -d)"
export UV_TOOL_DIR="$(mktemp -d)"
export UV_TOOL_BIN_DIR="$(mktemp -d)"

uvx --isolated --no-cache --no-sources --default-index https://pypi.org/simple \
  mcp-console --version
uv tool install --no-cache --no-sources --default-index https://pypi.org/simple \
  mcp-console
"$UV_TOOL_BIN_DIR/mcp-console" --version
```

Confirm that unqualified resolution selects the new version.
Leave all existing PyPI releases unchanged and unyanked.

## Recover from a failed release

First check the public PyPI version endpoint and publication logs to determine whether any wheel was uploaded.

If no wheel was uploaded, fix the failure through the same review, CI, and rehearsal steps.
Moving an already-pushed tag requires explicit user authorization.
With that authorization, confirm the old run cannot still publish, verify the remote tag target, and use a force-with-lease push limited to that tag ref.
Moving a tag does not require force-pushing `main`.

Once any wheel is published, preserve the tag and uploaded files.
Never yank or remove a PyPI file.
If publication partially fails, rerun the failed publication job from the same workflow run while its original wheel artifacts remain available.
Do not rebuild an already partially uploaded version or expect to replace an uploaded filename.
If the published code is defective, fix it and publish a new version.
