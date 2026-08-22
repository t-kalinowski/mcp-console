base::local(
  {
    managed <- base::.libPaths()[[1L]]

    prepare <- function(library) {
      result <- base::tryCatch(
        {
          library <- base::normalizePath(
            library,
            winslash = "/",
            mustWork = TRUE
          )
          paths <- base::.libPaths()
          base::.libPaths(base::c(library, paths[paths != managed]))
          if (!base::identical(base::.libPaths()[[1L]], library)) {
            base::stop("resolved R library was not added to .libPaths()")
          }
          managed <<- library
          base::list(kind = "prepared", library = library)
        },
        error = function(error) {
          base::list(kind = "failed", message = base::conditionMessage(error))
        }
      )
      jsonlite::toJSON(
        result,
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    base::environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
