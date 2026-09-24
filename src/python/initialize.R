base::local(
  {
    namespace <- NULL
    globals <- NULL
    selected <- NULL
    old_path <- NULL
    old_session <- NULL
    old_python_path <- NULL
    python_embedded <- FALSE

    replace_binding <- function(name, value) {
      was_locked <- bindingIsLocked(name, namespace)
      if (was_locked) {
        unlockBinding(name, namespace)
      }
      assign(name, value, envir = namespace)
      if (was_locked) lockBinding(name, namespace)
    }

    select_python <- function(required_module = NULL, use_environment = NULL) {
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
      } else if (.Platform$OS.type != "windows" && is.null(config$libpython)) {
        python_not_found(
          "Python shared library not found, Python bindings not loaded."
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
      python_embedded <<- !is.null(get("main_process_python_info", namespace)())

      # These are reticulate's environment inputs to CPython. Set them before
      # the native owner initializes the selected interpreter.
      if (nzchar(config$virtualenv)) {
        Sys.setenv(VIRTUAL_ENV = config$virtualenv)
      }
      old_session <<- Sys.getenv("R_SESSION_INITIALIZED", unset = NA)
      Sys.setenv(
        R_SESSION_INITIALIZED = sprintf(
          'PID=%s:NAME="reticulate"',
          Sys.getpid()
        )
      )
      if (get("is_rstudio", namespace)()) {
        if (is.na(Sys.getenv("PYTHONIOENCODING", unset = NA))) {
          Sys.setenv(PYTHONIOENCODING = "utf-8")
        }
      }
      old_path <<- get("python_munge_path", namespace)(config$python)
      get("prefix_python_lib_to_ld_library_path", namespace)(config$python)
      if (get("is_osx", namespace)()) {
        symlink <- Sys.getenv("RSTUDIO_FALLBACK_LIBRARY_PATH", unset = NA)
        if (!is.na(symlink)) {
          unlink(symlink)
          file.symlink(dirname(config$libpython), symlink)
        }
      }
      old_python_path <<- Sys.getenv("PYTHONPATH")
      python_path <- Sys.getenv(
        "RETICULATE_PYTHONPATH",
        unset = paste(
          config$pythonpath,
          system.file("python", package = "reticulate"),
          sep = .Platform$path.sep
        )
      )
      Sys.setenv(PYTHONPATH = python_path)
      selected <<- config
      config
    }

    cancel_selection <- function() {
      if (!is.null(old_path)) {
        Sys.setenv(PATH = old_path)
      }
      if (!is.null(old_session)) {
        if (is.na(old_session)) {
          Sys.unsetenv("R_SESSION_INITIALIZED")
        } else {
          Sys.setenv(R_SESSION_INITIALIZED = old_session)
        }
      }
      if (!is.null(old_python_path)) {
        Sys.setenv(PYTHONPATH = old_python_path)
      }
      invisible()
    }

    state$selected_python <- function() {
      config <- select_python()
      jsonlite::toJSON(
        list(
          python = config$python,
          libpython = config$libpython,
          python_home = config$pythonhome
        ),
        auto_unbox = TRUE
      )
    }
    state$cancel_python_selection <- function() {
      cancel_selection()
      "cancelled"
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
      attached <- FALSE
      on.exit(
        {
          if (!attached) {
            cancel_selection()
          }
          if (!is.null(old_python_path)) {
            Sys.setenv(PYTHONPATH = old_python_path)
          }
          finish_python_initialization()
        },
        add = TRUE
      )
      get("py_initialize", namespace)(
        config$python,
        config$libpython,
        config$pythonhome,
        config$virtualenv_activate,
        config$version$major,
        config$version$minor,
        interactive(),
        numpy_load_error
      )
      attached <- TRUE
      if (!is.null(old_python_path)) {
        Sys.setenv(PYTHONPATH = old_python_path)
      }

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
      if (python_embedded) {
        path <- system.file("python", package = "reticulate")
        command <- sprintf("import sys; sys.path.append(%s)", shQuote(path))
        get("py_run_string_impl", namespace)(command)
      }
      command <- sprintf(
        "import sys; sys.executable  = r'''%s'''",
        config$executable
      )
      get("py_run_string_impl", namespace)(command, local = TRUE)
      if (nzchar(config$base_executable)) {
        command <- sprintf(
          "import sys; sys._base_executable = r'''%s'''",
          config$base_executable
        )
        get("py_run_string_impl", namespace)(command, local = TRUE)
      }
      get("py_run_string_impl", namespace)(
        "import sys; sys.path.insert(0, '')",
        local = TRUE
      )
      get("py_set_qt_qpa_platform_plugin_path", namespace)(config)
      if (get("was_python_initialized_by_reticulate", namespace)()) {
        allow_threads <- tolower(Sys.getenv(
          "RETICULATE_ALLOW_THREADS",
          "true"
        )) %in%
          c("true", "1", "yes")
        if (allow_threads) get("py_allow_threads_impl", namespace)(TRUE)
      }
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
        config$python,
        config$libpython,
        config$pythonhome
      ))
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
