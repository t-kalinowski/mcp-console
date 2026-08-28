base::local(
  {
    finish_python_initialization <- function(...) {
      invisible(.Call("mcp_console_finish_python_initialization"))
    }

    install_python_initializer <- function(...) {
      namespace <- asNamespace("reticulate")
      original_initialize <- get("py_initialize", envir = namespace)
      initialize <- function(python, libpython, pythonhome, ...) {
        invisible(.Call(
          "mcp_console_initialize_python",
          python,
          libpython,
          pythonhome
        ))
        original_initialize(python, libpython, pythonhome, ...)
      }
      was_locked <- bindingIsLocked("py_initialize", namespace)
      if (was_locked) {
        unlockBinding("py_initialize", namespace)
      }
      assign("py_initialize", initialize, envir = namespace)
      if (was_locked) {
        lockBinding("py_initialize", namespace)
      }

      setHook(
        "reticulate.onPyInit",
        finish_python_initialization,
        action = "append"
      )
      if (get("is_python_initialized", envir = namespace)()) {
        invisible(.Call(
          "mcp_console_load_python_library",
          reticulate::py_config()$libpython
        ))
        finish_python_initialization()
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
