function(source) {
  tryCatch(
    suppressWarnings(str2expression(source)),
    error = function(error) {
      error$call <- NULL
      stop(error)
    }
  )
  NULL
}
