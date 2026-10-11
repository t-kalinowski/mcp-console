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
    show_html <- function(path, title = "") {
      text <- .Call("mcp_console_documentation_html", path)
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
