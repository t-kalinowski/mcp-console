default_packages <- getOption("defaultPackages")
stopifnot(is.character(default_packages), length(default_packages) > 0)

r_home <- R.home()
stopifnot(is.character(r_home), nzchar(r_home), dir.exists(r_home))

r_home_paths <- c(
  home = R.home(),
  share = R.home("share"),
  include = R.home("include"),
  doc = R.home("doc"),
  library = R.home("library")
)
stopifnot(all(nzchar(r_home_paths)), all(dir.exists(r_home_paths)))

loaded <- vapply(default_packages, requireNamespace, logical(1), quietly = TRUE)
if (!all(loaded)) {
  missing <- default_packages[!loaded]
  stop(
    sprintf(
      "R default packages unavailable: %s",
      paste(missing, collapse = ", ")
    ),
    call. = FALSE
  )
}

cat(sprintf("R executable: %s\n", Sys.which("R")))
cat(sprintf("R home: %s\n", r_home))
cat(sprintf("R version: %s\n", paste(R.version$major, R.version$minor, sep = ".")))
cat(sprintf("Default packages: %s\n", paste(default_packages, collapse = ", ")))
cat(".libPaths():\n")
for (path in .libPaths()) {
  cat(sprintf("  - %s\n", path))
}
cat("sessionInfo():\n")
print(sessionInfo())
