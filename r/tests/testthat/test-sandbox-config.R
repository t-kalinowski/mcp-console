policy_json <- function(x) {
  as.character(jsonlite::toJSON(as.list(x), auto_unbox = TRUE))
}

test_that("omitted mappings and explicit empties stay distinct", {
  expect_identical(policy_json(sandbox_config()), "{}")
  expect_identical(
    policy_json(sandbox_config(filesystem = sandbox_filesystem())),
    '{"filesystem":{}}'
  )
  expect_identical(
    policy_json(sandbox_config(
      filesystem = sandbox_filesystem(read_write = character())
    )),
    '{"filesystem":{"read_write":[]}}'
  )
  expect_identical(
    policy_json(sandbox_config(network = sandbox_network())),
    '{"network":{"proxy":{}}}'
  )
  expect_identical(
    policy_json(sandbox_proxy(domains = sandbox_domains())),
    '{"domains":{}}'
  )
  expect_identical(
    policy_json(sandbox_proxy(domains = sandbox_domains(allow = character()))),
    '{"domains":{"allow":[]}}'
  )
})

test_that("all public fields have the config.yaml wire shape", {
  x <- sandbox_config(
    filesystem = sandbox_filesystem(
      read_only = c("./data", "./.claude"),
      read_write = ".",
      deny = "./secrets"
    ),
    network = sandbox_network(
      proxy = sandbox_proxy(
        mode = "full",
        domains = sandbox_domains(
          allow = "*.example.org",
          deny = "bad.example.org"
        ),
        socks5 = "tcp",
        allow_upstream_proxy = FALSE
      ),
      sockets = sandbox_sockets(unix_sockets = "/tmp/a.sock"),
      allow_local_binding = FALSE
    )
  )
  expected <- list(
    filesystem = list(
      read_only = list("./data", "./.claude"),
      read_write = list("."),
      deny = list("./secrets")
    ),
    network = list(
      proxy = list(
        mode = "full",
        domains = list(
          allow = list("*.example.org"),
          deny = list("bad.example.org")
        ),
        socks5 = "tcp",
        allow_upstream_proxy = FALSE
      ),
      sockets = list(unix_sockets = list("/tmp/a.sock")),
      allow_local_binding = FALSE
    )
  )
  expect_identical(as.list(x), expected)
  expect_identical(
    jsonlite::fromJSON(policy_json(x), simplifyVector = FALSE),
    expected
  )
  expect_true(S7::S7_inherits(x, sandbox_config))
})

test_that("scalars, lists, and special socket selectors are not conflated", {
  for (network in c("restricted", "enabled")) {
    expect_identical(
      as.list(sandbox_config(network = network)),
      list(network = network)
    )
  }
  expect_identical(
    policy_json(sandbox_sockets(unix_sockets = character())),
    '{"unix_sockets":[]}'
  )
  expect_identical(
    policy_json(sandbox_sockets(unix_sockets = "dangerously_allow_all")),
    '{"unix_sockets":"dangerously_allow_all"}'
  )
  paths <- c(
    named = "./café 雪",
    "../not-created",
    "~/literal",
    "$HOME/literal"
  )
  expect_identical(
    as.list(sandbox_filesystem(read_write = paths))$read_write,
    unname(as.list(paths))
  )
})

test_that("S7 validates both construction and property updates", {
  expect_error(sandbox_filesystem(read_write = NA_character_), "read_write")
  expect_error(sandbox_filesystem(read_write = ""), "read_write")
  expect_error(sandbox_filesystem(read_write = TRUE), "read_write")
  expect_error(sandbox_config(filesystem = list()), "filesystem")
  expect_error(sandbox_config(network = FALSE), "network")
  expect_error(sandbox_config(network = character()), "network")
  expect_error(sandbox_config(network = "enable"), "network")
  expect_error(sandbox_network(proxy = NULL), "proxy")
  expect_error(
    sandbox_network(allow_local_binding = c(TRUE, FALSE)),
    "allow_local_binding"
  )
  expect_error(sandbox_proxy(mode = "limited", socks5 = "disabled"), "socks5")
  expect_error(sandbox_config(unknown = TRUE), "unused argument")
  x <- sandbox_proxy(mode = "full", socks5 = "tcp")
  expect_error(x@mode <- "limited", "socks5")
  expect_error(x@socks5 <- "udp", "socks5")
})

test_that("exported class objects have value semantics and retain context defaults", {
  x <- sandbox_config(network = "restricted")
  y <- x
  y@network <- "enabled"
  expect_identical(x@network, "restricted")
  expect_identical(y@network, "enabled")
  # Exactly the same node can be embedded under resolver.sandbox by a caller.
  json <- jsonlite::toJSON(
    list(resolver = list(sandbox = as.list(sandbox_config()))),
    auto_unbox = TRUE
  )
  expect_identical(as.character(json), '{"resolver":{"sandbox":{}}}')
})

test_that("invalid public launch options fail before executable resolution", {
  expect_error(
    console_tool(sandbox = list(), path = "missing"),
    "sandbox_config"
  )
  expect_error(
    console_tool(sandbox = sandbox_config(), no_sandbox = TRUE),
    "cannot be combined"
  )
  expect_error(
    sandboxed_system2("echo", sandbox = NULL, path = "missing"),
    "sandbox_config"
  )
  expect_error(
    sandboxed_system2("echo", sandbox = FALSE, path = "missing"),
    "sandbox_config"
  )
  expect_error(
    sandboxed_system2("echo", path = "missing", version = "0.0.2"),
    "Only one"
  )
  expect_error(
    sandboxed_system2("echo", typo = 1, path = "missing"),
    "must be empty"
  )
})
