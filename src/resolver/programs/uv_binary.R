base::local({
  reticulate_path <- base::find.package("reticulate", quiet = TRUE)
  if (!base::length(reticulate_path)) {
    base::quit(save = "no", status = 42L, runLast = FALSE)
  }

  namespace <- base::loadNamespace("reticulate")
  if (!base::exists("uv_binary", envir = namespace, inherits = FALSE)) {
    base::quit(save = "no", status = 43L, runLast = FALSE)
  }

  if (base::identical(base::commandArgs(trailingOnly = TRUE), "--probe")) {
    base::quit(save = "no", status = 0L, runLast = FALSE)
  }

  payload <- Sys.getenv("MCP_CONSOLE_RESOLVER_PAYLOAD")
  if (nzchar(payload) && identical(Sys.getenv("RETICULATE_UV"), "managed")) {
    # The upstream shell installer uses macOS mktemp and shell here-documents,
    # which ignore TMPDIR. Download and unpack inside the enforced workload;
    # no installer receives access to the host temporary directory.
    uv <- file.path(payload, "uv", "bin", "uv")
    if (!file.exists(uv)) {
      platform <- Sys.info()[["sysname"]]
      machine <- Sys.info()[["machine"]]
      architecture <- switch(
        machine,
        arm64 = "aarch64",
        aarch64 = "aarch64",
        x86_64 = "x86_64",
        stop("unsupported uv architecture")
      )
      system <- switch(
        platform,
        Darwin = "apple-darwin",
        Linux = "unknown-linux-gnu",
        stop("unsupported uv platform")
      )
      target <- paste(architecture, system, sep = "-")
      archive <- tempfile(fileext = ".tar.gz")
      unpacked <- tempfile()
      dir.create(unpacked)
      on.exit(unlink(c(archive, unpacked), recursive = TRUE), add = TRUE)
      utils::download.file(
        paste0(
          "https://github.com/astral-sh/uv/releases/latest/download/uv-",
          target,
          ".tar.gz"
        ),
        archive,
        quiet = TRUE,
        mode = "wb"
      )
      utils::untar(archive, exdir = unpacked, tar = "internal")
      dir.create(dirname(uv), recursive = TRUE, showWarnings = FALSE)
      if (!file.rename(file.path(unpacked, paste0("uv-", target), "uv"), uv)) {
        stop("could not install managed uv")
      }
      Sys.chmod(uv, "0755")
    }
  } else {
    uv <- base::get("uv_binary", envir = namespace, inherits = FALSE)()
  }
  if (
    !base::is.character(uv) ||
      base::length(uv) != 1L ||
      base::is.na(uv) ||
      !base::nzchar(uv)
  ) {
    base::quit(save = "no", status = 44L, runLast = FALSE)
  }
  base::writeLines(base::enc2utf8(uv))
})
