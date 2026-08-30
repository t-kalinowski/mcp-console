# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI wheels.
The release publishes native Apple Silicon and Intel macOS wheels and an x86-64 Linux wheel.
It does not publish a source distribution, Windows wheels, or GitHub release archives.

Each wheel includes the private sandbox runner below `libexec/`.
The runner is built from the exact Codex release and commit recorded in `sandbox-runner.json`; it is not installed as a public command.
The Linux wheel also includes the runner's private Bubblewrap companion at `libexec/codex-resources/bwrap`; it is not installed as a public command and no system Bubblewrap installation is required.
The wheel also includes the required license and notice material, including Bubblewrap's license, its exact corresponding-source notice, and the statically linked libcap notices in the Linux wheel.
The exact source archive is also listed in the package metadata and README so it appears with the PyPI distribution.

`Cargo.toml` is the package-version source of truth.
Keep the root `mcp-console` entry in `Cargo.lock` synchronized with it.

## Build the private runner locally

Check out the commit recorded in `sandbox-runner.json` in a clean Codex checkout, then run:

```sh
scripts/stage-sandbox-runner ~/github/t-kalinowski/codex
```

The script verifies the checkout's exact clean `HEAD`, builds with the recorded Rust toolchain and Codex lockfile, and stages Maturin wheel data below `target/private-wheel-data/data/`.
On Linux it builds and stages the private Bubblewrap companion before the runner and embeds a digest of those exact companion bytes in the runner.
Linux builds require a C toolchain, `pkg-config`, and the libcap development files.
The repository records the exact static libcap archive hashes, package revisions, source URLs, and package copyright files supplied by the supported Ubuntu build hosts.
Staging fails if `pkg-config` selects another version or archive, and uses a digest-specific Cargo target directory so a package update cannot reuse a cached companion.
The Linux wheel includes the selected package provenance and its complete distribution copyright file.
It does not download source or select a mutable branch.

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

## Publish a release

Choose a new release version and merge the release metadata with `Cargo.toml` and `Cargo.lock` both at that version, then confirm CI passes on `main`.

A manual run of the Release workflow builds and smoke-tests all three wheels for inspection but does not publish them.

Create the release from a clean, current `main` checkout:

```sh
git switch main
git pull --ff-only
scripts/format
scripts/check

release_version=X.Y.Z
git tag -a "v${release_version}" -m "Release v${release_version}"
git push origin "v${release_version}"
```

The tag-triggered Release workflow verifies that the tag matches `Cargo.toml`, builds all three native wheels, install-tests them with `uv`, and publishes them through PyPI Trusted Publishing.
It checks out the exact sandbox source revision from `sandbox-runner.json` before building each wheel.

## Verify the publication

Use clean `uv` directories when testing the public index:

```sh
cache_dir="$(mktemp -d)"
tool_dir="$(mktemp -d)"
bin_dir="$(mktemp -d)"
release_version=X.Y.Z

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uvx "mcp-console@${release_version}" --version

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uvx "mcp-console@${release_version}" --help

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uv tool install "mcp-console==${release_version}"

"$bin_dir/mcp-console" --version
"$bin_dir/mcp-console" --help
"$bin_dir/mcp-console" sandbox -- /usr/bin/true
test ! -e "$bin_dir/mcp-console-sandbox"
test ! -e "$bin_dir/bwrap"
```

Verify these commands on Apple Silicon macOS, Intel macOS, and x86-64 Linux.
Also start

```sh
uvx "mcp-console@${release_version}" serve
```

through an MCP client.
`serve` waits for protocol input; waiting is not an interactive-command failure.

After exact-version verification, test unqualified resolution in fresh `uv` directories:

```sh
uvx mcp-console --help
uv tool install mcp-console
```

Leave PyPI release `0.0.1` unchanged and unyanked.

## Recover from a failed release

PyPI versions and filenames are immutable.

If publication fails, rerun the failed job from the same workflow run while its original wheel artifacts remain available.
Do not start a fresh build and expect it to replace an uploaded wheel with the same filename.

If `0.0.2` is defective after publication, fix the defect and publish a new version such as `0.0.3`.
Do not move, delete, or reuse `v0.0.2`.
