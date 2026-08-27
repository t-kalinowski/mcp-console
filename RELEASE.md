# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI
wheels. The initial release publishes native Apple Silicon and Intel macOS
wheels. It does not publish a source distribution, Linux or Windows wheels, or
GitHub release archives.

`Cargo.toml` is the package-version source of truth. Keep the root
`mcp-console` entry in `Cargo.lock` synchronized with it.

## One-time PyPI setup

Before the first release:

1. Create a GitHub Actions environment named `pypi`.
2. In the existing PyPI project `mcp-console`, add a GitHub Trusted Publisher
   with:
   - owner `t-kalinowski`;
   - repository `mcp-console`;
   - workflow `release.yml`; and
   - environment `pypi`.
3. Do not add a `PYPI_TOKEN` repository secret.

The publication job is the only job granted an OpenID Connect token.

## Publish 0.0.2

Merge the release metadata with `Cargo.toml` and `Cargo.lock` both at `0.0.2`,
then confirm CI passes on `main`.

A manual run of the Release workflow builds and smoke-tests both wheels for
inspection but does not publish them.

Create the release from a clean, current `main` checkout:

```sh
git switch main
git pull --ff-only
scripts/format
scripts/check

git tag -a v0.0.2 -m "Release v0.0.2"
git push origin v0.0.2
```

The tag-triggered Release workflow verifies that the tag matches
`Cargo.toml`, builds both native wheels, install-tests them with `uv`, and
publishes them through PyPI Trusted Publishing.

## Verify the publication

Use clean `uv` directories when testing the public index:

```sh
cache_dir="$(mktemp -d)"
tool_dir="$(mktemp -d)"
bin_dir="$(mktemp -d)"

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uvx mcp-console@0.0.2 --version

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uvx mcp-console@0.0.2 --help

UV_CACHE_DIR="$cache_dir" \
UV_TOOL_DIR="$tool_dir" \
UV_TOOL_BIN_DIR="$bin_dir" \
  uv tool install 'mcp-console==0.0.2'

"$bin_dir/mcp-console" --version
"$bin_dir/mcp-console" --help
"$bin_dir/mcp-console" sandbox -- /usr/bin/true
```

Verify these commands on both Apple Silicon and Intel macOS. Also start

```sh
uvx mcp-console@0.0.2 serve
```

through an MCP client. `serve` waits for protocol input; waiting is not an
interactive-command failure.

After exact-version verification, test unqualified resolution in fresh `uv`
directories:

```sh
uvx mcp-console --help
uv tool install mcp-console
```

Leave PyPI release `0.0.1` unchanged and unyanked.

## Recover from a failed release

PyPI versions and filenames are immutable.

If publication fails, rerun the failed job from the same workflow run while its
original wheel artifacts remain available. Do not start a fresh build and
expect it to replace an uploaded wheel with the same filename.

If `0.0.2` is defective after publication, fix the defect and publish a new
version such as `0.0.3`. Do not move, delete, or reuse `v0.0.2`.
