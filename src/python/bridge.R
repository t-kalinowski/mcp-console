base::local(
  {
    state <- function() jsonlite::fromJSON(.Call("mcp_console_python_state"))
    manifest <- function(value) {
      list(
        packages = I(sort(unique(value$packages))),
        python_version = I(sort(unique(as.character(value$python_version)))),
        exclude_newer = value$exclude_newer
      )
    }
    prepare <- function(requirements) {
      request <- jsonlite::toJSON(
        list(manifest = manifest(requirements)),
        auto_unbox = TRUE,
        null = "null"
      )
      result <- jsonlite::fromJSON(.Call("mcp_console_python_prepare", request))
      if (!identical(result$kind, "prepared")) {
        stop(result$message, call. = FALSE)
      }
      invisible()
    }
    install <- function(...) {
      namespace <- asNamespace("reticulate")
      replace <- function(name, value) {
        locked <- bindingIsLocked(name, namespace)
        if (locked) {
          unlockBinding(name, namespace)
        }
        assign(name, value, envir = namespace)
        if (locked) lockBinding(name, namespace)
      }
      globals <- get(".globals", envir = namespace)
      config <- globals$py_config
      rm(list = "py_config", envir = globals)
      makeActiveBinding(
        "py_config",
        function(value) {
          if (!missing(value)) {
            config <<- value
          }
          current <- state()
          if (
            !is.null(config) &&
              !is.null(current$discovery) &&
              !identical(config$python, current$discovery$executable)
          ) {
            config <<- get("python_config", envir = namespace)(
              current$discovery$executable
            )
            config$ephemeral <<- !is.null(current$manifest)
            config$available <<- TRUE
          }
          config
        },
        globals
      )
      original_initialize <- get("initialize_python", envir = namespace)
      replace("initialize_python", function(...) {
        current <- jsonlite::fromJSON(.Call("mcp_console_python_initialize"))
        selected <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        on.exit(
          if (is.na(selected)) {
            Sys.unsetenv("RETICULATE_PYTHON")
          } else {
            Sys.setenv(RETICULATE_PYTHON = selected)
          },
          add = TRUE
        )
        Sys.setenv(RETICULATE_PYTHON = current$discovery$executable)
        config <- original_initialize(...)
        .Call("mcp_console_python_connect_interrupts")
        config$ephemeral <- !is.null(current$manifest)
        config
      })
      replace("remap_output_streams", function(...) invisible())
      replace("install_interrupt_handlers", function(...) invisible())
      current <- state()$manifest
      if (!is.null(current)) {
        replace(
          "resolve_python_version",
          function(constraints = NULL, uv = NULL) {
            request <- jsonlite::toJSON(
              list(constraints = I(as.character(constraints))),
              auto_unbox = TRUE
            )
            .Call("mcp_console_resolve_python_version", request)
          }
        )
        requirements <- get("py_reqs_get", envir = namespace)()
        requirements$history <- list(list(
          requested_from = "mcp-console",
          env_is_package = FALSE,
          packages = current$packages,
          python_version = current$python_version,
          exclude_newer = current$exclude_newer,
          exclude_newer_supplied = !is.null(current$exclude_newer),
          action = "set"
        ))
        rm(list = "python_requirements", envir = globals)
        makeActiveBinding(
          "python_requirements",
          function(value) {
            if (!missing(value)) {
              prepare(value)
              requirements <<- value
            }
            current <- state()$manifest
            requirements$packages <- current$packages
            requirements$python_version <- if (length(current$python_version)) {
              current$python_version
            } else {
              NULL
            }
            requirements$exclude_newer <- current$exclude_newer
            requirements
          },
          globals
        )
        replace("py_reqs_activate", function(requirements) {
          prepare(requirements)
          config <- get("python_config", envir = namespace)(
            state()$discovery$executable
          )
          config$ephemeral <- TRUE
          config$available <- TRUE
          config
        })
      }
      invisible()
    }
    setHook(packageEvent("reticulate", "onLoad"), install, action = "append")
    if ("reticulate" %in% loadedNamespaces()) {
      install()
    }
    # These bindings are language conveniences; reading py activates the bridge.
    tools <- base::attach(
      NULL,
      pos = 2L,
      name = "tools:mcp-console",
      warn.conflicts = FALSE
    )
    base::makeActiveBinding(
      "py",
      function() reticulate::import_main(convert = TRUE),
      tools
    )
    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
