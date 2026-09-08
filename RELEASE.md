# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI wheels.
The initial release publishes native Apple Silicon and Intel macOS wheels.
It does not publish a source distribution, Linux or Windows wheels, or GitHub release archives.

`Cargo.toml` is the package-version source of truth.
Keep the root `mcp-console` entry in `Cargo.lock` synchronized with it.

## Private sandbox executable

`sandbox-runner.json` pins the runner source repository, release, commit, protocol, and Rust toolchain.
Build and stage it from a clean checkout at that commit before building MCP Console:

```sh
scripts/stage-sandbox-runner ~/github/t-kalinowski/codex
```

The script builds with the pinned toolchain and lockfile, stages the executable at `target/private-wheel-data/data/libexec/mcp-console-sandbox`, and records its source revision, target triple, and SHA-256 in `target/sandbox-runner-build.json`.
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
CI and the release workflow build the pinned source before packaging and verify the private layout, executable permissions, and sandbox launches from both the Cargo binary and installed command with an empty `PATH`.

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

## Publish 0.0.3

Version `0.0.3` records the final release with the in-project sandbox implementation.
Merge the release metadata with `Cargo.toml` and `Cargo.lock` both at `0.0.3`, then confirm push CI passes on `main` for that exact commit.

A manual run of the Release workflow builds and smoke-tests both wheels for inspection but does not publish them.

Create the release from a clean, current `main` checkout:

```sh
git switch main
git pull --ff-only
scripts/format
scripts/check

git tag -a v0.0.3 -m "Release v0.0.3"
git push origin v0.0.3
```

The tag-triggered Release workflow verifies that the tag matches `Cargo.toml`, builds both native wheels, install-tests them with `uv`, and publishes them through PyPI Trusted Publishing.

## Verify the publication

Use clean `uv` directories when testing the public index:

```sh
cache_dir="$(mktemp -d)"
tool_dir="$(mktemp -d)"
bin_dir="$(mktemp -d)"

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uvx mcp-console@0.0.3 --version

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uvx mcp-console@0.0.3 --help

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uv tool install 'mcp-console==0.0.3'

"$bin_dir/mcp-console" --version
"$bin_dir/mcp-console" --help
"$bin_dir/mcp-console" sandbox -- /usr/bin/true
```

Verify these commands on both Apple Silicon and Intel macOS.
Also start

```sh
uvx mcp-console@0.0.3 serve
```

through an MCP client.
`serve` waits for protocol input; waiting is not an interactive-command failure.

After exact-version verification, test unqualified resolution in fresh `uv` directories:

```sh
uvx mcp-console --help
uv tool install mcp-console
```

Leave PyPI releases `0.0.1` and `0.0.2` unchanged and unyanked.

## Recover from a failed release

PyPI versions and filenames are immutable.

If publication fails, rerun the failed job from the same workflow run while its original wheel artifacts remain available.
Do not start a fresh build and expect it to replace an uploaded wheel with the same filename.

If `0.0.3` is defective after publication, fix the defect and publish a new version such as `0.0.4`.
Do not move, delete, or reuse `v0.0.3`.
