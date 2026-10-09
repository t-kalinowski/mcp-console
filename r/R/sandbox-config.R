# One internal parent gives the public configuration nodes a shared serializer.
# It has no state, defaults, or policy of its own.
sandbox_node <- S7::new_class(
  "sandbox_node",
  package = "mcp.console",
  abstract = TRUE
)

sandbox_strings <- function(value) {
  if (!is.null(value) && (anyNA(value) || any(!nzchar(value)))) {
    "must contain non-empty strings, without missing values"
  }
}

sandbox_string_list <- function() {
  S7::new_property(NULL | S7::class_character, validator = sandbox_strings)
}

sandbox_choice <- function(choices) {
  force(choices)
  S7::new_property(
    NULL | S7::class_character,
    validator = function(value) {
      if (
        !is.null(value) &&
          (length(value) != 1L || is.na(value) || !value %in% choices)
      ) {
        paste0("must be NULL or one of: ", paste(choices, collapse = ", "))
      }
    }
  )
}

sandbox_flag <- function() {
  S7::new_property(
    NULL | S7::class_logical,
    validator = function(value) {
      if (!is.null(value) && (length(value) != 1L || is.na(value))) {
        "must be NULL, TRUE, or FALSE"
      }
    }
  )
}

#' Sandbox configuration
#'
#' These S7 class constructors represent the public `sandbox:` node in
#' Console's `config.yaml`, not the native runner's complete-policy protocol.
#' Pass a `sandbox_config()` to [console_tool()] or [sandboxed_system2()].
#'
#' `NULL` properties are omitted. A constructed child with no properties is an
#' explicit empty mapping, and `character()` is an explicit empty sequence.
#' This distinction also preserves defaults when the serialized node is used
#' as `resolver.sandbox`. No worker or resolver defaults are baked into R.
#'
#' `as.list()` returns a recursive, JSON-ready mapping. Sequences are unnamed
#' lists, including singletons; empty mappings are named empty lists. For
#' example, `jsonlite::toJSON(list(sandbox = as.list(policy)),
#' auto_unbox = TRUE)` produces an application configuration, not native JSON.
#'
#' @section Policy semantics:
#' Paths are literal and launch-relative. R does not expand `~`, resolve
#' symlinks, create directories, reorder permissions, or interpret globs.
#' Console and its native runner own precedence, defaults, domain validation,
#' and platform support. Unsupported policies fail; there is no retry with
#' weaker permissions. An empty worker policy allows host reads and private
#' temporary writes, but does not grant workspace writes or networking.
#'
#' Domain patterns use Console's native matching. Host ports and URL paths
#' are not supported. Limited proxy mode is not a general read-only network
#' guarantee. Socket and local-binding permissions can allow direct channels.
#' `tcp_udp` is representable but rejected by the currently pinned runner.
#'
#' @param read_only,read_write,deny Character vectors of literal filesystem
#'   paths. For `sandbox_domains()`, `deny` instead contains domain patterns.
#' @return An S7 configuration object. The constructor is also its S7 class.
#' @examples
#' policy <- sandbox_config(
#'   filesystem = sandbox_filesystem(read_write = ".", deny = "./secrets"),
#'   network = sandbox_network(
#'     proxy = sandbox_proxy(domains = sandbox_domains(allow = "api.example.com"))
#'   )
#' )
#' as.list(policy)
#' sandbox_config(network = "restricted")
#' sandbox_config(filesystem = sandbox_filesystem(read_write = character()))
#' @name sandbox_config
#' @export
sandbox_filesystem <- S7::new_class(
  "sandbox_filesystem",
  package = "mcp.console",
  parent = sandbox_node,
  properties = list(
    read_only = sandbox_string_list(),
    read_write = sandbox_string_list(),
    deny = sandbox_string_list()
  )
)

#' @rdname sandbox_config
#' @param allow Character vector of domain patterns allowed by the proxy.
#' @export
sandbox_domains <- S7::new_class(
  "sandbox_domains",
  package = "mcp.console",
  parent = sandbox_node,
  properties = list(allow = sandbox_string_list(), deny = sandbox_string_list())
)

#' @rdname sandbox_config
#' @param mode `NULL`, `"full"`, or `"limited"`. Omission uses Console's default.
#' @param domains `NULL` or a `sandbox_domains()`. An explicit empty object
#'   clears generated domain defaults; `NULL` preserves them.
#' @param socks5 `NULL`, `"disabled"`, `"tcp"`, or `"tcp_udp"`. Any explicit
#'   value is invalid with `mode = "limited"`, including `"disabled"`.
#' @param allow_upstream_proxy `NULL`, `TRUE`, or `FALSE`; controls use of an
#'   upstream proxy from the trusted launch environment.
#' @export
sandbox_proxy <- S7::new_class(
  "sandbox_proxy",
  package = "mcp.console",
  parent = sandbox_node,
  properties = list(
    mode = sandbox_choice(c("full", "limited")),
    domains = NULL | sandbox_domains,
    socks5 = sandbox_choice(c("disabled", "tcp", "tcp_udp")),
    allow_upstream_proxy = sandbox_flag()
  ),
  validator = function(self) {
    if (identical(self@mode, "limited") && !is.null(self@socks5)) {
      "`socks5` must be NULL in limited mode"
    }
  }
)

#' @rdname sandbox_config
#' @param unix_sockets `NULL`, a character vector of literal absolute Unix
#'   socket paths (possibly empty), or the string `"dangerously_allow_all"`.
#'   The CLI validates native path and platform restrictions.
#' @export
sandbox_sockets <- S7::new_class(
  "sandbox_sockets",
  package = "mcp.console",
  parent = sandbox_node,
  properties = list(unix_sockets = sandbox_string_list())
)

#' @rdname sandbox_config
#' @param proxy A `sandbox_proxy()`; defaults to an explicit empty proxy
#'   mapping. A network mapping always enables a managed proxy. Use a scalar
#'   network choice in `sandbox_config()` for proxy-free networking.
#' @param sockets `NULL` or a `sandbox_sockets()`.
#' @param allow_local_binding `NULL`, `TRUE`, or `FALSE`. This does not promise
#'   access from a host browser to a Linux network namespace.
#' @export
sandbox_network <- S7::new_class(
  "sandbox_network",
  package = "mcp.console",
  parent = sandbox_node,
  properties = list(
    proxy = S7::new_property(sandbox_proxy, default = quote(sandbox_proxy())),
    sockets = NULL | sandbox_sockets,
    allow_local_binding = sandbox_flag()
  )
)

#' @rdname sandbox_config
#' @param filesystem `NULL` or a `sandbox_filesystem()`.
#' @param network `NULL`, `"restricted"`, `"enabled"`, or a
#'   `sandbox_network()`. Enabling networking does not disable filesystem
#'   enforcement. `NULL` preserves the context's default networking.
#' @export
sandbox_config <- S7::new_class(
  "sandbox_config",
  package = "mcp.console",
  parent = sandbox_node,
  properties = list(
    filesystem = NULL | sandbox_filesystem,
    network = S7::new_property(
      NULL | S7::class_character | sandbox_network,
      validator = function(value) {
        if (
          is.character(value) &&
            (length(value) != 1L ||
              is.na(value) ||
              !value %in% c("restricted", "enabled"))
        ) {
          "must be NULL, restricted, enabled, or a sandbox_network()"
        }
      }
    )
  )
)

# as.list() is deliberately a wire-shape conversion, not a defaults resolver.
# The native CLI remains the final authority on policy validity.
S7::method(as.list, sandbox_node) <- function(x, ...) {
  S7::validate(x)
  fields <- S7::props(x)
  fields <- fields[!vapply(fields, is.null, logical(1))]
  arrays <- c("read_only", "read_write", "deny", "allow", "unix_sockets")
  for (name in names(fields)) {
    value <- fields[[name]]
    if (S7::S7_inherits(value, sandbox_node)) {
      fields[[name]] <- as.list(value)
    } else if (
      name %in%
        arrays &&
        !(name == "unix_sockets" &&
          identical(unname(value), "dangerously_allow_all"))
    ) {
      fields[[name]] <- unname(as.list(value))
    } else {
      fields[[name]] <- unname(value)
    }
  }
  json_object(fields)
}

check_sandbox_config <- function(sandbox, allow_null = FALSE) {
  if (allow_null && is.null(sandbox)) {
    return(invisible(NULL))
  }
  if (!S7::S7_inherits(sandbox, sandbox_config)) {
    stop("`sandbox` must be a sandbox_config() object.", call. = FALSE)
  }
  S7::validate(sandbox)
  invisible(NULL)
}

sandbox_cli_arguments <- function(sandbox) {
  if (is.null(sandbox)) {
    return(character())
  }
  check_sandbox_config(sandbox)
  value <- as.character(jsonlite::toJSON(as.list(sandbox), auto_unbox = TRUE))
  # Configuration maps merge recursively. Clear this node before assigning it,
  # so an explicitly supplied object replaces, rather than widens, a policy
  # read from global/project configuration. Validation follows all overrides.
  c("-c", "sandbox=null", "-c", paste0("sandbox=", value))
}
