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
macOS Cargo builds prepare it automatically with `scripts/stage-sandbox-runner`, including when invoked by `uv tool install --reinstall .`.
The script fetches the exact source revision into `sandbox-runner-cache/<commit>` under Cargo's target prefix and builds it with the pinned toolchain and lockfile.
Source builds require Python 3, Git, and rustup; rustup installs the pinned toolchain if needed.
The runner has its own Cargo build directory and jobserver, so the nested build also works when the outer Cargo uses `--jobs 1` or a custom target directory.
It uses the pinned compiler's default macOS deployment target, independently of the application's `MACOSX_DEPLOYMENT_TARGET`.
Inherited generic Rust flags and Cargo build, profile, and target settings, including compiler and linker overrides, are removed from the nested build environment.
The runner and its host build dependencies use `/usr/bin/cc`, bypassing `cc` wrappers on the caller's PATH.
Cargo runs from `/` with an explicit manifest and the pinned workspace configuration, so it does not discover configuration in the caller's checkout or home directory.
Root-level `/.cargo/config` or `/.cargo/config.toml` is unsupported and causes an error before building.
The runner's Cargo home and dependency cache live in `codex-rs/target/cargo-home` within its source checkout; rustup keeps its normal toolchain cache.
Completed runner bundles are cached separately under `sandbox-runner-cache/artifacts/`, keyed by the pin, staging script, and target.
Cache hits verify the executable, license, and notice checksums and copy the bundle without fetching sources or invoking Cargo.

The source checkout and runner build stay under the selected target prefix.
To use a dedicated clean checkout at the pin on a cache miss, explicitly set `MCP_CONSOLE_SANDBOX_SOURCE`; the release workflow uses a checkout within its own workspace.
The default build does not inspect or change other working checkouts.
For a standalone runner build, run `scripts/stage-sandbox-runner`; its output is `target/sandbox-runner/`.
Use `--target aarch64-apple-darwin` or `--target x86_64-apple-darwin` for an explicit target.
Without that option, the standalone script selects the pinned compiler's native target.

Staging strips the distributed runner with `xcrun strip -S -x` before computing its digest; the original executable remains in the nested Cargo build directory for debugging.
MCP Console verifies the runner's source revision, target, and SHA-256 of each bundled file before packaging the executable, upstream license, and notice.
The wheel installs this relocatable layout:

```text
bin/mcp-console
libexec/mcp-console-sandbox
share/licenses/mcp-console/LICENSE
share/licenses/mcp-console/NOTICE
```

The main executable resolves the runner relative to its own canonical path and verifies all three companion files using streaming SHA-256 with a bounded buffer on every sandbox launch.
Missing or modified files produce an installation error.
Move the complete bundle when relocating it; a symlink to `bin/mcp-console` also works.
There is no embedded payload, extraction step, or runtime runner cache.
Sandbox launches do not download anything or search PATH for the runner.
Native Cargo builds put the companions under the target prefix, alongside the `debug` and `release` directories.
This native bundle requires Cargo's default shared build/target layout.
Use `CARGO_TARGET_DIR` or `--target-dir` to move it; running the native Cargo output with a separate intermediate directory (`CARGO_BUILD_BUILD_DIR` or `build.build-dir`) is unsupported.
Cargo does not expose the invoking command's final `--target-dir` to build scripts, so the build script cannot reliably find that destination independently of `OUT_DIR`.
Wheel installations use the staged wheel data independently of the native bundle layout.
Use `uv tool install --reinstall .` to install a development checkout: `cargo install` copies only the main executable and cannot install the companion bundle.

The current pin uses protocol 2: invoke the runner with `--bootstrap-fd <N>` and inherit a readable descriptor greater than 2.
Send one four-byte big-endian length followed by UTF-8 JSON on that setup descriptor after spawning; leave the target's original stdin attached to fd 0.
The runner consumes exactly the frame and closes setup before native launch without waiting for EOF.
When advancing the pin, inspect the package's `PROTOCOL.md`, implementation, executable contract tests, and `rust-toolchain.toml`; update all callers together.
Release smoke exercises the installed runner directly with a non-default descriptor and open, idle stdin, then checks the public launcher and artifact verification.

The tracked `wheel-data/data` directory lets Maturin prepare metadata before the first build; Cargo fills it with verified companion files during compilation.
Wheel packaging requires exclusive use of its source checkout until Maturin finishes writing the archive.
Use separate source checkouts for concurrent builds; different Cargo target directories do not isolate wheel staging.
The release matrix gives each target its own checkout.
Source distributions retain the directory marker and omit generated companions so they can build for the destination machine.
CI caches the completed runner independently of the application's dependencies, so ordinary changes do not rebuild the runner or restore its source and dependency graph.
Successful main-branch checks save this cache before the Rust cache action removes non-Cargo artifacts.
uv source installation and wheel construction share the application's Cargo target directory and use Cargo's normal parallelism.
The small staging-script fixture checks isolation from the outer jobserver and compiler environment.
Installation checks use unstaged sources, hide the build artifacts, and check the installed commands with a decoy runner on PATH.
Wheel verification also checks sandbox launches with an empty PATH, bundled license notices, relocation without a writable home directory, bounded verification allocations, and rejection of missing or modified companions.
Each build clears the generated wheel data before staging, removing stale files from previous targets or staging recipes, including cached builds.
Linux builds require no sandbox runner and leave the generated wheel data empty.
The Linux installation smoke test evaluates R through `serve --no-sandbox`.

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

Verify these commands on both Apple Silicon and Intel macOS.
On ARM64 and x86-64 Linux, omit the `sandbox` invocation and add `--no-sandbox` when starting `serve` through an MCP client.
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
