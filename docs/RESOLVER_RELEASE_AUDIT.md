# Resolver release trust audit

This audit evaluates Console base `28b9ee4aa23037416a1f565c8e7d53fbcbb1a6ac` with the pinned companion `85d407d8a4544ed0916aff3c7a273461e739f215`.
The accompanying public acceptance cases add evidence without changing production permissions or resolver selection.
Results apply to the described policies and probes; they are not a claim that arbitrary package code is safe.

## Authority under test

The server captures configuration and owns accepted requirements.
Its retained preparation process runs discovery, resolution, Python inspection, builds, and extension installation inside a separate native sandbox on macOS/Linux.
Workers can request named dependencies but cannot supply a resolver program through a cell or stdin.
Selected wrappers, configuration files, package sources, and granted caches remain mutable trusted inputs.
If a worker is explicitly permitted to replace them, later preparation can execute that code with the resolver's permissions.
It must not acquire additional controller permissions.

The default resolver permits host reads, private temporary/cache writes, and approved downloads.
Readable secrets, installation effects within granted caches, and later execution of shared host-cache artifacts are therefore outside an isolation claim.
Configuration capture preserves values and policy, not file contents.

## Supported behavior and evidence

| Behavior                                                  | Contract                                                                                      | Public evidence                                                                                                                                                                                                                                                          |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Cell changes to uv values and Console configuration       | Later preparation retains startup values and policy.                                          | Existing `captures_user_uv_configuration` and `retains_tailored_resolver_policy`; replacement-wrapper probes check the captured timeout and absence of worker `UV_OFFLINE`.                                                                                              |
| Replacement of selected uv/ir wrappers                    | Execution remains inside resolver permissions.                                                | `test_trust_boundary`: workers replace the selected path after initialization; real uv/ir preparation succeeds after forbidden direct/aliased writes and an unapproved download are rejected.                                                                            |
| Mutable uv configuration and local package wheel          | File contents may change package selection; package startup remains contained.                | A worker writes a wheel with a `.pth` startup hook and rewrites the captured configuration's `find-links`; preparation runs the hook and the worker imports the resulting package.                                                                                       |
| Source package build backend                              | Build code uses resolver permissions.                                                         | A worker writes a named source distribution with a PEP 517 backend; real uv builds its wheel, the backend runs the permission probe, and the prepared module returns its expected value.                                                                                 |
| Poisoned Python cache with an explicit worker write grant | Cached startup code may run during preparation but cannot gain wider permissions.             | A worker writes `sitecustomize.py` into its Console-managed Python installation and an escape symlink beneath the writable cache; subsequent preparation rejects writes through that symlink.                                                                            |
| Worker-owned R startup profile                            | Preparation does not source the project profile.                                              | Replacement-ir case writes a failing project `.Rprofile`; later real CRAN preparation succeeds from its private working directory. Existing local-R-source rejection remains applicable.                                                                                 |
| Native target startup and filesystem aliases              | Native restrictions apply before target constructors; configured denials survive aliases.     | `test_native_startup`: a compiled constructor attempts a forbidden write before `main`; explicit system reads and an executable alias still permit launch; a denied file remains unreadable through a symlink. Linux exercises `/bin`, `/lib`, and `/lib64` read grants. |
| Controller/result handoff and failure                     | Results remain descriptor-bound; failed or cancelled preparation does not commit candidates.  | Existing `preparation_pins_result_files`, failed-preparation/input rollback, preparation EOF, and resolver-I/O retirement cases.                                                                                                                                         |
| Default and custom caches, R/Python/DuckDB                | Cache selection remains captured; companion staging and cache-root metadata remain protected. | Existing resolver-cache and cache-location cases, including real extension installation/loading, cold R preparation, companion-cache denial, and metadata denial.                                                                                                        |

The probes use owned temporary files outside macOS's writable user-temporary directory.
They require actual filesystem denial and proxy rejection, successful permitted preparation, and a receipt written by resolver-side code.
A marker in the environment alone is not evidence of containment.
The poisoned-cache case grants worker writes explicitly; the default worker policy does not grant writes to Console caches.

## Unsupported claims and historical concerns

| Scenario                                                                                               | Boundary                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Windows preparation or `serve --no-sandbox`                                                            | Runs with host permissions. Explicit Windows resolver sandbox requests and Console caches with unsandboxed execution are rejected.                                                                                                                                                                                                         |
| Untrusted requirements, host-readable secrets, arbitrary installation effects, or host-cache consumers | Package installation is intentionally trusted. The resolver policy limits authority; it does not validate package code or make shared artifacts safe for unrelated host processes.                                                                                                                                                         |
| Concurrent replacement of a selected interpreter during initialization                                 | The selected installation is trusted and must remain stable; descriptor-bound result files do not freeze its executable or shared libraries.                                                                                                                                                                                               |
| Runner death, stopped runners, or unobserved escaped descendants on macOS                              | The documented native lifetime limits apply. Successful operation completion and native retirement are distinct from recovery after runner loss.                                                                                                                                                                                           |
| Historical companion external-mode startup gate                                                        | The companion's external mode delegates enforcement and may start directly. Console's public filesystem mapping emits a restricted native policy; `rejects_native_external_resolver_mode` confirms that the public configuration rejects the native selector. The old companion PR is not a prerequisite for this supported resolver path. |

No new protection is justified by the local cases: the tested mutable inputs influence preparation within its documented permissions.
There is no silent transition to unsandboxed preparation in those cases.
A demonstrated violation of the supported boundary requires a regression and repair; listing a case as unsupported must not conceal such a violation.

## Validation and release decision

The nine new cases pass on macOS with the pinned companion.
The focused run passed 31 cases in 35 executions, covering eight new cases and existing permission, configuration, result-handoff, R/Python/DuckDB, cache, rollback, and retirement contracts.
The ninth case separately confirms rejection of the native external-mode selector.
The ordinary `scripts/check` gate also passed: source and architecture checks, Rust formatting, Clippy, Rust tests, and smoke acceptance.
Development records `20261007-221107-e0bzh02q` and `20261007-221431-ms2h_s31` retain the focused and ordinary gate evidence.
These local results do not establish Linux or Windows acceptance or CI success on the final PR head.

At the pinned companion's [CI run](https://github.com/t-kalinowski/cobox/actions/runs/37522396814), Linux executable contracts, native sandbox tests, and release-artifact contracts passed.
The subsequent native lint step failed on an unfulfilled `clippy::zombie_processes` expectation.
That lint failure is not an observed containment failure, and the job as a whole did not pass.

**Release decision: NO-GO pending passing Linux integration acceptance and the final release candidate's CI.** The local audit identifies no demonstrated breach requiring a production change or restoration of the historical companion dependency.
The decision can become GO for this bounded trust contract once the new and reused cases pass on the supported sandbox platforms at the final candidate and its verified companion pin.
This audit does not approve publication, bypass other release gates, or establish guarantees excluded above.
