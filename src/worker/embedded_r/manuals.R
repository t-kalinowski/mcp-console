base::local(
  {
    request <- function(
      operation,
      topic = "",
      query = "",
      start = 1L,
      lines = 0L,
      column = 1L,
      limit = 5L
    ) {
      stopifnot(
        is.character(topic),
        length(topic) == 1L,
        !is.na(topic),
        is.character(query),
        length(query) == 1L,
        !is.na(query),
        length(start) == 1L,
        is.finite(start),
        start >= 1,
        start == as.integer(start),
        length(lines) == 1L,
        is.finite(lines),
        lines >= 0,
        lines == as.integer(lines),
        length(column) == 1L,
        is.finite(column),
        column >= 1,
        column == as.integer(column),
        length(limit) == 1L,
        is.finite(limit),
        limit >= 1,
        limit <= 20,
        limit == as.integer(limit)
      )
      result <- .Call(
        "mcp_console_manual",
        enc2utf8(c(
          R.home(),
          file.path(R.home("doc"), "manual"),
          R.version.string,
          operation,
          topic,
          query,
          start,
          lines,
          column,
          limit
        ))
      )
      if (nzchar(result[[2L]])) {
        writeLines(result[[2L]], useBytes = TRUE)
      }
      invisible(result[[1L]])
    }

    console <- base::get(".console", base::as.environment("tools:mcp-console"))
    console$manual <- function(
      topic = "",
      start = 1L,
      lines = 0L,
      column = 1L
    ) {
      request("read", topic, start = start, lines = lines, column = column)
    }
    console$manual_search <- function(
      query,
      manual = "",
      start = 1L,
      limit = 5L
    ) {
      stopifnot(nzchar(query))
      if (nchar(enc2utf8(query), type = "bytes") > 1024L) {
        stop("Manual search queries must be at most 1024 UTF-8 bytes")
      }
      request("search", manual, query, start, limit = limit)
    }

    previous_browser <- getOption("browser")
    options(browser = function(url, ...) {
      if (missing(url)) {
        return(previous_browser(...))
      }
      stopifnot(is.character(url), length(url) == 1L)
      if (startsWith(url, "file://")) {
        # RShowDoc encodes the whole path. Separate the fragment after the
        # manual filename, preserving '#' characters in directory names.
        local <- utils::URLdecode(substring(url, 8L))
        # URLdecode leaves UTF-8 path bytes unmarked under native C locales.
        Encoding(local) <- "UTF-8"
        path <- sub(
          "([/]R-[A-Za-z]+[.]html)#[^/]*$",
          "\\1",
          local,
          ignore.case = TRUE
        )
        directory <- normalizePath(
          file.path(R.home("doc"), "manual"),
          winslash = "/",
          mustWork = FALSE
        )
        if (
          tolower(tools::file_ext(path)) == "html" &&
            identical(
              normalizePath(dirname(path), winslash = "/", mustWork = FALSE),
              directory
            ) &&
            grepl("^R-[A-Za-z]+$", tools::file_path_sans_ext(basename(path)))
        ) {
          manual <- tools::file_path_sans_ext(basename(path))
          anchor <- if (nchar(path) < nchar(local)) {
            substring(local, nchar(path) + 2L)
          } else {
            ""
          }
          # Native RShowDoc appends the manual name when no fragment is given.
          if (identical(anchor, manual)) {
            anchor <- ""
          }
          return(console$manual(
            if (nzchar(anchor)) paste0(manual, "#", anchor) else manual
          ))
        }
      }
      if (is.function(previous_browser)) {
        return(previous_browser(url, ...))
      }
      utils::browseURL(url, browser = previous_browser, ...)
    })

    # R < 4.6 rejects manual fragments before consulting the browser option
    # and defaults to PDF even in interactive sessions. Adapt only installed
    # HTML manuals; keep other requests with the original function.
    if (getRversion() < "4.6.0") {
      original_show_doc <- utils::RShowDoc
      show_doc <- function(what, type = c("pdf", "html", "txt"), package) {
        if (
          !missing(what) &&
            missing(package) &&
            is.character(what) &&
            length(what) == 1L &&
            !is.na(what) &&
            grepl("^R-[A-Za-z]+(#.*)?$", what) &&
            (missing(type) || identical(match.arg(type), "html"))
        ) {
          type <- "html"
          name <- sub("#.*", "", what)
          path <- file.path(R.home("doc"), "manual", paste0(name, ".html"))
          if (file.exists(path)) {
            fragment <- substring(what, nchar(name) + 1L)
            utils::browseURL(paste0(
              "file://",
              utils::URLencode(paste0(path, fragment))
            ))
            return(invisible(path))
          }
        }
        original_show_doc(what, type, package)
      }
      locations <- list(asNamespace("utils"))
      if ("package:utils" %in% search()) {
        locations[[2L]] <- as.environment("package:utils")
      }
      for (where in locations) {
        locked <- bindingIsLocked("RShowDoc", where)
        if (locked) {
          unlockBinding("RShowDoc", where)
        }
        assign("RShowDoc", show_doc, envir = where)
        if (locked) lockBinding("RShowDoc", where)
      }
    }
    invisible(NULL)
  },
  envir = base::new.env(parent = base::baseenv())
)
