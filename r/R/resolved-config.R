#' Inspect and edit resolved Console configuration
#'
#' `resolve_console_config()` asks the selected Console executable to capture
#' configuration and generate its complete tool presentation. It does not start
#' interpreters, install packages, or create a session. It returns an ordinary
#' [ConsoleConfig()] with populated S7 properties and nested configuration objects.
#' [print()] shows the complete configuration, including tool descriptions.
#'
#' Pass the result to [console_tool()] to use the settings without discovering
#' configuration files again. Executable and project choices remain arguments
#' to each call; use the same project so relative permission paths keep their meaning.
#'
#' Tool descriptions and parameter descriptions are literal strings. Setting a
#' parameter's `enabled` to `FALSE` hides it without rewriting the materialized
#' text. The parameter's `schema` contains read-only types and constraints;
#' nested fields live in `parameters`. Use `enabled = FALSE` to hide a field;
#' setting a property to `NULL` restores that setting's native default.
#'
#' Runtime-dependent choices remain policies: interpreter discovery, inherited
#' process environment, automatic package lists, and runtime-created cache and
#' temporary paths are resolved at launch. This object is not a dependency lock.
#'
#' @param config A [ConsoleConfig()] supplying settings and optional file discovery.
#' @inheritParams console_tool
#' @return A [ConsoleConfig()] with `discovery = NULL`. [as.list()] returns its
#'   JSON-ready application settings. The object's `sandbox` property retains
#'   `FALSE` when host execution was requested.
#' @examples
#' \dontrun{
#' config <- resolve_console_config(ConsoleConfig(languages = "r"))
#' print(config)
#' config@tool@description <- "Run one R cell in a persistent workbench."
#' config@tool@parameters$timeout_ms@enabled <- FALSE
#' send <- console_tool(config = config)
#' }
#' @export
resolve_console_config <- function(
  config = ConsoleConfig(),
  ...,
  path = NULL,
  version = NULL,
  project = getwd()
) {
  stopifnot(
    ...length() == 0L,
    S7::S7_inherits(config, ConsoleConfig),
    is.character(project),
    length(project) == 1L,
    !is.na(project),
    dir.exists(project)
  )
  S7::validate(config)
  binary <- resolve_mcp_console(path, version)
  project <- normalizePath(project, mustWork = TRUE)
  result <- processx::run(
    binary,
    c("config", "resolve", "--format", "json", console_cli_arguments(config)),
    wd = project,
    error_on_status = FALSE
  )
  if (result$status != 0L) {
    stop(result$stderr, call. = FALSE)
  }
  settings <- jsonlite::fromJSON(result$stdout, simplifyVector = FALSE)
  if (identical(config@sandbox, FALSE)) {
    settings$sandbox <- FALSE
  }
  config_from_json(settings)
}

# Native capture validates the document before this conversion. Constructors
# share the same properties and serialization as hand-built configuration.
config_node_from_json <- function(class, fields, children = list()) {
  if (is.null(fields)) {
    return(NULL)
  }
  for (name in intersect(names(fields), names(children))) {
    fields[name] <- list(children[[name]](fields[[name]]))
  }
  for (name in intersect(
    names(fields),
    c(
      "read_only",
      "read_write",
      "deny",
      "allow",
      "unix_sockets",
      "packages",
      "languages"
    )
  )) {
    if (!is.null(fields[[name]])) {
      fields[[name]] <- as.character(unlist(fields[[name]], use.names = FALSE))
    }
  }
  if (!is.null(fields$environment)) {
    fields$environment <- structure(
      as.character(unlist(fields$environment, use.names = FALSE)),
      names = names(fields$environment)
    )
  }
  do.call(class, fields)
}

config_python_from_json <- function(value) {
  if (is.null(value)) {
    return(NULL)
  }
  if (!is.null(value$existing)) {
    return(ExistingPython(value$existing))
  }
  if (!is.null(value$managed)) {
    return(config_node_from_json(ManagedPython, value$managed))
  }
  FirstAvailablePython(lapply(value$first_available, function(candidate) {
    if (identical(candidate, "active_venv")) {
      candidate
    } else {
      config_python_from_json(candidate)
    }
  }))
}

config_from_json <- function(settings) {
  parameter <- function(fields) {
    config_node_from_json(
      ToolParameterConfig,
      fields,
      list(
        parameters = function(fields) lapply(fields, parameter)
      )
    )
  }
  policy <- function(fields) {
    if (identical(fields, FALSE)) {
      return(FALSE)
    }
    config_node_from_json(
      SandboxPolicy,
      fields,
      list(
        filesystem = function(fields) {
          config_node_from_json(SandboxFilesystem, fields)
        },
        network = function(fields) {
          if (is.character(fields)) {
            return(fields)
          }
          config_node_from_json(
            SandboxNetwork,
            fields,
            list(
              proxy = function(fields) {
                config_node_from_json(
                  SandboxProxy,
                  fields,
                  list(
                    domains = function(fields) {
                      config_node_from_json(SandboxDomains, fields)
                    }
                  )
                )
              },
              sockets = function(fields) {
                config_node_from_json(SandboxSockets, fields)
              }
            )
          )
        }
      )
    )
  }
  config_node_from_json(
    ConsoleConfig,
    settings,
    list(
      r = function(fields) config_node_from_json(RConfig, fields),
      python = config_python_from_json,
      startup = function(fields) config_node_from_json(StartupConfig, fields),
      sandbox = policy,
      resolver = function(fields) {
        config_node_from_json(ResolverConfig, fields, list(sandbox = policy))
      },
      tool = function(fields) {
        config_node_from_json(
          ToolConfig,
          fields,
          list(
            parameters = function(fields) lapply(fields, parameter)
          )
        )
      }
    )
  )
}
