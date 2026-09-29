# Prepared runtime acceptance audit

This records the version 4 audit below; the current version 5 handoff is documented in [the target launch envelope](RELAY_PROTOCOL.md#target-launch-envelope).

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

## Recorded results

The final audit ran on 2026-09-27 on macOS 26.7 arm64 (build 25G229), with Docker Engine 29.5.2 in Colima and standalone SBX client/server v0.45.1.
Application and execution fixtures use Console 0.0.4 and prepared protocol 4.
The clean implementation revision is `6828509ce498ccaf48b52e734d45af7c64ed21d9`; the following audit documentation changes no production code.

| Command                                      | Result and retained record                                                                                                                                                                                                                                       |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scripts/check`                              | Passed; standard core and smoke profile. `.dev-workflow/runs/20260927-233035-pypg2ewl/result.json`.                                                                                                                                                              |
| `scripts/check --full`                       | Passed in 984.6 seconds: extracted runtime syntax, architecture, formatting, Clippy, 101 repository-tool tests, 46 Rust tests, 1,262 transcript executions, and both source/wheel installation tests. `.dev-workflow/runs/20260927-233257-wftkxo2i/result.json`. |
| Full transcript child, `scripts/test --full` | Passed all 1,262 applicable executions; 23 declared skips below. `.dev-workflow/runs/20260927-233551-8hesc2rz/result.json` and `case-timings.jsonl`.                                                                                                             |
| R adapter integration                        | Passed both cases, 19 assertions, using the checkout executable and `pkgload::load_all("r")` followed by `testthat::test_dir("r/tests/testthat", stop_on_failure = TRUE)`. An existing ellmer import warning remains.                                            |
| Focused probe/protocol verification          | Public empty-selection regression failed before the diagnostic fix (`20260927-232517-va6fpki0`); protocol cases plus real Docker setup rejection passed after the fix (`20260927-232939-3apied8l`).                                                              |

The full gate's installation phase runs `python3 tests/install.py`, including editable and ordinary uv source installations, native compiler-flag rebuilds, source archives, relocated wheel smoke, SDK extras, Python client smoke, and native bundle acceptance on three installed executable paths.
Its Linux-only bundled-helper-selection case is skipped on each macOS installation; the applicable bundle checks pass.
The standard gate alone was not used as completion evidence.

The separate R integration invocation was:

```sh
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" \
  scripts/with-checkout Rscript --vanilla /tmp/mcp-console-prepared-R-integration.R
```

That script loads `testthat`, `pkgload`, and `ellmer`, loads this checkout's `r` package, and runs its entire `r/tests/testthat` directory with `stop_on_failure = TRUE`.
The provider selection variables in the reproduction recipe were set throughout the full gate, with `DOCKER_CONTEXT=colima` and a real installed R-free Linux OpenSSH fixture in `MCP_CONSOLE_TEST_SSH_NO_R_EXTERNAL`.
The ordinary external SSH fixture used the reachable Linux host through `tests/support/ssh_external.py` and installed the current checkout there.

| Execution target                | Selection and preparation                                                                                                                                        | Execution, recording, replacement, and retirement                                                                                                                                                                                               |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Local direct/native sandbox     | Existing managed uv and explicitly selected CPython policies; R-present managed behavior unchanged.                                                              | 88 public MCP sans-R executions passed (44 in each mode), plus the applicable mixed-language, SQL, recording, and lifecycle suites.                                                                                                             |
| SSH loopback and external Linux | Remote managed and explicit selections, remote preparation and activation, existing version/build checks.                                                        | All 17 sans-R executions passed, including real external direct/native execution; R-present external policy, loopback runtime, recordings, replacement, and retirement passed.                                                                  |
| Real Docker direct              | Preinstalled selection only; image/workload PATH, target-relative/bare Python, legacy selection, disabled inheritance, invalid selections, minimal dependencies. | Eleven R-free direct cases and R-present image/lifecycle/setup regressions passed; cached extension loading without network, controller artifacts, fresh restart/crash state, attachment loss, cancellation, and authoritative removal checked. |
| Real Docker native runner       | Installed compatible companion with `filesystem: external-sandbox`.                                                                                              | The additional R-free lifecycle case passed; ordinary restricted namespace and proxy setup rejected as expected on this daemon. No nested restricted-namespace success is claimed.                                                              |
| Real SBX compute                | Same shared prepared selection/configuration, with no native companion or preparation.                                                                           | Eleven R-free cases and R-present runtime/lifecycle cases passed, including controller/attachment loss, input EOF, cancellation, startup failure, recordings, fresh replacement, and authoritative VM absence.                                  |
| Fake protocol peers             | Malformed, incompatible, oversized, missing, contradictory, and unsupported runtime results.                                                                     | Three public MCP cases passed, including 16 rejection modes. These establish transport/admission behavior only.                                                                                                                                 |

R-free examples were rebuilt after the final probe-diagnostic fix and imported again for the final run.
R-present fixtures retain their analysis environment and receive the same wheel-installed Linux application and, for Docker, its complete native bundle.
All fixture build, export, qualification, and template-load commands completed successfully.
Captured identities were:

```text
Docker R-free: sha256:a57a8ebba5316057fafb201e244729a0c9b25a3d3684e2570e7b24c323f22a53
Docker R-present: sha256:2d3b622579d118dfb7c153ff17e5306ed6ca7f0a3fc80f5513c562d2c6df7c33
SBX R-free: docker.io/library/mcp-console-prepared-sbx@sha256:907d42813457c13a88f44d4c231603b3a95e3a2a7d421cbff0bd0c7bea71307d
SBX R-present: docker.io/library/mcp-console-prepared-sbx@sha256:eed3c3dbf6861d76c199ac6b5c37f278781f6e332b4a04bca258df525753f06c
SSH R-free host image: sha256:c4c591d1d5defb4fe5e076458bc67fab4771a3be4b2254be1970f1cd4ad6f1b7
```

Earlier audit failures were repaired before the final run: Clippy rejected a redundant probe-selector argument; R integration teardown passed an empty list to `Sys.setenv`; Docker workload-isolation fixtures hid the caller's non-default CLI context when changing `HOME`; and failed probe diagnostics depended on whether EOF or child exit was observed first.
The fixes use one launch-operation selector, restore only set R variables, retain caller Docker configuration, and let an empty failed probe stream report its exit while still rejecting a successful probe without a structured result.
The first two full transcript attempts stopped at the Docker context fixture (`20260927-224945-0ybi3rc2`) and the probe diagnostic snapshot (`20260927-231017-oemgt2cn`); their remaining in-flight cases were cancelled and installation did not run.
Neither failed attempt is reported as full acceptance.
A valid reset mutation now shares the early rejection assertion with add/set, including restart, code, and input; all three real lifecycle variants passed after regeneration.

The 23 full-profile skips comprise 17 Linux-only namespace, Landlock, procfs, ELF/seccomp, or filename cases:

```text
cli/sandbox/test_configuration::explicit_landlock_preserves_policy_and_rejects_supervised_lifetime
cli/sandbox/test_configuration::supervised_linux_accepts_full_write_policies
cli/sandbox/test_configuration::landlock_rejects_incompatible_options_before_native_setup
cli/sandbox/test_configuration::landlock_requires_truncate_capability_before_target_execution
cli/sandbox/test_configuration::landlock_rejects_policies_requiring_direct_enforcement
cli/sandbox/test_configuration::bubblewrap_enforces_root_write_carveouts
cli/sandbox/test_linux::isolates_files_network_and_processes
cli/sandbox/test_linux::retires_descendants_after_exit_and_supervisor_loss
cli/sandbox/test_linux::relays_signal_and_preserves_target_status
cli/sandbox/test_procfs::fresh_procfs_preserves_policy_and_supervisor_isolation
cli/sandbox/test_procfs::inherited_procfs_preserves_policy_and_supervisor_isolation
cli/sandbox/test_writable_roots::reports_linux_runner_file_root_limitation
client_server/lifecycle/test_descriptors::sanitizes_descriptors_without_close_range_cloexec
client_server/r/test_loader::loads_native_libraries_from_selected_r_home
client_server/r/test_loader::preserves_non_utf8_r_home
client_server/sandbox/test_ragnar::creates_ragnar_store_after_workspace_write_denial_on_linux
client_server/sandbox/test_writable_roots::writable_roots_reach_every_runner_launch_on_linux
```

Two unsupported-platform negative cases are inapplicable because this host supports workers and the sandbox: `cli/interface/test_unsupported::reports_that_the_sandbox_is_unsupported` and `client_server/protocol/test_unsupported::keeps_the_public_interface_without_starting_workers`.
`client_server/r/test_lifecycle::restarts_after_r_worker_segfault[direct]` and `[sandbox]` require `SEGV_MAPERR`; ARM macOS reports `SEGV_ACCERR` for the asserted null fault.
`client_server/server/test_docker_sandbox_lifecycle::restart_retires_independently_started_process_and_container` requires the optional inner-Docker fixture and existing permission to pull busybox.
`client_server/server/test_docker_sandbox_policy::inherited_allow_and_owned_rules_preserve_global_policy` requires an existing inherited policy allowing the specified package registries; the audit leaves global policy unchanged.
Neither optional provider capability is claimed as accepted.

Production review volume against `origin/main` (`fc2def98ab8e2713d4caf2718ab01c0145dcdb57`) is 703 additions plus 124 deletions, **827 lines in 18 files**, after staging all intended new files.
No code was moved outside that count.

After the final gate, authoritative Docker and SBX listings confirmed that all test-owned containers and VMs were absent.
The separate R-free SSH host fixture was forcibly removed and its exact container identity was confirmed absent.
Unrelated containers, the existing stopped VM, user mounts, and preinstalled caches were preserved.
