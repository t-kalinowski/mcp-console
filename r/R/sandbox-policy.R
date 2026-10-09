# One internal parent gives the public configuration nodes a shared serializer.
# It has no state, defaults, or policy of its own.
ConfigNode <- S7::new_class(
  "ConfigNode",
  abstract = TRUE
)

config_strings <- function(value) {
  if (!is.null(value) && (anyNA(value) || any(!nzchar(value)))) {
    "must contain non-empty strings, without missing values"
  }
}

config_string_list <- function() {
  S7::new_property(NULL | S7::class_character, validator = config_strings)
}

config_choice <- function(choices) {
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

config_flag <- function() {
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
#' Use a `SandboxPolicy()` in [ConsoleConfig()] or [sandboxed_system2()].
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
#'   paths. For `Domains()`, `deny` instead contains domain patterns.
#' @return An S7 configuration object. The constructor is also its S7 class.
#' @examples
#' policy <- SandboxPolicy(
#'   filesystem = Filesystem(read_write = ".", deny = "./secrets"),
#'   network = Network(
#'     proxy = Proxy(domains = Domains(allow = "api.example.com"))
#'   )
#' )
#' as.list(policy)
#' SandboxPolicy(network = "restricted")
#' SandboxPolicy(filesystem = Filesystem(read_write = character()))
#' @name SandboxPolicy
#' @export
Filesystem <- S7::new_class(
  "Filesystem",
  parent = ConfigNode,
  properties = list(
    read_only = config_string_list(),
    read_write = config_string_list(),
    deny = config_string_list()
  )
)

#' @rdname SandboxPolicy
#' @param allow Character vector of domain patterns allowed by the proxy.
#' @export
Domains <- S7::new_class(
  "Domains",
  parent = ConfigNode,
  properties = list(allow = config_string_list(), deny = config_string_list())
)

#' @rdname SandboxPolicy
#' @param mode `NULL`, `"full"`, or `"limited"`. Omission uses Console's default.
#' @param domains `NULL` or a `Domains()`. An explicit empty object
#'   clears generated domain defaults; `NULL` preserves them.
#' @param socks5 `NULL`, `"disabled"`, `"tcp"`, or `"tcp_udp"`. Any explicit
#'   value is invalid with `mode = "limited"`, including `"disabled"`.
#' @param allow_upstream_proxy `NULL`, `TRUE`, or `FALSE`; controls use of an
#'   upstream proxy from the trusted launch environment.
#' @export
Proxy <- S7::new_class(
  "Proxy",
  parent = ConfigNode,
  properties = list(
    mode = config_choice(c("full", "limited")),
    domains = NULL | Domains,
    socks5 = config_choice(c("disabled", "tcp", "tcp_udp")),
    allow_upstream_proxy = config_flag()
  ),
  validator = function(self) {
    if (identical(self@mode, "limited") && !is.null(self@socks5)) {
      "`socks5` must be NULL in limited mode"
    }
  }
)

#' @rdname SandboxPolicy
#' @param unix_sockets `NULL`, a character vector of literal absolute Unix
#'   socket paths (possibly empty), or the string `"dangerously_allow_all"`.
#'   The CLI validates native path and platform restrictions.
#' @export
Sockets <- S7::new_class(
  "Sockets",
  parent = ConfigNode,
  properties = list(unix_sockets = config_string_list())
)

#' @rdname SandboxPolicy
#' @param proxy A `Proxy()`; defaults to an explicit empty proxy
#'   mapping. A network mapping always enables a managed proxy. Use a scalar
#'   network choice in `SandboxPolicy()` for proxy-free networking.
#' @param sockets `NULL` or a `Sockets()`.
#' @param allow_local_binding `NULL`, `TRUE`, or `FALSE`. This does not promise
#'   access from a host browser to a Linux network namespace.
#' @export
Network <- S7::new_class(
  "Network",
  parent = ConfigNode,
  properties = list(
    proxy = S7::new_property(Proxy, default = quote(Proxy())),
    sockets = NULL | Sockets,
    allow_local_binding = config_flag()
  )
)

#' @rdname SandboxPolicy
#' @param filesystem `NULL` or a `Filesystem()`.
#' @param network `NULL`, `"restricted"`, `"enabled"`, or a
#'   `Network()`. Enabling networking does not disable filesystem
#'   enforcement. `NULL` preserves the context's default networking.
#' @export
SandboxPolicy <- S7::new_class(
  "SandboxPolicy",
  parent = ConfigNode,
  properties = list(
    filesystem = NULL | Filesystem,
    network = S7::new_property(
      NULL | S7::class_character | Network,
      validator = function(value) {
        if (
          is.character(value) &&
            (length(value) != 1L ||
              is.na(value) ||
              !value %in% c("restricted", "enabled"))
        ) {
          "must be NULL, restricted, enabled, or a Network()"
        }
      }
    )
  )
)

# Wire conversion preserves omissions, mappings, and arrays; native validation
# and defaults remain the CLI's responsibility.
config_mapping <- function(fields) {
  fields <- fields[!vapply(fields, is.null, logical(1))]
  arrays <- c(
    "read_only",
    "read_write",
    "deny",
    "allow",
    "unix_sockets",
    "packages",
    "languages"
  )
  for (name in names(fields)) {
    value <- fields[[name]]
    if (S7::S7_inherits(value, ConfigNode)) {
      fields[[name]] <- as.list(value)
    } else if (name == "environment") {
      fields[[name]] <- json_object(as.list(value))
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

S7::method(as.list, ConfigNode) <- function(x, ...) {
  S7::validate(x)
  config_mapping(S7::props(x))
}

check_sandbox_policy <- function(sandbox) {
  if (!S7::S7_inherits(sandbox, SandboxPolicy)) {
    stop("`sandbox` must be a SandboxPolicy() object.", call. = FALSE)
  }
  S7::validate(sandbox)
  invisible(NULL)
}

config_override <- function(key, value) {
  c("-c", paste0(key, "=", jsonlite::toJSON(value, auto_unbox = TRUE)))
}

sandbox_cli_arguments <- function(sandbox) {
  check_sandbox_policy(sandbox)
  c("-c", "sandbox=null", config_override("sandbox", as.list(sandbox)))
}
