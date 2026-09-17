base::local(
  {
    rust_owned <- FALSE

    finish_python_initialization <- function() {
      invisible(.Call("mcp_console_finish_python_initialization"))
    }

    install_console_services <- function() {
      invisible(.Call(
        "mcp_console_install_python_services",
        reticulate::py_config()$libpython
      ))
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
        original_inject_hooks()
        # Install after reticulate's input hook, including R-first startup.
        install_console_services()
      }
      replace_binding("py_inject_hooks", inject_hooks)
      # Reticulate reinstalls its interrupt handler after injecting hooks.
      # Keep its event polling, but restore Console's delivery/acknowledgment.
      replace_binding("install_interrupt_handlers", install_console_services)

      if (get("is_python_initialized", envir = namespace)()) {
        rust_owned <<- isTRUE(.Call(
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
  envir = base::new.env(parent = base::baseenv())
)
