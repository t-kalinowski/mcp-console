base::local({
  input <- base::paste(
    base::readLines(
      base::file("stdin", encoding = "UTF-8"),
      warn = FALSE
    ),
    collapse = "\n"
  )
  constraints <- base::unlist(
    jsonlite::fromJSON(input),
    use.names = FALSE
  )
  if (!base::length(constraints)) {
    constraints <- NULL
  }
  version <- base::try(
    reticulate:::resolve_python_version(constraints),
    silent = TRUE
  )
  if (base::inherits(version, "try-error")) {
    error <- base::attr(version, "condition")
    base::writeLines(base::conditionMessage(error), con = base::stderr())
    base::quit(save = "no", status = 1L, runLast = FALSE)
  }
  base::stopifnot(
    base::length(version) == 1L,
    !base::is.na(version),
    base::nzchar(version)
  )
  base::cat(version, "\n", sep = "")
})
