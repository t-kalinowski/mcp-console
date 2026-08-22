base::local({
  input <- base::paste(
    base::readLines(
      base::file("stdin", encoding = "UTF-8"),
      warn = FALSE
    ),
    collapse = "\n"
  )
  input <- jsonlite::fromJSON(input)
  packages <- base::unique(c("numpy", "pandas", input$packages))
  python_version <- input$python_version
  if (!base::length(python_version)) {
    python_version <- NULL
  }
  exclude_newer <- input$exclude_newer
  if (!base::length(exclude_newer)) {
    exclude_newer <- NULL
  }
  messages <- utils::capture.output(
    ignored_output <- utils::capture.output(
      python <- base::try(
        reticulate:::uv_get_or_create_env(
          packages = packages,
          python_version = python_version,
          exclude_newer = exclude_newer
        ),
        silent = TRUE
      ),
      type = "output"
    ),
    type = "message"
  )
  if (base::inherits(python, "try-error")) {
    python_selection <- base::grep(
      "^[[:space:]]*Python:",
      messages,
      value = TRUE
    )
    uv_error <- base::any(base::startsWith(messages, "uv error code: "))
    if (uv_error && base::length(python_selection)) {
      base::cat(
        base::sub(
          "^[[:space:]]*Python:[[:space:]]*",
          "",
          python_selection[[1L]]
        ),
        "\n",
        sep = ""
      )
    } else {
      error <- base::attr(python, "condition")
      base::writeLines(base::conditionMessage(error), con = base::stderr())
    }
    base::quit(save = "no", status = 1L, runLast = FALSE)
  }
  base::stopifnot(
    base::length(python) == 1L,
    !base::is.na(python),
    base::nzchar(python)
  )
  ignored_status <- base::system2(
    python,
    c("-I", "-c", base::shQuote("import matplotlib.font_manager")),
    stdout = FALSE,
    stderr = FALSE
  )
  base::cat(python, "\n", sep = "")
})
