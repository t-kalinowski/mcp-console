# Releasing MCP Console

Tag-driven releases publish exactly four binary-only PyPI wheels: Apple Silicon and Intel macOS, ARM64 and x86-64 Linux.
Linux builds use Ubuntu 24.04 and require glibc 2.39+.
The release workflow publishes no source distribution, Windows wheel, or GitHub release archive.
`Cargo.toml` owns the version; keep the root `mcp-console` entry in `Cargo.lock` synchronized.

## Private sandbox executable

Source builds need Python 3.11+, Git, rustup, and platform build tools.
Console's minimum Rust version is in `Cargo.toml`; the pinned companion's `codex-rs/rust-toolchain.toml` independently selects its compiler.
Wheel builds need Maturin 1.15+.
R/libR/packages are not build or Python-only execution prerequisites.

Experimental Windows source builds use the MSVC toolchain and Windows SDK, skip companion staging, and support local unsandboxed R/Python only; see [Windows setup and validation](docs/WINDOWS.md).
Windows packaging holds a blocking native checkout lock through wheel creation and rejects staged Unix companion files.
The companion staging instructions below apply to macOS and Linux.
Windows direct build commands use `scripts/with-checkout.cmd` and share ownership with packaging; `scripts/check.cmd --full` exercises local wheel and source installation.

On macOS, install Xcode Command Line Tools.
Ubuntu builds need a C toolchain, `pkg-config`, libcap development files, libcurl development files for R resolver bootstrap, and binutils (`readelf` / `strip`).
Packages may need additional system libraries.
Runtime Linux installations need the helper's system dependencies, including dynamically linked libcap when selected.

`sandbox-runner.json` pins source, release, commit, and protocol.
The packaging backend stages the companion before Maturin builds, including editable installs:

```sh
scripts/with-checkout uv tool install --reinstall .
```

For direct Cargo/Maturin builds, run `scripts/stage-sandbox-runner` first.
Staging supports explicit macOS/Linux ARM64/x86-64 targets with `--target`; automatic uv installation uses the native target.
`cargo install` is insufficient because it copies only the main executable.

The automatic companion checkout and build cache live under `${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/sandbox/<repository>/<commit>/source`.
An explicit `XDG_CACHE_HOME` must be absolute.
`MCP_CONSOLE_SANDBOX_SOURCE` may select a dedicated clean checkout at the pin.
Staging invokes Cargo with the companion's own lockfile/toolchain, overriding `RUSTUP_TOOLCHAIN` only for that build; Console keeps the caller's toolchain.

Use `scripts/stage-sandbox-runner --describe` to locate build inputs.
After changing compilers/linkers through PATH, SDKs, or system libraries that Cargo cannot track, clean the affected build directories, including the companion's `codex-rs/target`.
Sharing download/build caches does not authorize concurrent mutation: follow [checkout ownership](docs/DEVELOPMENT.md#checkout-ownership).

### Bundle and verification

```text
bin/mcp-console
libexec/mcp-console-sandbox
share/licenses/mcp-console/{LICENSE,NOTICE}
```

Linux also bundles `libexec/bwrap` and Bubblewrap license, notice, and source metadata.
Staging strips distributed binaries, records SHA-256 digests, and rejects inherited `CODEX_BWRAP_SOURCE_DIR` / `CODEX_SKIP_BWRAP_BUILD`, even empty.
The verified pinned source must supply Bubblewrap.

Linux staging inspects actual ELF linkage.
Dynamic libcap is not bundled; static libcap requires a nonempty `MCP_CONSOLE_LIBCAP_NOTICE` file with the applicable redistribution notice.
The build does not certify that supplied license text.
Wheel smoke verifies notices, source identity, helper digest, and actual linkage.

`scripts/build_backend.py` owns staging through wheel creation.
`build.rs` verifies and copies prepared files beside native Cargo output; it does not build the runner or mutate wheel staging.
`target/sandbox-runner-build.json` describes staged files under `wheel-data/data`.
Obsolete generated files are reconciled on staging.

Move the complete bundle when relocating it; a symlink to the main executable works.
Native builds require Cargo's shared build/target layout: move it with `CARGO_TARGET_DIR` / `--target-dir`, not a separate `CARGO_BUILD_BUILD_DIR`.
Sandbox launch verifies the runner/licenses without downloading or extracting anything.
A trusted host bwrap may take precedence; when the bundled helper is selected, it is verified and executed through the same descriptor.

When advancing the pin, review the companion's `PROTOCOL.md`, implementation, executable tests, and toolchain together.
Keep the environment-config and framed bootstrap-descriptor smoke checks.
[Sandbox architecture](docs/SANDBOX.md) explains the launch contract; release prose is not another protocol definition.

### CI caches and Linux rehearsal

CI caches are bounded by OS, architecture, toolchain/dependency inputs, and UTC ISO week.
Cargo freshness still applies when a finished artifact is not an exact hit.
Bump `CI_BUILD_CACHE_VERSION` in `.github/workflows/ci.yaml` when workflow build flags or native dependencies change outside hashed inputs.
Do not treat Cargo reuse as detection of all external compiler/SDK changes.

Keep the installed bundled-helper smoke with an empty PATH; a working system bwrap does not validate the relocated bundled helper.
Ubuntu AppArmor user- namespace restrictions can permit `/usr/bin/bwrap` but reject the bundle.
Do not weaken a user's host policy merely to pass a test.
Use an approved disposable build environment; [Linux compatibility](docs/LINUX_COMPATIBILITY.md) explains runtime requirements and diagnosis.
Installation tests need the checkout and `TMPDIR` on the same writable filesystem for renames.

## One-time PyPI setup

Create the GitHub Actions environment `pypi`, then configure the existing `mcp-console` PyPI project's Trusted Publisher for owner `t-kalinowski`, repository `mcp-console`, workflow `release.yml`, and environment `pypi`.
Do not add a `PYPI_TOKEN` secret.
Only publication receives an OIDC token.

## Prepare and rehearse

Choose an unpublished `X.Y.Z` after checking public PyPI and remote tags.
Update Cargo's version and lockfile entry.
Regenerate affected CLI/MCP snapshots, updating the [canonical handshake](tests/boundaries/README.md#canonical-handshake) first; inspect for intended changes, then run `scripts/format` and `scripts/check --full`.

Rehearse the release branch before tagging:

```sh
gh workflow run release.yml --ref release/X.Y.Z
```

All four wheel builds and installed smoke tests must pass; manual dispatch must skip publication.
Exercise both mixed-language and R-free installed wheels.
`smoke-wheel --without-r` isolates R discovery.
Preserve distinct startup and response budgets: smoke explicitly restarts/establishes readiness before normal cell assertions even though built-in startup is asynchronous.

## Publish

Merge only after passing CI and a verified GPT connector reviewer thumbs-up for the current head.
Then require successful push CI on `main` for the exact merged commit.
Compare its tree with the rehearsed tree and rehearse again if different.

Tag the verified commit from a clean checkout, retaining its full SHA even if `main` advances:

```sh
release_version=X.Y.Z
release_sha=FULL_VERIFIED_MERGE_SHA
git switch --detach "$release_sha"
git tag -a "v$release_version" "$release_sha"
git push origin "refs/tags/v$release_version"
```

Use `-F <message-file>` for a noninteractive annotation.
The tag workflow checks version, main ancestry, exact-commit push CI, and exactly four install-tested wheels before Trusted Publishing.

## Verify the publication

Check `https://pypi.org/pypi/mcp-console/X.Y.Z/json`: all four wheels must be present, unyanked, and match tag-workflow artifact SHA-256 digests.
Verify consumer installation on all four platforms with fresh uv directories and the public index:

```sh
export UV_CACHE_DIR="$(mktemp -d)"
export UV_TOOL_DIR="$(mktemp -d)"
export UV_TOOL_BIN_DIR="$(mktemp -d)"
export UV_NO_CONFIG=1
uvx --isolated --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console@$release_version" --version
uv tool install --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console==$release_version"
"$UV_TOOL_BIN_DIR/mcp-console" --help
"$UV_TOOL_BIN_DIR/mcp-console" sandbox -- /usr/bin/true
```

Launch the exact version's `serve` through an MCP client, establish worker readiness, and verify Python `6 * 7` returns `42`; check R when installed.
`serve` waiting for protocol input is not an interactive-command failure.
Then repeat unqualified `uvx mcp-console --version` and `uv tool install
mcp-console` with fresh directories and the same isolation/public-index flags; confirm they select the new version.
Leave older releases unchanged.

## Recover from a failed release

First check PyPI and publication logs for any uploaded wheel.
Before upload, fix through the same review/CI/rehearsal process.
Moving a pushed tag requires explicit user authorization, confirmation the old run cannot publish, and a force-with-lease push limited to that verified tag ref, never `main`.

After **any** wheel is published, preserve the tag and uploaded files.
Never yank or remove a PyPI file.
For partial publication, rerun the failed job from the same workflow run while its original artifacts remain; do not rebuild that version or attempt to replace an uploaded filename.
Defective published code requires a new version.
