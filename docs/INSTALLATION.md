# Installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) to run Console with `uvx`, without a separate Console installation.
The [R](R.md) and [Python](PYTHON.md) guides also cover installing their client packages.

## Supported platforms

Prebuilt packages are available for macOS and Linux on ARM64 and x86-64, and for Windows x64.
Sandboxed Windows sessions require Windows 11 with [Microsoft Execution Containers (MXC)](https://support.microsoft.com/en-us/servicing/os/windows-11/2026/08/kb5120998-windows-11-24h2-25h2-update#microsoft-execution-containers).

If Console fails to start on Linux, see [Linux host compatibility](LINUX_COMPATIBILITY.md) for missing libraries or sandbox restrictions.

## R, Python, and SQL

To let your agent use R, install it separately, for example with [rig](https://github.com/r-lib/rig#id-installation) and `rig add release`.
Python and SQL work without R.
The first session may download Python and packages.

## Check your installation

To check your installation and prepare runtime and package caches before opening a client, run `uvx mcp-console warmup`.
See [Check your installation](CLI.md#check-your-installation) for configuration and diagnostics.

## Build from source

To install the development version, follow [Build from source](BUILD_FROM_SOURCE.md).

Continue with [Get started](getting-started.qmd) to choose a connection method and try an analysis.
