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
    pending_import_resolution <- NULL
    pending_requirements <- NULL
    source <- NULL
    `%||%` <- function(x, y) if (is.null(x)) y else x
    managed_python_disabled_message <- if (!dynamic_resolution) {
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

    activation_json <- function(requirements) {
      jsonlite::toJSON(
        manifest(
          requirements$packages,
          requirements$python_version,
          requirements$exclude_newer
        ),
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    report_activation <- function(requirements) {
      .Call("mcp_console_python_activated", activation_json(requirements))
      invisible()
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
      rm(list = "python_requirements", envir = globals)
      makeActiveBinding(
        "python_requirements",
        function(value) {
          if (missing(value)) {
            return(requirements)
          }
          if (!is.null(pending_requirements)) {
            committed <- manifest(
              value$packages,
              value$python_version,
              value$exclude_newer
            )
            stopifnot(identical(committed, pending_requirements))
          }
          requirements <<- value
          if (!is.null(pending_requirements)) {
            pending_requirements <<- NULL
            report_activation(committed)
          }
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
      original_activate <- get("py_reqs_activate", envir = namespace)
      activate <- function(requirements) {
        stopifnot(is.null(pending_requirements))
        config <- original_activate(requirements)
        if (is.null(python_module)) {
          reticulate::py_set_attr(
            reticulate::import("sys", convert = FALSE),
            "executable",
            config$executable
          )
        } else {
          invisible(dispatch_python(
            "activate_process_environment",
            list(config$executable)
          ))
        }
        pending_requirements <<- manifest(
          requirements$packages,
          requirements$python_version,
          requirements$exclude_newer
        )
        config
      }
      replace_binding("uv_get_or_create_env", resolve)
      replace_binding("resolve_python_version", resolve_version)
      replace_binding("py_reqs_activate", activate)
      setHook(
        "reticulate.onPyInit",
        function() {
          report_activation(current_requirements())
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

    materialize_manifest <- function() {
      if (is.na(managed) || !"reticulate" %in% loadedNamespaces()) {
        return(NULL)
      }
      namespace <- asNamespace("reticulate")
      requirements <- reticulate::py_require()
      initialized <- get("is_python_initialized", envir = namespace)()
      if (!initialized) {
        invisible(get("uv_get_or_create_env", envir = namespace)(
          requirements$packages,
          requirements$python_version,
          requirements$exclude_newer
        ))
      }
      manifest(
        requirements$packages,
        requirements$python_version,
        requirements$exclude_newer
      )
    }

    prepare_packages <- function(packages) {
      if (is.na(managed)) {
        return(list(
          kind = "disabled",
          message = managed_python_disabled_message
        ))
      }

      namespace <- asNamespace("reticulate")
      globals <- get(".globals", envir = namespace)
      snapshot <- get("py_reqs_get", envir = namespace)()
      tryCatch(
        {
          reticulate::py_require(packages, action = "add")
          requirements <- materialize_manifest()
          if (is.null(requirements)) {
            stop("Python preparation did not produce a managed manifest")
          }
          list(kind = "ready")
        },
        error = function(error) {
          globals$python_requirements <- snapshot
          list(kind = "failed", message = conditionMessage(error))
        }
      )
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
      python_module <<- reticulate::import("_mcp_console", convert = FALSE)
      configured <- FALSE
      on.exit(
        if (!configured) python_module <<- NULL,
        add = TRUE
      )
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

    evaluate_impl <- function(id) {
      if (!initialized) {
        initialize_python_runtime(strict = TRUE)
        dispatch_python("disable_matplotlib_show")
        initialized <<- TRUE
      }

      filename <- paste0("<mcp-console:python:", id, ">")
      on.exit(
        {
          images <- dispatch_python("take_images")
          for (image in images) {
            invisible(.Call("mcp_console_publish_python_plot", image))
          }
        },
        add = TRUE
      )
      dispatch_python(
        "eval_cell",
        list(source, filename)
      )
      invisible()
    }

    interrupted <- FALSE

    evaluate <- function(id) {
      interrupted <<- FALSE
      # Observe the condition without handling it; R_tryEval remains the boundary.
      withCallingHandlers(
        evaluate_impl(id),
        interrupt = function(condition) interrupted <<- TRUE,
        error = function(condition) interrupted <<- FALSE
      )
    }

    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
