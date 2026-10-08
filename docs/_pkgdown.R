output_dir <- Sys.getenv("QUARTO_PROJECT_OUTPUT_DIR")
stopifnot(nzchar(output_dir))

pkgdown::build_site_github_pages(
  pkg = "../r",
  dest_dir = file.path(normalizePath(output_dir, mustWork = TRUE), "r"),
  install = TRUE,
  examples = FALSE
)
