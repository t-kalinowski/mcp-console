base::local(
  {
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
    # Import-resolver registration still converts its R callback through
    # reticulate without adding dispatcher names to the user's globals.
    python_dispatch <-
      "(lambda: None).__builtins__['_mcp_console_dispatch']()"
    pending_import_resolution <- NULL
    requirements_adapter <- NULL
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

    manifest <- function(packages, python_version, exclude_newer) {
      list(
        packages = I(sort(unique(packages %||% character()))),
        python_version = I(sort(unique(python_version %||% character()))),
        exclude_newer = exclude_newer
      )
    }

    request_json <- function(
      requirements,
      retained_requirements,
      import_resolution = NULL
    ) {
      request <- list(
        requirements = requirements,
        retained_requirements = retained_requirements
      )
      if (!is.null(import_resolution)) {
        request$import_resolution <- import_resolution
      }
      jsonlite::toJSON(
        request,
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    version_request_json <- function(constraints) {
      jsonlite::toJSON(
        list(
          constraints = I(as.character(constraints %||% character()))
        ),
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    activation_manifest <- function(requirements) {
      manifest(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      )
    }

    install_managed_python <- function(...) {
      namespace <- asNamespace("reticulate")
      current_requirements <- function() {
        get("py_reqs_get", envir = namespace)()
      }
      resolve <- function(
        packages = current_requirements()$packages,
        python_version = get("py_reqs_python_version", envir = namespace)(),
        exclude_newer = current_requirements()$exclude_newer
      ) {
        current <- current_requirements()
        requirements <- manifest(packages, python_version, exclude_newer)
        retained_requirements <- manifest(
          packages,
          current$python_version,
          exclude_newer
        )
        .Call(
          "mcp_console_resolve_python",
          request_json(
            requirements,
            retained_requirements,
            pending_import_resolution
          )
        )
      }
      resolve_version <- function(constraints = NULL, uv = NULL) {
        # The host resolver owns its executable and environment; worker code
        # supplies only version constraints.
        .Call(
          "mcp_console_resolve_python_version",
          version_request_json(constraints)
        )
      }
      seed <- jsonlite::fromJSON(managed)
      packages <- unlist(seed$packages, use.names = FALSE)
      python_version <- unlist(seed$python_version, use.names = FALSE)
      if (!length(python_version)) {
        python_version <- NULL
      }
      globals <- get(".globals", envir = namespace)
      requirements <- get("py_reqs_get", envir = namespace)()
      changed <- !identical(
        manifest(
          requirements$packages,
          requirements$python_version,
          requirements$exclude_newer
        ),
        manifest(packages, python_version, seed$exclude_newer)
      )
      if (changed) {
        requirements$packages <- packages
        requirements$python_version <- python_version
        requirements$exclude_newer <- seed$exclude_newer
        requirements$history <- c(
          requirements$history,
          list(list(
            requested_from = "mcp-console",
            env_is_package = FALSE,
            packages = packages,
            python_version = python_version,
            exclude_newer = seed$exclude_newer,
            exclude_newer_supplied = !is.null(seed$exclude_newer),
            action = "set"
          ))
        )
        globals$python_requirements <- requirements
      }
      stopifnot(
        !bindingIsActive("python_requirements", globals),
        !bindingIsLocked("python_requirements", globals)
      )
      .Call("mcp_console_python_requirements_set", requirements, NULL)
      rm(requirements)
      rm(list = "python_requirements", envir = globals)
      makeActiveBinding(
        "python_requirements",
        function(value) {
          if (missing(value)) {
            return(.Call("mcp_console_python_requirements_get"))
          }
          activation <- if (.Call("mcp_console_python_activation_pending")) {
            activation_manifest(value)
          } else {
            NULL
          }
          .Call("mcp_console_python_requirements_set", value, activation)
          invisible(value)
        },
        globals
      )

      replace_binding <- function(name, value) {
        was_locked <- bindingIsLocked(name, namespace)
        if (was_locked) {
          unlockBinding(name, namespace)
        }
        on.exit(
          if (was_locked) lockBinding(name, namespace),
          add = TRUE
        )
        assign(name, value, envir = namespace)
        invisible()
      }
      # Keep reticulate's declaration checks and candidate configuration
      # lookup. The native owner activates the selected environment.
      live_python_version <- function() {
        as.character(get("py_version", namespace)(patch = TRUE))
      }
      check_version <- function(request) {
        current_version <- get("py_version", namespace)(patch = TRUE)
        for (check in get("as_version_constraint_checkers", namespace)(
          request$python_version
        )) {
          if (!isTRUE(check(current_version))) {
            stop(paste0(
              "Python version requirements cannot be changed after Python has ",
              "been initialized.\n",
              "* Python version request: '",
              paste(request$python_version, collapse = ","),
              "'",
              if (request$env_is_package) {
                paste0(" (from package:", request$requested_from, ")")
              },
              "\n* Python version initialized: '",
              current_version,
              "'"
            ))
          }
        }
        invisible()
      }
      check_packages <- function(added, current) {
        requirement_name <- get("py_requirement_name", namespace)
        added_names <- requirement_name(added)
        current_names <- requirement_name(current)
        conflicts <- added_names %in% current_names
        if (any(conflicts)) {
          new <- paste0("`", sort(added[conflicts]), "`", collapse = ", ")
          old <- current[current_names %in% added_names[conflicts]]
          old <- paste0("`", sort(old), "`", collapse = ", ")
          stop(paste(
            "After Python has initialized, only `action = 'add'` with new packages is supported.",
            "You tried to add",
            new,
            "but requirements contain",
            old,
            "already."
          ))
        }
        invisible()
      }
      candidate_config <- function(python) {
        config <- get("python_config", namespace)(python)
        config$ephemeral <- TRUE
        config
      }
      live_libpython <- function() globals$py_config$libpython
      available_config <- function(config) {
        config$available <- TRUE
        config
      }
      raise_python_setup_error <- function() check_python_setup(FALSE)
      record_activation <- function(requirements) {
        .Call(
          "mcp_console_python_activation_record",
          activation_manifest(requirements)
        )
      }
      replace_binding("uv_get_or_create_env", resolve)
      replace_binding("resolve_python_version", resolve_version)
      transition <- function(current, request, initialized) {
        result <- .Call(
          "mcp_console_python_transition",
          current,
          request,
          initialized,
          requirements_adapter
        )
        if (inherits(result, "interrupt")) {
          stop(result)
        }
        result
      }
      declare_packages <- function(packages) {
        reticulate::py_require(packages, action = "add")
      }
      declared_requirements <- function() reticulate::py_require()
      python_initialized <- function() {
        get("is_python_initialized", namespace)()
      }
      restore_requirements <- function(snapshot) {
        globals$python_requirements <- snapshot
        invisible()
      }
      requirements_adapter <<- environment()
      replace_binding("py_reqs_transition", transition)
      setHook(
        "reticulate.onPyInit",
        function() {
          invisible(.Call(
            "mcp_console_python_initialized",
            activation_manifest(current_requirements())
          ))
        },
        action = "append"
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
      if ("reticulate" %in% loadedNamespaces()) {
        install_managed_python()
      }
    }

    prepare_packages <- function(packages) {
      if (is.na(managed)) {
        return(list(
          kind = "disabled",
          message = managed_python_disabled_message
        ))
      }
      # Loading reticulate installs the adapter without initializing Python.
      asNamespace("reticulate")
      result <- .Call(
        "mcp_console_python_prepare",
        packages,
        requirements_adapter
      )
      if (inherits(result, "interrupt")) {
        stop(result)
      }
      result
    }

    resolve_import_distribution <- function(module, distribution) {
      module <- reticulate::py_to_r(module)
      distribution <- reticulate::py_to_r(distribution)
      stopifnot(is.null(pending_import_resolution))
      if (!identical(module, distribution)) {
        pending_import_resolution <<- list(
          module = module,
          distribution = distribution
        )
      }
      on.exit(pending_import_resolution <<- NULL)
      jsonlite::toJSON(
        prepare_packages(distribution),
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    check_python_setup <- function(completed) {
      if (!completed) {
        # Native setup retains the exception without printing it. Preserve
        # reticulate's R condition classes, last error, and interrupt handling.
        reticulate::py_eval(
          "(lambda: None).__builtins__['_mcp_console_raise_setup_error']()",
          convert = TRUE
        )
      }
      invisible()
    }

    initialize_python_runtime <- function(strict = FALSE) {
      python_config <- reticulate::py_config()
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
      if (isTRUE(.Call("mcp_console_python_runtime_is_configured"))) {
        return(invisible(TRUE))
      }
      invisible(.Call(
        "mcp_console_install_python_runtime",
        python_config$libpython
      ))
      python_module <- reticulate::import("_mcp_console", convert = FALSE)
      disabled_reason <- if (is.na(managed)) {
        managed_python_disabled_message
      } else {
        NULL
      }
      callback <- if (is.na(managed)) NULL else resolve_import_distribution
      reticulate::py_set_attr(
        python_module,
        "operation",
        "configure_import_resolution"
      )
      reticulate::py_set_attr(
        python_module,
        "arguments",
        list(callback, disabled_reason)
      )
      invisible(reticulate::py_run_string(
        python_dispatch,
        local = TRUE,
        convert = FALSE
      ))
      invisible(.Call("mcp_console_python_runtime_configured"))
      invisible(TRUE)
    }

    disable_matplotlib_show <- function(...) {
      if (initialize_python_runtime(strict = FALSE)) {
        check_python_setup(.Call("mcp_console_disable_matplotlib_show"))
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

    prepare <- function(request) {
      if (is.na(managed)) {
        stop("Python preparation requires a server-managed interpreter")
      }
      packages <- unlist(jsonlite::fromJSON(request), use.names = FALSE)
      result <- prepare_packages(packages)
      if (identical(result$kind, "ready")) {
        result$kind <- "prepared"
      }
      jsonlite::toJSON(
        result,
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    evaluate_impl <- function() {
      initialize_python_runtime(strict = TRUE)
      check_python_setup(.Call("mcp_console_disable_matplotlib_show"))
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
