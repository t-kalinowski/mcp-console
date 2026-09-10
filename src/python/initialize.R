base::local(
  {
    rust_owned <- FALSE

    finish_python_initialization <- function() {
      invisible(.Call("mcp_console_finish_python_initialization"))
    }

    configure_python_input <- function() {
      if (!rust_owned || !interactive()) {
        return(invisible())
      }
      # Reticulate remaps input only when its own C layer initialized CPython.
      namespace <- asNamespace("reticulate")
      builtins <- reticulate::import_builtins(convert = TRUE)
      input <- function(prompt = "") readline(prompt)
      globals <- get(".globals", envir = namespace)
      globals$og_input_builtin <- builtins[["input"]]
      builtins[["input"]] <- input
      invisible()
    }

    install_python_initializer <- function(...) {
      namespace <- asNamespace("reticulate")
      replace_binding <- function(name, value) {
        was_locked <- bindingIsLocked(name, namespace)
        if (was_locked) {
          unlockBinding(name, namespace)
        }
        assign(name, value, envir = namespace)
        if (was_locked) {
          lockBinding(name, namespace)
        }
      }

      original_initialize <- get("py_initialize", envir = namespace)
      initialize <- function(python, libpython, pythonhome, ...) {
        rust_owned <<- isTRUE(.Call(
          "mcp_console_initialize_python",
          python,
          libpython,
          pythonhome
        ))
        if (rust_owned) {
          # Do not strand the initial GIL when reticulate errors or interrupts.
          on.exit(finish_python_initialization(), add = TRUE)
        }
        original_initialize(python, libpython, pythonhome, ...)
      }
      replace_binding("py_initialize", initialize)

      original_inject_hooks <- get("py_inject_hooks", envir = namespace)
      inject_hooks <- function() {
        configure_python_input()
        original_inject_hooks()
      }
      replace_binding("py_inject_hooks", inject_hooks)

      if (get("is_python_initialized", envir = namespace)()) {
        rust_owned <<- isTRUE(.Call(
          "mcp_console_load_python_library",
          reticulate::py_config()$libpython
        ))
        if (rust_owned) {
          finish_python_initialization()
          configure_python_input()
        }
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
  envir = base::new.env(parent = base::baseenv())
)
