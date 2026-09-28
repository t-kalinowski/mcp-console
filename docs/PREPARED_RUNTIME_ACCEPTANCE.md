# Prepared runtime acceptance audit

This audit covers the shared prepared-runtime change for Docker and Docker Sandbox (SBX), with local and SSH regressions through the public Console interfaces.
The provider fixtures contain installed compatible Linux builds, not controller executables or mocked workers.
R-free fixtures come from `examples/docker/Dockerfile.python` and `examples/docker-sandbox/Dockerfile.python`; their selected `/opt/console-python` interpreters and embedding libraries do not exist on the controller.
Mixed-language fixtures retain R and reticulate.

The shared `client_server/python/test_prepared_without_r` suite checks both real providers: SQL-first startup, persistent Python objects and catalogs, custom DB-API switching, plotting, input, interrupts, debugger continuation, child interpreters, recordings, restart, crash replacement, attachment loss, input closure, startup failures, probe cancellation, and confirmed removal.
It also checks target-relative and bare interpreter selection, disabled environment inheritance, conflicting layout overrides, legacy selection, PATH `python` when `python3` is absent, invalid selected runtimes, missing embedding libraries, minimal environments, early requirement-mutation rejection, and missing imports without resolver invocation.
Controller R/Python/uv/ir commands and target uv/ir commands are sentinels; the SBX fixture also rejects native-companion invocation.
Cached `fts` loads after actual repository network access is blocked, with automatic extension installation disabled.
SQL spill and secret directory settings stay beneath private disposable worker storage.
Every case checks authoritative absence of its owned containers or VMs after retirement and preserves shared files.

Protocol compatibility cases in `client_server/server/test_prepared_protocol` cover the independent prepared-target version 4 handoff, bounded frames, incompatible builds, unsupported native payloads, and retirement barriers.
Those fake CLI cases are transport regression evidence only.
They do not establish provider acceptance.
SSH retains its existing version 4 negotiation and managed preparation contract.

## Reproduce the provider audit

Build and import the R-free fixtures using the [Docker](DOCKER.md#prepared-python-without-r) and [SBX](DOCKER_SANDBOX.md#prepared-python-without-r) recipes.
Build the existing mixed-language examples with the same application build.
Select all four explicitly:

```sh
export MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE=my-console:python
export MCP_CONSOLE_TEST_SBX_PYTHON_TEMPLATE="$(cat console-python-reference.txt)"
export MCP_CONSOLE_TEST_DOCKER_IMAGE=my-console:analysis
export MCP_CONSOLE_TEST_SBX_TEMPLATE="$(cat console-template-reference.txt)"
scripts/test --jobs 2 client_server/python/test_prepared_without_r \
  client_server/server/test_prepared_protocol \
  client_server/server/test_docker \
  client_server/server/test_docker_setup \
  client_server/server/test_docker_sandbox_runtime
scripts/check
scripts/check --full
```

`DOCKER_CONTEXT` selects the audit daemon when the caller uses a non-default context.
Real external SSH sans-R coverage requires `MCP_CONSOLE_TEST_SSH_NO_R_EXTERNAL`, containing the existing test fixture's target and OpenSSH configuration; ordinary external SSH discovery remains in `tests/support/ssh_external.py`.
The full gate includes capability-applicable local/direct/native, loopback and external SSH, provider, Python SDK integration, and installation checks.
Run the R package's integration suite against the checkout executable separately with `MCP_CONSOLE_TEST_BINARY` set.

## Provider limits

The macOS audit uses Docker Engine in Colima and standalone SBX compute enforcement.
Ordinary Docker namespace restrictions can reject an inner native sandbox; the applicable public case checks that rejection, while the R-free lifecycle case exercises the installed native runner with `filesystem: external-sandbox`.
That mode retains Docker's documented filesystem and network isolation limits.
It does not establish nested restricted-namespace support.
SBX never discovers or launches the native companion.

Provider network-policy inheritance and inner-Docker acceptance require their existing optional capabilities and flags.
A skip supplies no evidence for that capability.
Linux SBX controller acceptance requires a supported SBX/KVM host and is not inferred from Linux workers running in macOS-owned VMs.
No global policy, credentials, user resource, mount, or preinstalled cache is removed by the audit.
Prepared targets remain non-managed; resolver trust limitations and local/SSH dependency semantics are unchanged.
