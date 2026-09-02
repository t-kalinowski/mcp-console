.onLoad <- function(libname, pkgname) {
  compatibility_warnings <- character()
  namespace <- withCallingHandlers(
    loadNamespace("pkgload"),
    warning = function(warning) {
      compatibility_warnings <<- c(
        compatibility_warnings,
        conditionMessage(warning)
      )
      invokeRestart("muffleWarning")
    }
  )
  stopifnot(
    is.function(get0(
      "makeNamespace",
      envir = namespace,
      inherits = FALSE
    )),
    !any(grepl(
      "pkgload is incompatible with the current version of R.",
      compatibility_warnings,
      fixed = TRUE
    ))
  )

  reticulate::py_require("py-yaml12")
}
