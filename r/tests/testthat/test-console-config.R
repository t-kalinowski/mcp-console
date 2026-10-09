test_that("ConsoleConfig preserves schema shapes and separates launch choices", {
  config <- ConsoleConfig(
    discovery = ConfigDiscovery(global = FALSE),
    r = RConfig(
      executable = "/opt/R/bin/R",
      packages = character(),
      resolution = "startup_only",
      vanilla = TRUE
    ),
    python = ManagedPython(
      version = "3.13",
      packages = "pandas",
      resolution = "explicit"
    ),
    resolver = ResolverConfig(
      environment = c(INDEX = "", LABEL = "resolver"),
      sandbox = SandboxPolicy(network = "enabled")
    ),
    sandbox = SandboxPolicy(filesystem = Filesystem(read_write = "./output")),
    languages = "r",
    cache = "host",
    environment = c(LABEL = "worker"),
    inherit_environment = FALSE
  )
  expected <- list(
    r = list(
      executable = "/opt/R/bin/R",
      packages = list(),
      resolution = "startup_only",
      vanilla = TRUE
    ),
    python = list(
      managed = list(
        version = "3.13",
        packages = list("pandas"),
        resolution = "explicit"
      )
    ),
    resolver = list(
      environment = list(INDEX = "", LABEL = "resolver"),
      sandbox = list(network = "enabled")
    ),
    sandbox = list(filesystem = list(read_write = list("./output"))),
    languages = list("r"),
    cache = "host",
    environment = list(LABEL = "worker"),
    inherit_environment = FALSE
  )
  expect_identical(as.list(config), expected)
  expect_identical(
    jsonlite::fromJSON(
      jsonlite::toJSON(as.list(config), auto_unbox = TRUE),
      simplifyVector = FALSE
    ),
    expected
  )
  expect_identical(
    as.list(ConsoleConfig()),
    structure(list(), names = character())
  )
  expect_identical(
    as.list(ConsoleConfig(python = ExistingPython(".venv"))),
    list(python = list(existing = ".venv"))
  )
  expect_identical(
    as.list(ConsoleConfig(environment = character())),
    list(environment = structure(list(), names = character()))
  )
  expect_identical(
    as.list(ConsoleConfig(sandbox = FALSE)),
    structure(list(), names = character())
  )
})

test_that("configuration classes have matching uppercase CamelCase names", {
  for (name in c(
    "ConsoleConfig",
    "ConfigDiscovery",
    "RConfig",
    "ManagedPython",
    "ExistingPython",
    "ResolverConfig",
    "SandboxPolicy",
    "Filesystem",
    "Network",
    "Proxy",
    "Domains",
    "Sockets"
  )) {
    class <- getExportedValue("mcp.console", name)
    expect_identical(class@name, name)
  }
})

test_that("configuration rejects unsupported choices before launching", {
  expect_error(ConsoleConfig(sandbox = TRUE), "sandbox")
  expect_error(
    ConsoleConfig(
      sandbox = FALSE,
      resolver = ResolverConfig(sandbox = SandboxPolicy())
    ),
    "sandbox"
  )
  expect_error(ConfigDiscovery(global = NA), "global")
  expect_error(RConfig(resolution = "sometimes"), "resolution")
  expect_error(RConfig(resolution = "disabled", packages = "dplyr"), "packages")
  expect_error(ManagedPython(resolution = "disabled"), "resolution")
  expect_error(ManagedPython(version = c("3.12", "3.13")), "version")
  expect_error(ExistingPython(".venv", packages = "pandas"), "unused argument")
  expect_error(ResolverConfig(environment = c("unnamed")), "environment")
  expect_error(console_tool(config = list(), path = "missing"), "ConsoleConfig")
  expect_error(console_tool(sandbox = FALSE, path = "missing"), "must be empty")
  expect_error(
    console_tool(no_sandbox = TRUE, path = "missing"),
    "must be empty"
  )
  expect_error(console_tool(project = "missing", path = "missing"), "project")
})

test_that("console_tool defaults to no discovery and exposes explicit source choices", {
  with_console_config_probe(function(path, record) {
    choices <- list(
      NULL,
      ConsoleConfig(),
      ConsoleConfig(discovery = ConfigDiscovery()),
      ConsoleConfig(discovery = ConfigDiscovery(global = FALSE)),
      ConsoleConfig(discovery = ConfigDiscovery(project = FALSE)),
      ConsoleConfig(
        discovery = ConfigDiscovery(global = FALSE, project = FALSE)
      ),
      ConsoleConfig(sandbox = FALSE)
    )
    expected <- list(
      c("serve", "--no-config"),
      c("serve", "--no-config"),
      "serve",
      c("serve", "--no-global-config"),
      c("serve", "--no-project-config"),
      c("serve", "--no-config"),
      c(
        "serve",
        "--no-config",
        "--no-sandbox",
        "-c",
        "sandbox=null",
        "-c",
        "resolver.sandbox=null"
      )
    )
    for (i in seq_along(choices)) {
      tool <- console_tool(path = path, config = choices[[i]])
      expect_identical(readLines(record, warn = FALSE), expected[[i]])
      rm(tool)
      invisible(gc())
    }
  })
})

test_that("runtime options layer while explicit permission nodes replace", {
  with_console_config_probe(function(path, record) {
    config <- ConsoleConfig(
      discovery = ConfigDiscovery(),
      r = RConfig(packages = character()),
      python = ManagedPython(packages = character()),
      sandbox = SandboxPolicy(),
      resolver = ResolverConfig(sandbox = SandboxPolicy())
    )
    tool <- console_tool(path = path, config = config)
    argv <- readLines(record, warn = FALSE)
    expect_identical(
      argv,
      c(
        "serve",
        "-c",
        "sandbox=null",
        "-c",
        "resolver.sandbox=null",
        "-c",
        "python=null",
        "-c",
        "r={\"packages\":[]}",
        "-c",
        "python={\"managed\":{\"packages\":[]}}",
        "-c",
        "resolver={\"sandbox\":{}}",
        "-c",
        "sandbox={}"
      )
    )
    rm(tool)
    invisible(gc())
  })
})

test_that("project selection changes only the child's launch directory", {
  with_console_config_probe(function(path, record) {
    with_sandbox_directory({
      project <- file.path(getwd(), "project with spaces")
      dir.create(project)
      old <- getwd()
      tool <- console_tool(path = path, project = project)
      expect_identical(
        normalizePath(readLines(paste0(record, ".cwd"))),
        normalizePath(project)
      )
      expect_identical(getwd(), old)
      rm(tool)
      invisible(gc())
    })
  })
})
