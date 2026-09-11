# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI wheels.
The release workflow publishes native Apple Silicon and Intel macOS wheels and ARM64 and x86-64 Linux wheels.
Linux wheels are built on Ubuntu 24.04 and require glibc 2.39 or later.
Wheel builds require Maturin 1.15 or later.
It does not publish a source distribution, Windows wheels, or GitHub release archives.

`Cargo.toml` is the package-version source of truth.
Keep the root `mcp-console` entry in `Cargo.lock` synchronized with it.
CI and release wheels build Console with stable Rust, independently of the sandbox runner's compiler.

## Private sandbox executable

`sandbox-runner.json` pins the runner source repository, release, commit, and protocol.
The pinned checkout's `codex-rs/rust-toolchain.toml` owns the runner's Rust toolchain configuration.
`uv tool install --reinstall .` prepares the native companion automatically before Maturin compiles MCP Console and assembles the wheel.
Editable source installations (`uv tool install --reinstall --editable .`) use the same preparation and packaging lock.
Source builds require Python 3.11 or later, Git, and rustup; rustup installs the pinned toolchain if needed.
The packaging backend calls `scripts/stage-sandbox-runner`, which fetches the exact revision into `target/sandbox-runner-cache/<commit>` within the source checkout.
The default build does not inspect or change other working checkouts.
To use a dedicated clean checkout at the pin, explicitly set `MCP_CONSOLE_SANDBOX_SOURCE`; CI and releases use a checkout within their own workspace.

Every source installation invokes the runner's Cargo build with its pinned toolchain and lockfile.
Staging reads `[toolchain].channel` from that file with Python's standard-library `tomllib` and selects it explicitly with `rustup run --install`.
This selection takes precedence over the caller's `RUSTUP_TOOLCHAIN` for runner commands.
Console's build retains the caller's toolchain selection.
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

Linux bundles also contain `libexec/bwrap` and include `bubblewrap-COPYING`, `bubblewrap-NOTICE`, and `bubblewrap-SOURCE.json` in the license directory.
The notice reproduces the copyright header from the pinned Bubblewrap source and identifies its source archive, build location, and Rust toolchain.
The generated source record identifies the pinned repository and revision, the vendored Bubblewrap directory, its Rust wrapper and build script, the stripped helper digest, and its ELF `DT_NEEDED` dependencies.
Staging rejects inherited `CODEX_BWRAP_SOURCE_DIR` and `CODEX_SKIP_BWRAP_BUILD` values, including empty values, before invoking Cargo for a Linux target.
Unset these options for package builds.
The verified checkout supplies Bubblewrap; the caller's normal native toolchain and pkg-config configuration still supply system dependencies.

Linux staging and wheel inspection require `readelf` from binutils.
Libcap linkage is determined from the helper's ELF, independently of pkg-config's requested linkage.
A `libcap.so.*` dependency records dynamic linkage; that system library is not bundled.
When libcap is linked statically, staging also verifies a defined libcap symbol in the unstripped helper and requires `MCP_CONSOLE_LIBCAP_NOTICE` to name a nonempty file containing the applicable redistribution notice for the libcap used by that build.
It packages that file as `libcap-NOTICE`; a subsequent dynamic build removes it.
Staging does not change the linking strategy, identify a libcap package version from its SONAME, or certify builder-supplied license text.
Release wheel smoke rejects empty or missing notices and a notice pointing to an obsolete source archive.
It checks the current source pin and source/build locations, the stripped helper digest, and the recorded linkage against the wheel's actual ELF dependencies.

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
Each source build reconciles the generated `libexec` and `share` trees, including when Cargo reuses its compiled output.
Staging removes obsolete files and preserves timestamps when the intended contents and permissions are unchanged.
`build.rs` verifies the prepared manifest and files and copies them beside native Cargo output; it neither builds the runner nor modifies wheel staging.
Unchanged native companions and generated Rust retain their timestamps so subsequent Cargo invocations can reuse the executable.
Direct staging, Cargo, and Maturin commands require exclusive use of their source checkout; release matrix jobs use separate checkouts.
Source distributions include the packaging backend, staging script, source pin, and data-directory marker, and omit generated companions.

CI separately caches completed staged runners, release wheels and native bundles, and Cargo build data for both workspaces.
Runner build-cache keys include the toolchain file from the checked-out source.
CI skips runner staging when both the finished runner and its build data are exact cache hits.
If either cache misses, staging invokes Cargo and saves the completed outputs before tests; a build-data miss still prepares the workspace for later source-install checks.
Cargo build data is restored across source, pin, and dependency changes within the same OS version, architecture, toolchain, applicable R version, and UTC week.
Cache keys use `ImageOS`, such as `macos26` or `ubuntu24`, so routine runner image revisions can reuse the same caches; the full `ImageVersion` remains in the logs.
Console keeps one cached build baseline per dependency set during that week.
Ordinary source edits reuse the baseline without uploading another large target-directory snapshot; Cargo updates the restored files for the current checkout.
This lets new PRs, later commits to a PR, and merge commits reuse the same baseline from `main`.
PR-specific caches remain available to that PR; the cache cleanup workflow deletes them when the PR closes so they do not crowd out shared caches.
All GitHub Actions caches intentionally reset each Monday in UTC, including uv, IR/renv, R package libraries, Cargo downloads, source archives, build data, and finished runners and wheels.
Every restore prefix includes the UTC ISO week, so a cache miss cannot restore data from an earlier week.
All CI builds enable incremental compilation, including release builds and source installation checks; Cargo decides which tracked inputs require rebuilding.
An exact match of source, packaging inputs, toolchain, R and Python versions, and OS version additionally permits skipping the release build and using the finished wheel and native bundle directly.
Otherwise, Maturin invokes Cargo with the restored build data and packages the updated output.
Unrelated workflow edits do not change cache keys.
Bump `CI_BUILD_CACHE_VERSION` in `.github/workflows/ci.yaml` when changing build inputs outside the hashed files, such as workflow build flags or native dependency setup; the version invalidates build data and finished outputs together.
Runner image updates can change native tools or libraries that Cargo does not track; bump this version if those changes require a fresh build before the next weekly reset.
Runner source archives preserve the timestamps used by Cargo while excluding Git metadata and the separately cached build directory.
PR and main runs save completed builds, including Clippy preparation, before tests.
R package checks use a separate cached library, and packaging and runtime preparation share a uv cache that retains downloaded wheels within the current week.
Python SDK integration tests resolve current releases on a fresh weekly uv cache and reuse satisfying environments afterward; MCP stays within major version 2.
Manual release builds also use a weekly uv cache; tag-driven release builds and publication do not restore uv caches.
CI keeps one job per platform and runs all current tests; source installation checks run last because they replace and hide the shared target directory.
Those installation checks still invoke Cargo and can reuse the prepared runner workspace.
Installation checks cover unstaged sources, compiler-flag changes between reinstalls, relocated bundles, bounded verification allocations, and rejection of missing or modified companions.
Linux staging first builds and strips the private bubblewrap helper, embeds that exact SHA-256 in the runner build, installs `libexec/bwrap`, and includes its license at `share/licenses/mcp-console/bubblewrap-COPYING`.
Rebuilding the helper therefore invalidates the runner's tracked digest input.
Builds require a C compiler, `pkg-config`, and libcap development files (`build-essential pkg-config libcap-dev` on Ubuntu); installations require `libcap.so.2`.
Linux smoke tests exercise the bundled helper with an empty `PATH` and evaluate R through default sandboxed `serve`.
CI permits unprivileged namespace setup on its disposable Ubuntu runners by disabling their AppArmor user-namespace restriction.
A local rehearsal must likewise run in an environment whose policy permits the bundled helper's namespace operations; an approved system `bwrap` alone does not verify that installation path.

On Ubuntu with `kernel.apparmor_restrict_unprivileged_userns=1`, a path-specific profile for `/usr/bin/bwrap` can allow ordinary sandbox runs while the relocated bundled helper fails with `RTM_NEWADDR: Operation not permitted`.
Check the kernel AppArmor records for a `net_admin` denial under the `unprivileged_userns` profile.
Keep the empty-`PATH` smoke checks: they verify that the installed bundle works without a host helper.

For local installation checks on such a host, use a disposable development container with the build prerequisites above and an isolated checkout.
Start it as container root with `--cap-add SYS_ADMIN --security-opt apparmor=unconfined --security-opt seccomp=unconfined`, then run `scripts/check` inside it.
These settings permit the nested namespace operations without changing the host's AppArmor policy.
Keep the checkout and `TMPDIR` on the same writable filesystem because the installation tests rename build artifacts into their temporary directory.
A non-root container with these flags can still encounter the host's unprivileged-user-namespace restriction.

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
