function(state) {
  base::eval(
    base::quote(
      {
        original_console_sql_connection <- console_sql_connection

        console_sql_connection <- function(connection) {
          selected <- original_console_sql_connection(connection)
          invisible(.Call("mcp_console_sql_use_r"))
          invisible(selected)
        }

        restore_managed_connection <- function() {
          invisible(original_console_sql_connection(NULL))
          1L
        }

        base::assign(
          "console_sql_connection",
          console_sql_connection,
          envir = tools
        )

        install_python_sql_runtime <- function(...) {
          invisible(.Call("mcp_console_install_python_sql_runtime"))
        }

        register_python_sql_runtime <- function(...) {
          namespace <- asNamespace("reticulate")
          base::setHook(
            "reticulate.onPyInit",
            install_python_sql_runtime,
            action = "append"
          )
          if (get("is_python_initialized", envir = namespace)()) {
            install_python_sql_runtime()
          }
          invisible()
        }

        base::setHook(
          base::packageEvent("reticulate", "onLoad"),
          register_python_sql_runtime,
          action = "append"
        )
        if ("reticulate" %in% loadedNamespaces()) {
          register_python_sql_runtime()
        }
      }
    ),
    envir = state
  )
  invisible(state)
}
