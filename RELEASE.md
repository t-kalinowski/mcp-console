# Releasing MCP Console

MCP Console releases are built from tags and published as binary-only PyPI wheels.
The release workflow publishes native Apple Silicon and Intel macOS wheels.
It does not publish a source distribution, Linux or Windows wheels, or GitHub release archives.

`Cargo.toml` is the package-version source of truth.
Keep the root `mcp-console` entry in `Cargo.lock` synchronized with it.

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

Wait for both native wheel builds and smoke tests to pass.
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
It builds both native wheels, install-tests them with `uv`, checks that the artifact set contains exactly those two wheels, and publishes through PyPI Trusted Publishing.

## Verify the publication

Read `https://pypi.org/pypi/mcp-console/X.Y.Z/json` for the published version.
Confirm that both expected macOS wheels are present and unyanked, and compare their SHA-256 digests with the artifacts from the tag-triggered workflow.

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
