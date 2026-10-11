base::local(
  {
    pager <- function(files, header, title, delete.file, encoding = "") {
      base::on.exit(if (delete.file) base::unlink(files))
      for (i in base::seq_along(files)) {
        if (base::nzchar(header[[i]])) {
          base::writeLines(base::enc2utf8(header[[i]]), useBytes = TRUE)
        }
        base::writeLines(
          base::enc2utf8(base::readLines(
            files[[i]],
            warn = FALSE,
            encoding = encoding
          )),
          useBytes = TRUE
        )
      }
      base::invisible(NULL)
    }

    base::options(help_type = "text", pager = pager)
    tools::Rd2txt_options(underline_titles = FALSE, showURLs = TRUE)

    utf8_pager <- function(...) pager(..., encoding = "UTF-8")
    show_html <- function(path, title = "", section = "") {
      text <- .Call("mcp_console_documentation_html", path, section)
      file <- base::tempfile()
      base::on.exit(base::unlink(file))
      base::writeBin(base::charToRaw(text), file)
      selected_pager <- base::getOption("pager")
      if (base::identical(selected_pager, pager)) {
        selected_pager <- utf8_pager
      }
      base::file.show(file, title = title, pager = selected_pager)
      base::invisible(NULL)
    }

    browser <- function(url) {
      parts <- base::strsplit(url, "#", fixed = TRUE)[[1L]]
      location <- utils::URLdecode(parts[[1L]])
      section <- if (base::length(parts) > 1L) {
        utils::URLdecode(parts[[2L]])
      } else {
        ""
      }
      if (base::startsWith(location, "file://")) {
        path <- base::substring(location, 8L)
      } else {
        port <- tools:::httpdPort()
        prefixes <- base::paste0(
          "http://",
          base::c("127.0.0.1", "localhost"),
          ":",
          port,
          "/doc/"
        )
        matched <- base::which(base::startsWith(location, prefixes))
        if (port <= 0L || !base::length(matched)) {
          base::stop(
            "Console browser requires local installed R HTML documentation: ",
            url,
            call. = FALSE
          )
        }
        path <- base::file.path(
          R.home("doc"),
          base::substring(location, base::nchar(prefixes[[matched]]) + 1L)
        )
      }
      path <- base::normalizePath(path, winslash = "/", mustWork = TRUE)
      root <- base::normalizePath(
        R.home("doc"),
        winslash = "/",
        mustWork = TRUE
      )
      if (
        !base::startsWith(path, base::paste0(root, "/")) ||
          !base::tolower(tools::file_ext(path)) %in% base::c("html", "htm")
      ) {
        base::stop(
          "Console browser requires local installed R HTML documentation: ",
          url,
          call. = FALSE
        )
      }
      # RShowDoc() appends the manual name even when no section was requested.
      if (
        base::identical(
          section,
          tools::file_path_sans_ext(base::basename(path))
        )
      ) {
        section <- ""
      }
      show_html(path, section = section)
    }
    base::options(browser = browser)

    print_vignette <- function(x, ...) {
      format <- base::tolower(tools::file_ext(x$PDF))
      if (format %in% base::c("html", "htm")) {
        rendered <- base::file.path(x$Dir, "doc", x$PDF)
        show_html(rendered, title = x$Title)
        return(base::invisible(x))
      } else {
        # PDF, absent rendered artifacts, and other formats use installed source.
        file <- base::file.path(x$Dir, "doc", x$File)
        if (!base::file.exists(file)) {
          base::stop(
            "Installed vignette source is unavailable: ",
            file,
            call. = FALSE
          )
        }
        header <- base::paste("Vignette source:", x$File)
      }
      selected_pager <- base::getOption("pager")
      # Read UTF-8/ASCII text independently of R's current locale.
      if (base::identical(selected_pager, pager)) {
        selected_pager <- utf8_pager
      }
      base::file.show(
        file,
        header = header,
        title = x$Title,
        pager = selected_pager
      )
      base::invisible(x)
    }

    # capture.output() can dispatch in utils' namespace. Follow S3 overrides there.
    utils_namespace <- base::asNamespace("utils")
    base::unlockBinding("print.vignette", utils_namespace)
    base::assign(
      "print.vignette",
      function(x, ...) {
        method <- utils::getS3method(
          "print",
          "vignette",
          envir = base::baseenv()
        )
        method(x, ...)
      },
      envir = utils_namespace
    )
    base::lockBinding("print.vignette", utils_namespace)
    base::registerS3method(
      "print",
      "vignette",
      print_vignette,
      envir = base::baseenv()
    )
    base::invisible(NULL)
  },
  envir = base::new.env(parent = base::baseenv())
)
