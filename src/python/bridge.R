base::local(
  {
    initialized <- FALSE
    managed <- Sys.getenv("MCP_CONSOLE_MANAGED_PYTHON", unset = NA_character_)
    dynamic_resolution <- identical(
      Sys.getenv(
        "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
        unset = "1"
      ),
      "1"
    )
    # Python 3.9 and older are intentionally outside the bridge contract.
    minimum_python <- base::numeric_version("3.10")
    # Reticulate callable proxies convert results through an interruptible wrapper.
    # Keep helpers in one module, then use py_eval's direct conversion path.
    python_dispatch <-
      "(lambda: None).__builtins__['_mcp_console_dispatch']()"
    python_module <- NULL
    `%||%` <- function(x, y) if (is.null(x)) y else x
    managed_python_disabled_message <- if (
      !dynamic_resolution &&
        Sys.getenv("MCP_CONSOLE_EXECUTION_COMPUTE") %in%
          c("docker", "docker_sandbox")
    ) {
      paste0(
        "MCP Console dynamic environment resolution is unavailable for ",
        if (Sys.getenv("MCP_CONSOLE_EXECUTION_COMPUTE") == "docker") {
          "Docker"
        } else {
          "Docker Sandbox"
        },
        " targets. ",
        "Install the distribution in the image and start a new server session."
      )
    } else if (!dynamic_resolution) {
      paste0(
        "MCP Console dynamic environment resolution is unavailable. ",
        "Install the distribution into the ambient Python environment, or ",
        "install `ir` or `uv` and restart MCP Console."
      )
    } else {
      paste0(
        "MCP Console is using a user-selected Python environment. ",
        "Automatic managed package resolution is disabled, and ",
        "`requirements.python` is also disabled for this interpreter selection. ",
        "Install the distribution into the selected environment or restart MCP ",
        "Console with managed Python enabled."
      )
    }

    owner_state <- function() {
      jsonlite::fromJSON(.Call("mcp_console_python_environment_state"))
    }

    install_managed_python <- function(...) {
      namespace <- asNamespace("reticulate")
      globals <- get(".globals", envir = namespace)
      original <- get("py_reqs_get", envir = namespace)()
      history <- original$history
      current_requirements <- function() {
        value <- owner_state()$manifest
        structure(
          list(
            python_version = if (length(value$python_version)) {
              value$python_version
            } else {
              NULL
            },
            packages = unlist(value$packages, use.names = FALSE) %||%
              character(),
            exclude_newer = value$exclude_newer,
            history = history
          ),
          class = "python_requirements"
        )
      }
      seed <- current_requirements()
      history <- c(
        history,
        list(list(
          requested_from = "mcp-console",
          env_is_package = FALSE,
          packages = seed$packages,
          python_version = seed$python_version,
          exclude_newer = seed$exclude_newer,
          exclude_newer_supplied = !is.null(seed$exclude_newer),
          action = "set"
        ))
      )
      rm(list = "python_requirements", envir = globals)
      makeActiveBinding(
        "python_requirements",
        function(value) {
          if (missing(value)) {
            return(current_requirements())
          }
          current <- current_requirements()
          stopifnot(
            identical(value$packages, current$packages),
            identical(value$python_version, current$python_version),
            identical(value$exclude_newer, current$exclude_newer)
          )
          # Only API provenance lives in R; the native owner already committed
          # the transition before reticulate assigns its returned metadata.
          history <<- value$history
          invisible(value)
        },
        globals
      )

      # Bind the stored configuration, so py_config(), py_exe(), and internal
      # readers all see the owner. The public functions remain unchanged, and
      # this getter never calls the initializer back through itself.
      config <- globals$py_config
      rm(list = "py_config", envir = globals)
      makeActiveBinding(
        "py_config",
        function(value) {
          if (!missing(value)) {
            config <<- value
            return(invisible(value))
          }
          active <- owner_state()$active
          result <- config
          if (!is.null(result) && !is.null(active)) {
            result$python <- result$executable <- active$executable
            result$libpython <- active$libpython
            result$prefix <- active$prefix
            result$exec_prefix <- active$exec_prefix
            result$pythonpath <- active$pythonpath
            result$pythonhome <- paste(
              active$prefix,
              active$exec_prefix,
              sep = ":"
            )
            result$virtualenv <- active$prefix
            activate_this <- file.path(
              dirname(active$executable),
              "activate_this.py"
            )
            result$virtualenv_activate <- if (file.exists(activate_this)) {
              activate_this
            } else {
              ""
            }
          }
          result
        },
        globals
      )
      replace_binding <- function(name, value) {
        was_locked <- bindingIsLocked(name, namespace)
        if (was_locked) {
          unlockBinding(name, namespace)
        }
        assign(name, value, envir = namespace)
        if (was_locked) lockBinding(name, namespace)
      }
      encode <- function(value) {
        jsonlite::toJSON(value, auto_unbox = TRUE, null = "null", na = "null")
      }
      declare <- function(request) {
        request$packages <- if (is.null(request$packages)) {
          NULL
        } else {
          I(request$packages)
        }
        request$python_version <- if (is.null(request$python_version)) {
          NULL
        } else {
          I(request$python_version)
        }
        invisible(.Call("mcp_console_python_declare", encode(request)))
      }
      # Managed py_require() delegates declarations and live activation here;
      # startup reads the same owner through the bootstrap below.
      replace_binding(
        "py_reqs_transition",
        function(current, request, initialized) {
          declare(request)
          result <- current_requirements()
          result$history <- c(current$history, list(request))
          list(manifest = result, config = NULL)
        }
      )
      replace_binding(
        "uv_get_or_create_env",
        function(
          packages = current_requirements()$packages,
          python_version = get("py_reqs_python_version", envir = namespace)(),
          exclude_newer = current_requirements()$exclude_newer
        ) {
          .Call(
            "mcp_console_python_bootstrap",
            encode(I(python_version %||% character()))
          )
        }
      )
      replace_binding(
        "resolve_python_version",
        function(constraints = NULL, uv = NULL) {
          .Call(
            "mcp_console_resolve_python_version",
            encode(list(
              constraints = I(as.character(constraints %||% character()))
            ))
          )
        }
      )
      invisible()
    }

    if (!is.na(managed)) {
      Sys.unsetenv("MCP_CONSOLE_MANAGED_PYTHON")
      setHook(
        packageEvent("reticulate", "onLoad"),
        install_managed_python,
        action = "append"
      )
      if ("reticulate" %in% loadedNamespaces()) install_managed_python()
    }

    dispatch_python <- function(operation, arguments = list()) {
      reticulate::py_set_attr(
        python_module,
        "operation",
        operation
      )
      reticulate::py_set_attr(
        python_module,
        "arguments",
        arguments
      )
      reticulate::py_eval(python_dispatch, convert = TRUE)
    }

    initialize_python_runtime <- function(strict = FALSE) {
      if (!is.null(python_module)) {
        return(invisible(TRUE))
      }

      python_config <- reticulate::py_config()
      if (!is.null(python_module)) {
        return(invisible(TRUE))
      }
      if (python_config$version < minimum_python) {
        if (!strict) {
          return(invisible(FALSE))
        }
        stop(
          paste0(
            "MCP Console requires Python 3.10 or later; selected ",
            "interpreter reports Python ",
            as.character(python_config$version)
          ),
          call. = FALSE
        )
      }
      invisible(.Call(
        "mcp_console_install_python_runtime",
        python_config$libpython
      ))
      # Run cancellable inspection after leaving harp's .Call interrupt mask.
      # Reticulate retains startup and converts KeyboardInterrupt to an R interrupt.
      invisible(reticulate::import(
        "_mcp_console_services",
        convert = FALSE
      )$initialize_python_environment(python_config$libpython))
      python_module <<- reticulate::import("_mcp_console", convert = FALSE)
      configured <- FALSE
      on.exit(
        if (!configured) python_module <<- NULL,
        add = TRUE
      )
      if (is.na(managed)) {
        invisible(dispatch_python(
          "configure_import_resolution",
          list(
            NULL,
            managed_python_disabled_message
          )
        ))
      }
      configured <- TRUE
      invisible(TRUE)
    }

    disable_matplotlib_show <- function(...) {
      if (initialize_python_runtime(strict = FALSE)) {
        invisible(dispatch_python("disable_matplotlib_show"))
      }
    }
    base::setHook(
      "reticulate::matplotlib.pyplot::load",
      disable_matplotlib_show,
      action = "append"
    )

    console_width <- getOption("width")
    install_python_hooks <- function(...) {
      namespace <- asNamespace("reticulate")
      configure_numpy <- function() {
        numpy <- reticulate::import("numpy", convert = FALSE)
        numpy$set_printoptions(linewidth = console_width)
      }
      configure_pandas <- function() {
        pandas <- reticulate::import("pandas", convert = FALSE)
        pandas$set_option("display.width", console_width)
      }
      on_python_init <- function() {
        # Reticulate imports NumPy before its module-load hooks are installed.
        reticulate::py_register_load_hook("numpy", configure_numpy)
        reticulate::py_register_load_hook("pandas", configure_pandas)
        initialize_python_runtime(strict = FALSE)
      }
      base::setHook(
        "reticulate.onPyInit",
        on_python_init,
        action = "append"
      )
      if (get("is_python_initialized", envir = namespace)()) {
        on_python_init()
      }
      invisible()
    }
    setHook(
      packageEvent("reticulate", "onLoad"),
      install_python_hooks,
      action = "append"
    )
    if ("reticulate" %in% loadedNamespaces()) {
      install_python_hooks()
    }

    evaluate_impl <- function() {
      if (!initialized) {
        initialize_python_runtime(strict = TRUE)
        dispatch_python("disable_matplotlib_show")
        initialized <<- TRUE
      }

      invisible()
    }

    interrupted <- FALSE

    evaluate <- function(id) {
      interrupted <<- FALSE
      # Observe the condition without handling it; R_tryEval remains the boundary.
      withCallingHandlers(
        evaluate_impl(),
        interrupt = function(condition) interrupted <<- TRUE,
        error = function(condition) interrupted <<- FALSE
      )
    }

    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
