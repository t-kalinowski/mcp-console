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

`sandbox-runner.json` pins source, release, and commit.
The private runner protocol is unversioned and changes with the pinned revision.
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
The pinned `mcp-console-sandbox-windows` package supplies the Console helpers; `codex-windows-sandbox` retains the upstream Codex helpers and identity.
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
Loader paths and bundled dependencies resolve within the installed Unix prefix: wheel scripts go under `bin`, data under the prefix, and Python libraries under `lib/pythonX.Y/site-packages`.
`DT_NEEDED` dependencies must be library names without `/`; filename dependencies bypass loader search paths and are unsupported by the audit.
In particular, relative filenames depend on the launch directory even when a matching library exists under RPATH/RUNPATH.
`$ORIGIN` paths may traverse between these installed directories but must stay within the prefix; reports retain archive-member keys and use prefix-relative search paths.
Bundled dependencies inherit `DT_RPATH` along their loading chain, with each ancestor's paths resolved against its own installed `$ORIGIN`.
`DT_RUNPATH` applies only to direct dependencies and suppresses RPATH lookup for that object's direct dependencies.
The audit loads dependencies breadth-first in `DT_NEEDED` order, reusing already loaded libraries before searching paths again.
Independent executables do not share loaded libraries or loader paths.
Numeric GLIBCXX/CXXABI ceilings follow the advertised [manylinux policies](https://github.com/pypa/auditwheel/blob/7cec8ec5b1436336bc03e560b785cc63d9c4190f/src/auditwheel/policy/manylinux-policy.json), including architecture differences and legacy aliases; multiple tags use the strictest ceiling.
Named CXXABI versions must be permitted by every advertised policy: `TM_1` from manylinux 2.17 on both architectures, and `FLOAT128` from 2.24 on x86_64 only.
Named GLIBC requirements follow the same rule: `ABI_DT_RELR` from 2.36 on both architectures, and `ABI_DT_X86_64_PLT` / `ABI_GNU2_TLS` from 2.42 on x86_64 only.
These GLIBC requirements remain incompatible with the 2.35 release floor; unknown names, including `GLIBC_PRIVATE`, are rejected.
GCC requirements from libgcc use each tag's exact architecture-specific permitted versions; sparse version sets cannot be validated with a numeric ceiling.
An unrecognized policy is rejected rather than inferred from the build host.
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
Installed wheels retain the same digest verification.
Windows wheels keep the native Console executable under `libexec` alongside its helpers and expose `mcp-console` through an installer-generated Python entry point.
The entry point resolves the native executable through the installed distribution's file manifest and waits for its exit, preserving stdio and all 32 exit-status bits.
This keeps the native installation prefix intact when `uv tool install` copies the public command onto PATH.
The backend updates wheel records and prepared metadata consistently; editable builds use the same launcher.
`target/sandbox-runner-build.json` describes staged files under `wheel-data/data`.
Obsolete generated files are reconciled on staging.

Move the complete native bundle when relocating it; a symlink to the native executable works.
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

Windows CI keeps the pinned source at `.sandbox-runner-source` for the entire job, including wheel and source-installation builds via `MCP_CONSOLE_SANDBOX_SOURCE`.
It restores the source timestamp archive separately from `codex-rs/target` and the complete `wheel-data/data` plus `target/sandbox-runner-build.json` staging output.
Restoration validates archived contents against the clean checkout and applies only timestamps, preserving Git's native files and symlink representation.
GNU tar creation uses `--force-local` so absolute Windows drive paths are treated as local archive files.
The exact Cargo cache key includes the timestamp archive's digest so regenerating an evicted archive cannot permanently pair newer source mtimes with older build intermediates.
An exact staging/build cache pair may skip initial staging; compatible Cargo fallbacks still stage normally.
The completed-output key includes the sandbox's pinned Rust toolchain, source pin, Windows target, runner OS version, and staging recipe inputs.
Cargo's existing `build.rs` validation rejects missing or mismatched artifacts before cache saving or sandbox use, even on an exact restore.
Successful caches are saved before provisioning and acceptance/installation tests.
Cargo downloads have their own early checkpoint, matching the Unix layout; the Console build/dependency cache retains its final checkpoint.
Accounts, credentials, ACLs and provisioning state are never cached; provisioning and the full Windows gate still run.
Local/source builds always invoke Cargo.

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
Both images preinstall uv-managed CPython in the Console dependency cache so sandboxed startup can keep interpreter downloads disabled.
Each validation run refreshes R package preparation before copying its cache into the runtime, because IR's latest-package resolution markers expire after 24 hours while Docker layers do not.
Earlier system and preparation-tool layers remain cached; repeated runs pay the R package preparation cost so source builds finish while compilers are available.
Preparation uses serial make and `CXX17FLAGS=-O0 -g0` to bound DuckDB build memory; these settings do not affect the distributed wheel.
Compilers, development packages, build directories, and cached CMake are excluded from the final runtime.
Normal sandboxed startup keeps the documented Console cache policy.
The Linux builder prepares the same default R environment before installed-wheel smoke, so cold source compilation completes separately from the startup observation budget.
This preparation uses the same language-only compiler flags and runtime prerequisites as the floor fixture; it does not change the shipped binaries or smoke assertions.
Clean floor validation runs in separate native jobs that download the smoke-tested wheel artifacts, giving language preparation its own bounded job budget.
Both wheel-building and floor-validation jobs must pass before publication.
Rerunning a failed floor job reuses the built wheel rather than rebuilding it.

The probes require native namespace support and run in disposable containers with `SYS_ADMIN`, unconfined seccomp, and unconfined AppArmor.
They do not change host sysctls.
Startup, Python/R evaluation, bounded shutdown, installed loader resolution, and existing sandbox installation acceptance must pass.
Installed loader checks run `ldd` from each dynamic ELF executable, including additional wheel entry points, so bundled libraries resolve in their executable's inherited RPATH context.
Shared libraries are not required to load independently; `inspect-wheel` still audits every ELF member and its dependency chains.
The empty-PATH probe establishes bundled-helper execution; host-helper precedence and integrity checks remain covered by installed acceptance.
A namespace denial is separate from a loader/ABI failure, and a skipped native probe does not pass the gate.

The release workflow uploads per-architecture `linux-compatibility-*` builder ABI reports and `linux-runtime-*` floor validation evidence.
Together these contain the wheel SHA-256, complete ELF/tag report, image digests/platforms, exact probe command, OS/kernel/library/language versions, and results.
Keep ARM64 and x86_64 reports separate.
Both independent controlled-Jammy builds at source `d26f9771` used Rust 1.95.0, GCC 11, Maturin 1.15.0, and the unchanged companion pin.
All three shipped ELF executables required at most GLIBC 2.34, with matching `manylinux_2_34` filename/WHEEL tags, no GLIBCXX/CXXABI requirements, and no RPATH/RUNPATH or extra ELF members.
Clean runtime tests used Ubuntu 22.04 with glibc 2.35, libgcc/libstdc++ 12.3, libcap 2.44, Python 3.13.16, uv 0.12.22, and R 4.6.1 for R-present coverage.
Both native architectures passed no-R and R-present startup, language, bounded shutdown, loader resolution, and installed acceptance.
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
