# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI wheels.
The release workflow publishes native Apple Silicon and Intel macOS wheels and ARM64 and x86-64 Linux wheels.
Linux wheels are built on Ubuntu 24.04 and require glibc 2.39 or later.
It does not publish a source distribution, Windows wheels, or GitHub release archives.

`Cargo.toml` is the package-version source of truth.
Keep the root `mcp-console` entry in `Cargo.lock` synchronized with it.

## Private sandbox executable

`sandbox-runner.json` pins the runner source repository, release, commit, protocol, and Rust toolchain.
On macOS, build and stage it from a clean checkout at that commit before building MCP Console:

```sh
scripts/stage-sandbox-runner ~/github/t-kalinowski/codex
```

The script builds with the pinned toolchain and lockfile, stages the executable at `wheel-data/data/libexec/mcp-console-sandbox`, and records its source revision, target triple, and SHA-256 in `target/sandbox-runner-build.json`.
By default it builds for the pinned compiler's native target, passing that target explicitly to Cargo.
Use `--target aarch64-apple-darwin` or `--target x86_64-apple-darwin` when building for an explicit target; inherited Cargo default-target settings do not change this selection.
The source checkout remains unchanged.
MCP Console's build verifies the staged revision, target, and digest and binds the runner's digest and protocol version into the executable.
Stage the runner again for the intended target before changing MCP Console's build target.
Cargo builds also copy the verified executable into the target prefix's `libexec` directory, so binaries in `debug`, `release`, and custom profile directories use the same relative lookup as installed wheels.
Sandbox launches reject a missing or mismatched private runner.

The current pin uses protocol 2: invoke the runner with `--bootstrap-fd <N>` and inherit a readable descriptor greater than 2.
Send one four-byte big-endian length followed by UTF-8 JSON on that setup descriptor after spawning; leave the target's original stdin attached to fd 0.
The runner consumes exactly the frame and closes setup before native launch without waiting for EOF.
When advancing the pin, inspect the package's `PROTOCOL.md`, implementation, executable contract tests, and `rust-toolchain.toml`; update all callers together.
Release smoke exercises the installed runner directly with a non-default descriptor and open, idle stdin, then checks the public launcher and artifact verification.

Maturin includes the staged executable under the installation's private `libexec` directory, with the upstream license and notice under `share/licenses/mcp-console/`.
Only `mcp-console` is installed as a public command.
On macOS, CI and the release workflow build the pinned source before packaging and verify the private layout, executable permissions, and sandbox launches from both the Cargo binary and installed command with an empty `PATH`.
Linux builds use the tracked `wheel-data/data` directory without staging a sandbox executable; its placeholder is excluded from wheels.
The Linux smoke test verifies installation and evaluates R through `serve --no-sandbox`.

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
