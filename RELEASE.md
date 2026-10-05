# Releasing MCP Console

Tag-driven releases publish exactly four binary-only PyPI wheels: Apple Silicon and Intel macOS, ARM64 and x86-64 Linux.
Linux builds use native Ubuntu 22.04 runners and target glibc 2.35+.
The release workflow publishes no source distribution, Windows wheel, or GitHub release archive.
`Cargo.toml` owns the version; keep the root `mcp-console` entry in `Cargo.lock` synchronized.

## Private sandbox executable

Source builds need Python 3.11+, Git, rustup, and platform build tools.
Console's minimum Rust version is in `Cargo.toml`; the pinned companion's `codex-rs/rust-toolchain.toml` independently selects its compiler.
Wheel builds need Maturin 1.15+.
R/libR/packages are not build or Python-only execution prerequisites.

Experimental Windows source builds use the MSVC toolchain, Windows SDK, and CMake and stage all three native sandbox executables; see [Windows setup and validation](docs/WINDOWS.md).
Windows packaging holds a blocking native checkout lock through wheel creation and replaces any staged Unix companion files with the pinned Windows bundle.
The platform-specific staging instructions below cover macOS and Linux.
Windows direct build commands use `scripts/with-checkout.cmd` and share ownership with packaging; `scripts/check.cmd --full` exercises local wheel and source installation.

On macOS, install Xcode Command Line Tools.
Ubuntu builds need a C toolchain, `pkg-config`, libcap and OpenSSL development files, libcurl development files for R resolver bootstrap, and binutils (`readelf` / `strip`).
Packages may need additional system libraries.
Runtime Linux installations need the helper's system dependencies, including dynamically linked libcap when selected.

`sandbox-runner.json` pins source, release, commit, and protocol.
The packaging backend stages the companion before Maturin builds, including editable installs:

```sh
scripts/with-checkout uv tool install --reinstall .
```

For direct Cargo/Maturin builds, run `scripts/stage-sandbox-runner` first.
Staging supports explicit macOS/Linux ARM64/x86-64 and Windows x64 MSVC targets with `--target`; automatic uv installation uses the native target.
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

Windows uses `.exe` suffixes and additionally bundles `mcp-console-sandbox-setup.exe` and `mcp-console-sandbox-runner.exe` under `libexec`; all helpers are verified before launch or setup.
Windows binaries are not processed by Unix strip tools.

Linux also bundles `libexec/bwrap` and Bubblewrap license, notice, and source metadata.
Staging strips distributed binaries, records SHA-256 digests, and rejects inherited `CODEX_BWRAP_SOURCE_DIR` / `CODEX_SKIP_BWRAP_BUILD`, even empty.
The verified pinned source must supply Bubblewrap.

Linux staging inspects actual ELF linkage.
Dynamic libcap is not bundled; static libcap requires a nonempty `MCP_CONSOLE_LIBCAP_NOTICE` file with the applicable redistribution notice.
The build does not certify that supplied license text.
Wheel smoke verifies notices, source identity, helper digest, and actual linkage.

`scripts/release.py inspect-wheel WHEEL --target TARGET --report abi.json` checks every ELF member, including private helpers and additional native libraries.
It checks architecture, loader, declared system libraries, loader paths, and symbol requirements against every advertised wheel tag; filename and WHEEL metadata must agree.
`--release` additionally enforces the glibc 2.35 floor and Ubuntu 22.04's libstdc++ symbol ceilings (GLIBCXX 3.4.30 / CXXABI 1.3.13).
The report records the wheel digest and each member's loader, dependencies, paths, and version requirements.
The release workflow also installs the built wheel in clean native floor runtimes; an ABI report alone is not a runtime pass.

Use `smoke-wheel WHEEL --installed-only --sandbox-pin PIN_FILE` in a runtime environment without a source checkout or Cargo outputs.
It reads package identity from the wheel, installs that artifact, and reuses the bundled-helper, startup, language, and bounded shutdown probes.
The ordinary smoke mode retains its comparison with the Cargo executable.

`scripts/build_backend.py` owns staging through wheel creation.
`build.rs` verifies and copies prepared files beside native Cargo output; it does not build the runner or mutate wheel staging.
On Windows, Cargo keeps each complete companion bundle in a content-addressed directory under `libexec` and binds the executable to it.
Older bundles remain available to active sandboxes; rebuilding never replaces their running helpers.
Installed wheels retain the flat layout above and the same digest verification.
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
The finished release cache includes the installed Maturin version used to build the wheel.
Cargo freshness still applies when a finished artifact is not an exact hit.
Bump `CI_BUILD_CACHE_VERSION` in `.github/workflows/ci.yaml` when workflow build flags or native dependencies change outside hashed inputs.
Do not treat Cargo reuse as detection of all external compiler/SDK changes.

Keep the installed bundled-helper smoke with an empty PATH; a working system bwrap does not validate the relocated bundled helper.
Ubuntu AppArmor user- namespace restrictions can permit `/usr/bin/bwrap` but reject the bundle.
Do not weaken a user's host policy merely to pass a test.
Use an approved disposable build environment; [Linux compatibility](docs/LINUX_COMPATIBILITY.md) explains runtime requirements and diagnosis.
Installation tests need the checkout and `TMPDIR` on the same writable filesystem for renames.

### Linux floor validation

The native bundle needs glibc's architecture loader, libc/libm, libgcc_s, and dynamically linked libcap for the current companion build.
On Ubuntu these come from `libc6`, `libgcc-s1`, and `libcap2`.
OpenSSL development files are needed during compilation, but the inspected wheel binaries do not depend on OpenSSL at runtime.
Default Python packages additionally need `libstdc++6`; R and its packages have their own runtime libraries and preparation requirements.
R-present validation uses current R from the signed CRAN Jammy repository because stock Ubuntu 22.04 R 4.1 cannot install current Arrow/DuckDB, which require R 4.2+.
The [runtime fixture](scripts/linux-wheel/Dockerfile) lists these language prerequisites separately from the native bundle.

Run on a matching native Docker host with the evidence directory shared with the daemon:

```sh
scripts/check-linux-wheel dist/*.whl \
    --target x86_64-unknown-linux-gnu --evidence linux-compatibility
```

Use `aarch64-unknown-linux-gnu` on ARM64; emulation is rejected.
`--docker-context` selects a daemon explicitly, for example `colima` on an ARM64 Mac.
Only the wheel and minimal release/installation harness enter the runtime containers.
The no-R image contains standalone Python 3.13 and uv, while the R image additionally retains separately prepared language libraries and resolver metadata.
Preparation uses serial make and `CXX17FLAGS=-O0 -g0` to bound DuckDB build memory; these settings do not affect the distributed wheel.
Compilers, development packages, build directories, source archives, and cached CMake are excluded from the final runtime.
Normal sandboxed startup keeps the documented Console cache policy.

The probes require native namespace support and run in disposable containers with `SYS_ADMIN`, unconfined seccomp, and unconfined AppArmor.
They do not change host sysctls.
Startup, Python/R evaluation, bounded shutdown, installed loader resolution, and existing sandbox installation acceptance must pass.
The empty-PATH probe establishes bundled-helper execution; host-helper precedence and integrity checks remain covered by installed acceptance.
A namespace denial is separate from a loader/ABI failure, and a skipped native probe does not pass the gate.

The release workflow uploads per-architecture `linux-compatibility-*` artifacts containing the wheel SHA-256, complete ELF/tag report, image digests/platforms, exact probe command, OS/kernel/library/language versions, and results.
Keep ARM64 and x86_64 reports separate.
Both independent controlled-Jammy builds at source `d26f9771` used Rust 1.95.0, GCC 11, Maturin 1.15.0, and the unchanged companion pin.
All three shipped ELF executables required at most GLIBC 2.34, with matching `manylinux_2_34` filename/WHEEL tags, no GLIBCXX/CXXABI requirements, and no RPATH/RUNPATH or extra ELF members.
Clean runtime tests used Ubuntu 22.04 with glibc 2.35, libgcc/libstdc++ 12.3, libcap 2.44, Python 3.13.16, uv 0.12.22, and R 4.6.1 for R-present coverage.
ARM64 passed no-R and R-present startup, language and installed acceptance; x86_64 no-R passed and R-present validation is pending.
The native older-runner rehearsal remains required before release.

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
