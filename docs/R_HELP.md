# Read installed R documentation

Submit these R cells through Console's `send(r=...)` interface to retrieve help from the packages installed in the session.
Start with the ordinary qualified lookup on macOS and Linux, or the scoped text pager on Windows; capture, discovery, installed-version provenance, and precise paging are optional additions.
These cells do not install packages or run the examples printed in a help page.

## Read an installed topic

In a default macOS or Linux Console session, R's ordinary qualified [`help()`](https://stat.ethz.ch/R-manual/R-devel/library/utils/html/help.html) lookup displays installed documentation as text:

<!-- recipe: ordinary-topic -->
```r
utils::help("mean", package = "base", help_type = "text")
```

On Windows, use the [scoped text pager](#choose-a-text-pager) below.
R's default Windows pager opens a GUI window and does not return the help text through Console.

Specify the package to select the topic without an ambiguity menu.
Operators are quoted strings, for example `utils::help("[", package = "base", help_type = "text")`.
Console bounds the response to its [8 KiB text budget](API.md#output-limits); use the optional capture and paging recipes below to inspect a long page in smaller windows.

For the installed package version, submit a separate cell:

<!-- recipe: quick-version -->
```r
utils::packageVersion("base", lib.loc = .libPaths())
```

## Choose a text pager

On Windows, or if the session has a custom pager, this small scoped option writes the rendered text directly to Console and restores the prior pager afterward:

<!-- recipe: text-pager -->
```r
local({
  old <- options(pager = function(files, header, title, delete.file) {
    writeLines(enc2utf8(readLines(files, warn = FALSE)), useBytes = TRUE)
    if (delete.file) unlink(files)
  })
  on.exit(options(old))
  print(utils::help("mean", package = "base", help_type = "text"))
})
```

This uses the public [`file.show()`](https://stat.ethz.ch/R-manual/R-devel/library/base/html/file.show.html) pager interface.
Explicit text mode avoids HTML and PDF rendering.

## Optional: retain a bounded excerpt

To retain the complete page in a session variable while displaying a short excerpt, submit this cell for `stats::lm`.
Change `package` and `topic` for another qualified topic, including operators such as `package <- "base"` and `topic <- "["`.
It captures the rendered file with a scoped pager and uses the public [`Rd2txt_options()`](https://stat.ethz.ch/R-manual/R-devel/library/tools/html/Rd2txt_options.html) formatting interface.

<!-- recipe: quick-topic -->
```r
help_text <- local({
  package <- "stats"
  topic <- "lm"
  path <- find.package(package, lib.loc = .libPaths(), quiet = TRUE)
  if (!length(path)) {
    stop(paste("Package is not installed:", package), call. = FALSE)
  }
  text <- character()
  old <- options(pager = function(files, header, title, delete.file) {
    text <<- enc2utf8(readLines(files, warn = FALSE))
    if (delete.file) unlink(files)
  })
  on.exit(options(old))
  old_format <- tools::Rd2txt_options(width = 80L, underline_titles = FALSE)
  on.exit(tools::Rd2txt_options(old_format), add = TRUE)
  page <- utils::help(
    (topic),
    package = (package),
    lib.loc = dirname(path),
    help_type = "text",
    try.all.packages = FALSE,
    verbose = FALSE
  )
  if (length(page) != 1L) {
    stop("Expected one installed help page", call. = FALSE)
  }
  print(page)
  writeLines(
    substr(paste(head(text, 8L), collapse = "\n"), 1L, 1500L),
    useBytes = TRUE
  )
  writeLines("[Excerpt; full text retained in help_text.]")
  text
})
```

The excerpt contains at most eight lines and 1,500 Unicode characters, or at most 6,000 UTF-8 bytes, plus the notice.
Submit it alone so other output does not consume Console's [8 KiB response budget](API.md#output-limits).
The complete page stays in `help_text`, and the prior pager and text-format settings are restored on success or error.
Missing packages and topics fail without package preparation or a selection menu.

For candidate discovery, use the search cell under [Discover an ambiguous topic](#discover-an-ambiguous-topic); then put the selected package and topic into either lookup above.
Use the optional recipe below when you need full provenance, a package index, or later line windows.

## Optional: full provenance and precise paging

Submit this cell once if you need the additions below.
These functions are ordinary session definitions; Console does not install a help package or maintain a documentation corpus.

<!-- recipe: definitions -->
```r
read_r_help <- function(package, topic = NULL) {
  stopifnot(
    is.character(package),
    length(package) == 1L,
    !is.na(package),
    is.null(topic) ||
      (is.character(topic) && length(topic) == 1L && !is.na(topic))
  )
  package_path <- find.package(package, lib.loc = .libPaths(), quiet = TRUE)
  if (length(package_path) == 0L) {
    stop(paste("Package is not installed:", package), call. = FALSE)
  }
  library <- dirname(package_path)
  text <- character()
  old_options <- options(pager = function(files, header, title, delete.file) {
    stopifnot(length(files) == 1L)
    text <<- enc2utf8(readLines(files, warn = FALSE))
    if (delete.file) unlink(files)
  })
  on.exit(options(old_options), add = TRUE)
  old_format <- tools::Rd2txt_options(width = 80L, underline_titles = FALSE)
  on.exit(tools::Rd2txt_options(old_format), add = TRUE)
  if (is.null(topic)) {
    page <- utils::help(
      package = (package),
      lib.loc = library,
      help_type = "text"
    )
  } else {
    page <- utils::help(
      (topic),
      package = (package),
      lib.loc = library,
      help_type = "text",
      try.all.packages = FALSE,
      verbose = FALSE
    )
    if (length(page) != 1L) {
      stop(
        sprintf(
          "Expected one help page for %s::%s; found %d",
          package,
          topic,
          length(page)
        ),
        call. = FALSE
      )
    }
  }
  print(page)
  stopifnot(length(text) > 0L)
  list(
    package = package,
    topic = if (is.null(topic)) "<index>" else topic,
    version = as.character(utils::packageVersion(package, lib.loc = library)),
    r_version = as.character(getRversion()),
    library = library,
    text = text
  )
}

show_r_help <- function(page, from = 1L, n = 40L, bytes = 6000L) {
  stopifnot(
    length(from) == 1L,
    from == as.integer(from),
    from >= 1L,
    from <= length(page$text),
    length(n) == 1L,
    n == as.integer(n),
    n >= 1L,
    length(bytes) == 1L,
    bytes == as.integer(bytes),
    bytes >= 1L,
    bytes <= 6000L
  )
  header <- enc2utf8(c(
    sprintf(
      "%s::%s | package %s | R %s",
      page$package,
      page$topic,
      page$version,
      page$r_version
    ),
    paste("Library:", page$library)
  ))
  # Reserve space for provenance, the range, and continuation notices.
  bytes <- min(bytes, 8000L - sum(nchar(header, type = "bytes") + 1L) - 200L)
  stopifnot(bytes > 0L)
  lines <- page$text[seq.int(from, min(from + n - 1L, length(page$text)))]
  count <- sum(cumsum(nchar(lines, type = "bytes") + 1L) <= bytes)
  shortened <- count == 0L
  if (shortened) {
    # A single oversized line is shortened at a UTF-8 character boundary.
    characters <- strsplit(lines[1L], "", fixed = TRUE)[[1L]]
    keep <- sum(cumsum(nchar(characters, type = "bytes")) <= bytes - 1L)
    lines <- substr(lines[1L], 1L, keep)
    count <- 1L
  } else {
    lines <- lines[seq_len(count)]
  }
  last <- from + count - 1L
  # Preserve the normalized UTF-8 bytes even in a non-UTF-8 R locale.
  writeLines(
    c(
      header,
      sprintf("Lines %d-%d of %d", from, last, length(page$text)),
      lines
    ),
    useBytes = TRUE
  )
  if (shortened) {
    writeLines("Line shortened; the full line remains in page$text.")
  }
  if (last < length(page$text)) {
    writeLines(sprintf("Next line: %d", last + 1L))
  } else {
    writeLines("End of help.")
  }
  invisible(last + 1L)
}
```

`read_r_help()` uses the first installed copy on `.libPaths()` and uses that same library for help and version provenance.
Parentheses around variable package arguments prevent R from interpreting the variable's name as a package name.
The pager and text-format settings are restored when retrieval finishes or fails.

`show_r_help()` prints at most 6,000 UTF-8 bytes of body text and reduces that allowance when provenance needs more room, keeping the display below Console's [8 KiB response budget](API.md#output-limits).
Submit one display per cell so other output does not consume that room.
The returned page retains the complete rendered text in `page$text`; a shortened oversized line can be inspected there.

## Retrieve a qualified topic or operator

Select the package explicitly, then display a short excerpt:

<!-- recipe: qualified -->
```r
lm_page <- read_r_help("stats", "lm")
show_r_help(lm_page, n = 8L)
```

The output begins with `stats::lm`, the installed stats version, the R version, the selected library, and the displayed line range.
The text includes “Fitting Linear Models”.

Operators are quoted topic strings:

<!-- recipe: operator -->
```r
extract_page <- read_r_help("base", "[")
show_r_help(extract_page, n = 8L)
```

## Discover an ambiguous topic

Assign search results before inspecting them.
Printing an ambiguous `help()` object can present an interactive selection menu.
For example, `plot` has help in both base and graphics:

<!-- recipe: candidates -->
```r
# R can turn non-native titles into NA with use_UTF8 = TRUE.
search <- utils::help.search(
  "^plot$",
  fields = "alias",
  agrep = FALSE,
  package = c("base", "graphics"),
  lib.loc = .libPaths(),
  types = "help"
)
matches <- search$matches
candidates <- sprintf(
  "%s::%s | %s",
  matches$Package,
  matches$Topic,
  matches$Title
)
writeLines(substr(head(candidates, 10L), 1L, 120L), useBytes = TRUE)
if (length(candidates) > 10L) {
  writeLines("More candidates remain in matches.")
}
```

This displays at most ten candidates, each shortened to 120 characters.
Change the pattern or package selection to search other installed documentation.
Choose a package/topic pair from `matches`, then retrieve it explicitly:

<!-- recipe: selected -->
```r
plot_page <- read_r_help("base", "plot")
show_r_help(plot_page, n = 8L)
```

## Read a package index

Omit the topic to capture the installed package's text index:

<!-- recipe: index -->
```r
index_page <- read_r_help("stats")
index_start <- match(gettext("Index:", domain = "R-base"), index_page$text)
show_r_help(index_page, from = index_start, n = 8L)
```

The complete index is retained in `index_page$text` and can be paged like a topic.

## Page through long help

A small byte budget makes the continuation visible without emitting the entire manual:

<!-- recipe: long-page -->
```r
quotes_page <- read_r_help("base", "Quotes")
next_line <- show_r_help(
  quotes_page,
  n = length(quotes_page$text),
  bytes = 240L
)
```

Submit a later cell to continue from the returned line number:

<!-- recipe: later-page -->
```r
next_line <- show_r_help(quotes_page, from = next_line, n = 8L, bytes = 240L)
```

Stop when the display says `End of help.` Paging uses the retained text; it does not render the page again or execute its examples.

## Missing documentation and limits

Retrieval fails if the package is absent; it does not declare dependencies or request package preparation:

<!-- recipe: missing-package -->
```r
read_r_help("mcpconsolemissinghelppackage", "lm")
```

A missing topic also produces an error rather than a search menu or an arbitrary substitute:

<!-- recipe: missing-topic -->
```r
read_r_help("stats", "mcpconsolemissinghelptopic")
```

These recipes request text only, without starting R's HTTP help server, a browser, or PDF rendering.
They render trusted installed-package documentation through R's help system.
R can evaluate render-time documentation macros; retrieving help does not prohibit all package code execution.
The recipes do not call private Rd lookup functions or invoke `example()`.
Manual wording and line counts can differ across installed R and package versions.
