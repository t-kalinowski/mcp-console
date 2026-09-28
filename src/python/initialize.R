base::local(
  {
    namespace <- NULL
    globals <- NULL
    selected <- NULL
    inspected <- NULL

    replace_binding <- function(name, value) {
      was_locked <- bindingIsLocked(name, namespace)
      if (was_locked) {
        unlockBinding(name, namespace)
      }
      assign(name, value, envir = namespace)
      if (was_locked) lockBinding(name, namespace)
    }

    select_python <- function(
      required_module = NULL,
      use_environment = NULL,
      run_before_initialized = FALSE
    ) {
      if (is.null(namespace)) {
        asNamespace("reticulate")
      }
      if (!is.null(selected)) {
        return(selected)
      }
      if (get("is_python_initialized", envir = namespace)()) {
        selected <<- globals$py_config
        return(selected)
      }
      if (run_before_initialized) {
        # R-first calls arrive through reticulate::ensure_python_initialized(),
        # which has already invoked this callback.
        callback <- getOption("reticulate.python.beforeInitialized")
        if (is.function(callback)) callback()
      }

      # Keep reticulate's discovery and its R-side selection hints in one place.
      if (!is.null(required_module)) {
        required_module <- strsplit(required_module, ".", fixed = TRUE)[[1L]][[
          1L
        ]]
      }
      py_discover_config <- get("py_discover_config", namespace)
      config <- local({
        previous_options <- options(reticulate.python.initializing = TRUE)
        on.exit(options(previous_options), add = TRUE)
        py_discover_config(required_module, use_environment)
      })
      python_not_found <- function(message) {
        hint <- paste0(
          'See the Python "Order of Discovery" here: ',
          'https://rstudio.github.io/reticulate/articles/versions.html#order-of-discovery.'
        )
        stop(paste(message, hint, sep = "\n"), call. = FALSE)
      }
      if (is.null(config)) {
        python_not_found(
          "Installation of Python not found, Python bindings not loaded."
        )
      } else if (get("is_incompatible_arch", namespace)(config)) {
        fmt <- "Your current architecture is %s; however, this version of Python was compiled for %s."
        message <- sprintf(
          fmt,
          get("current_python_arch", namespace)(),
          config$architecture
        )
        python_not_found(message)
      }
      state$check_python_version(config)
      # Discovery retains reticulate's selection precedence and metadata.
      # Console owns the embedding fields consumed by both native startup
      # and reticulate's later attachment to that same interpreter.
      inspected <<- tryCatch(
        .Call("mcp_console_inspect_python", config$python),
        error = function(error) {
          class(error) <- c("console_python_inspection_error", class(error))
          stop(error)
        }
      )
      embedding <- jsonlite::fromJSON(inspected)$embedding
      config$python <- embedding$python
      config$executable <- embedding$python
      config$libpython <- embedding$libpython
      config$pythonhome <- embedding$python_home
      selected <<- config
      config
    }

    cancel_selection <- function() {
      # Only called before CPython starts. Selection has no process mutations.
      inspected <<- NULL
      selected <<- NULL
      invisible()
    }

    state$selected_python <- function() {
      # Extend a fresh selection's cleanup through serialization. A cached
      # selection may already belong to a running interpreter.
      pending <- is.null(selected) &&
        !reticulate::py_available(initialize = FALSE)
      on.exit(if (pending && !is.null(selected)) cancel_selection(), add = TRUE)
      config <- tryCatch(
        select_python(run_before_initialized = TRUE),
        console_python_inspection_error = function(error) {
          message("Error: ", conditionMessage(error))
          NULL
        }
      )
      # Inspection has not committed an interpreter or environment. Report
      # its failure as cell output and leave the existing worker retryable.
      if (is.null(config)) {
        return("")
      }
      result <- inspected
      pending <- FALSE
      result
    }
    state$cancel_python_selection <- function() {
      cancel_selection()
      0L
    }

    finish_python_initialization <- function() {
      invisible(.Call("mcp_console_finish_python_initialization"))
    }

    install_console_services <- function() {
      invisible(.Call(
        "mcp_console_install_python_services",
        reticulate::py_config()$libpython
      ))
    }

    attach_python <- function(config) {
      numpy_load_error <- tryCatch(
        {
          if (is.null(config$numpy) || config$numpy$version < "1.6") {
            "installation of Numpy >= 1.6 not found"
          } else {
            ""
          }
        },
        error = function(error) "<unknown>"
      )
      # Console owns CPython's environment. Reticulate attaches conversion
      # and callbacks without activating or changing the running interpreter.
      on.exit(finish_python_initialization(), add = TRUE)
      get("py_initialize", namespace)(
        config$python,
        config$libpython,
        config$pythonhome,
        "",
        config$version$major,
        config$version$minor,
        interactive(),
        numpy_load_error
      )

      # Reticulate owns conversion, cross-language calls, and event integration.
      reg.finalizer(
        globals,
        function(environment) {
          try(get("py_allow_threads_impl", namespace)(FALSE))
          if (
            tolower(Sys.getenv("RETICULATE_ENABLE_PYTHON_FINALIZER")) %in%
              c("true", "1", "yes")
          ) {
            get("py_finalize", namespace)()
          }
        },
        onexit = TRUE
      )
      config$available <- TRUE
      # Declaration diagnostics are reticulate compatibility, not selection
      # or activation. Preserve its opt-out and warning behavior.
      if (nzchar(config$virtualenv)) {
        check_packages <- tolower(Sys.getenv(
          "RETICULATE_CHECK_REQUIRED_PACKAGES",
          "true"
        )) %in%
          c("true", "1", "yes")
        if (check_packages) {
          tryCatch(
            get("check_virtualenv_required_packages", namespace)(config),
            error = function(error) invisible()
          )
        }
      }
      config
    }

    initialize_python <- function(
      required_module = NULL,
      use_environment = NULL
    ) {
      config <- select_python(required_module, use_environment)
      invisible(.Call(
        "mcp_console_initialize_python",
        inspected,
        system.file("python", package = "reticulate")
      ))
      # Reticulate publishes .globals$py_config only after this returns, so
      # unfinished attachment remains eligible for its existing retry path.
      attach_python(config)
    }

    install_python_initializer <- function(...) {
      namespace <<- asNamespace("reticulate")
      globals <<- get(".globals", envir = namespace)
      replace_binding("initialize_python", initialize_python)
      original_inject_hooks <- get("py_inject_hooks", envir = namespace)
      inject_hooks <- function() {
        original_inject_hooks()
        # Install after reticulate's input hook, including R-first startup.
        install_console_services()
      }
      replace_binding("py_inject_hooks", inject_hooks)
      # Reticulate reinstalls its interrupt handler after injecting hooks.
      replace_binding("install_interrupt_handlers", install_console_services)

      if (get("is_python_initialized", envir = namespace)()) {
        rust_owned <- isTRUE(.Call(
          "mcp_console_load_python_library",
          reticulate::py_config()$libpython
        ))
        if (rust_owned) {
          finish_python_initialization()
        }
        install_console_services()
      }
      invisible()
    }

    setHook(
      packageEvent("reticulate", "onLoad"),
      install_python_initializer,
      action = "append"
    )
    if ("reticulate" %in% loadedNamespaces()) {
      install_python_initializer()
    }
    invisible()
  },
  envir = base::new.env(parent = environment())
)
