"""Small local gate over existing boundary cases and their declared executions.

Keep exact case selectors: adding a case to a suite must not grow this profile.
Full and explicitly selected runs do not use this list.
"""

SMOKE = (
    # MCP and CLI admission, including the canonical tools/list snapshot.
    "client_server/server/test_tools::initializes_and_lists_tools",
    "client_server/server/test_tools::invalid_send_has_no_external_effects",
    "cli/interface/test_help::help",
    "cli/test_config_overrides::layers_project_then_cli_in_order",
    # Real runtimes, persistent state, and mixed-language recording with a plot.
    "client_server/r/test_runtime::applies_complete_expressions_before_incomplete_source",
    "client_server/python/test_runtime::evaluates_cells_in_persistent_reticulate_state",
    "client_server/sql/test_catalog::evaluates_queries_in_a_persistent_catalog",
    "client_server/recording/test_markdown::records_real_mixed_language_session",
    # Interactive input and interruption, in direct and sandboxed execution.
    "client_server/r/test_stdin::routes_idle_and_timed_out_stdin",
    "client_server/lifecycle/test_interrupts::interrupts_running_worker_with_sigint",
    # Native filesystem and network policy through the standalone CLI.
    "cli/sandbox/test_execution::enforces_host_read_only_and_temporary_writes",
    "cli/sandbox/test_execution::denies_network_access",
)
