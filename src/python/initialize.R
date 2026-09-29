base::local(
  {
    namespace <- NULL
    globals <- NULL
    selected <- NULL
    inspected <- NULL
    selection_callback <- NULL
    incomplete_attachment <- FALSE
    captured_hint <- Sys.getenv("RETICULATE_PYTHON")

    same_python_selection <- function(requested, running) {
      requested <- reticulate:::normalize_python_path(requested)$path
      # Virtualenv executables can link to the same base binary. Compare their
      # containing directories too, while allowing aliases within one bin directory.
      identical(
        normalizePath(dirname(requested), mustWork = FALSE),
        normalizePath(dirname(running), mustWork = FALSE)
      ) &&
        identical(
          normalizePath(requested, mustWork = FALSE),
          normalizePath(running, mustWork = FALSE)
        )
    }

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
      running <- .Call("mcp_console_running_python")
      if (!is.null(running)) {
        identity <- jsonlite::fromJSON(running)
        requests <- globals$required_python_version
        # An unchanged hint already selected the captured identity. Resolving
        # it again after Python changes cwd or PATH would select a new path.
        if (!.Call("mcp_console_python_environment_selection_unchanged")) {
          requests <- c(Sys.getenv("RETICULATE_PYTHON"), requests)
        }
        for (requested in requests) {
          if (
            nzchar(requested) &&
              (requested == "managed" ||
                !same_python_selection(requested, identity$embedding$python))
          ) {
            stop(
              "Python is already initialized with another selection; restart required",
              call. = FALSE
            )
          }
        }
        inspected <<- running
        selected <<- state$conversion_config(identity)
        return(selected)
      }

      if (!is.null(selected)) {
        return(selected)
      }
      if (run_before_initialized) {
        # R-first calls arrive through reticulate::ensure_python_initialized(),
        # which has already invoked this callback.
        callback <- getOption("reticulate.python.beforeInitialized")
        if (is.function(callback)) {
          callback()
          selection_callback <<- callback
        }
      }

      # Ordinary selection is complete on the execution host. Startup hooks
      # may add managed declarations, but never rediscover a default interpreter.
      state$resolve_startup_declaration()
      inspected <<- .Call("mcp_console_planned_python")
      identity <- jsonlite::fromJSON(inspected)
      hint <- Sys.getenv("RETICULATE_PYTHON")
      if (
        !identical(hint, captured_hint) &&
          nzchar(hint) &&
          (hint == "managed" ||
            !same_python_selection(hint, identity$embedding$python))
      ) {
        stop(
          "Python startup selection conflicts with the launch configuration; configure python and restart",
          call. = FALSE
        )
      }
      if (!nzchar(hint)) {
        for (requested in globals$required_python_version) {
          if (!same_python_selection(requested, identity$embedding$python)) {
            stop(
              "Python startup selection conflicts with the launch configuration; configure python and restart",
              call. = FALSE
            )
          }
        }
      }
      selected <<- state$conversion_config(identity)
      selected
    }

    cancel_selection <- function() {
      # Only called before CPython starts. Selection has no process mutations.
      inspected <<- NULL
      selected <<- NULL
      selection_callback <<- NULL
      invisible()
    }

    state$selected_python <- function() {
      # Extend a fresh selection's cleanup through serialization. A cached
      # selection may already belong to a running interpreter.
      pending <- is.null(selected) &&
        !reticulate::py_available(initialize = FALSE)
      on.exit(if (pending && !is.null(selected)) cancel_selection(), add = TRUE)
      select_python(run_before_initialized = TRUE)
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
      reticulate:::py_initialize(
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
          try(reticulate:::py_allow_threads_impl(FALSE))
          if (
            tolower(Sys.getenv("RETICULATE_ENABLE_PYTHON_FINALIZER")) %in%
              c("true", "1", "yes")
          ) {
            reticulate:::py_finalize()
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
            reticulate:::check_virtualenv_required_packages(config),
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
      original_python_config <- get("python_config", envir = namespace)
      replace_binding("python_config", function(python, ...) {
        identity <- jsonlite::fromJSON(.Call("mcp_console_planned_python"))
        if (same_python_selection(python, identity$embedding$python)) {
          return(state$conversion_config(identity))
        }
        original_python_config(python, ...)
      })
      replace_binding("initialize_python", initialize_python)
      original_ensure_initialized <- get(
        "ensure_python_initialized",
        envir = namespace
      )
      replace_binding("ensure_python_initialized", function(...) {
        if (incomplete_attachment) {
          stop(
            "R/Python attachment is incomplete; restart required",
            call. = FALSE
          )
        }
        completed <- FALSE
        on.exit(
          {
            # Before py_config publication, reticulate can retry attachment to
            # the same interpreter. Later hooks may have arbitrary partial effects.
            if (!completed && !is.null(globals$py_config)) {
              incomplete_attachment <<- TRUE
            }
          },
          add = TRUE
        )
        callback <- getOption("reticulate.python.beforeInitialized")
        if (
          !is.null(selection_callback) &&
            identical(callback, selection_callback) &&
            !is.null(.Call("mcp_console_running_python"))
        ) {
          options(reticulate.python.beforeInitialized = NULL)
          on.exit(
            {
              if (is.null(getOption("reticulate.python.beforeInitialized"))) {
                options(reticulate.python.beforeInitialized = callback)
              }
            },
            add = TRUE
          )
        }
        result <- original_ensure_initialized(...)
        completed <- TRUE
        invisible(result)
      })
      original_use_python <- get("use_python", envir = namespace)
      replace_binding("use_python", function(python, required = NULL) {
        running <- .Call("mcp_console_running_python")
        if (!is.null(running) && !identical(required, FALSE)) {
          identity <- jsonlite::fromJSON(running)
          if (!same_python_selection(python, identity$embedding$python)) {
            stop(
              "Python is already initialized with another selection; restart required",
              call. = FALSE
            )
          }
        }
        original_use_python(python, required)
      })
      original_inject_hooks <- get("py_inject_hooks", envir = namespace)
      inject_hooks <- function() {
        original_inject_hooks()
        # Install after reticulate's input hook, including R-first startup.
        install_console_services()
      }
      replace_binding("py_inject_hooks", inject_hooks)
      # Reticulate reinstalls its interrupt handler after injecting hooks.
      replace_binding("install_interrupt_handlers", install_console_services)

      # Installed before default packages can initialize reticulate.
      state$install_python_hooks()
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
