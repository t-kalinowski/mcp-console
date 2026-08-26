#' Use MCP Console as an ellmer tool
#'
#' `mcp_console_tool()` resolves the `mcp-console` executable, starts
#' `mcp-console serve`, and returns its `send` tool as an [ellmer::ToolDef].
#' The server process and its persistent R, Python, and DuckDB state live as
#' long as the returned tool remains reachable.
#'
#' @param from A Python package requirement passed to
#'   [reticulate::uv_run_tool()]. The default resolves the latest available
#'   `mcp-console` package; use `"mcp-console==0.0.2"` to pin a version.
#' @return An [ellmer::ToolDef] for [ellmer::Chat]`$register_tool()`.
#' @examples
#' \dontrun{
#' chat <- ellmer::chat_openai()
#' chat$register_tool(mcp_console_tool())
#' chat$chat("Use the console to simulate data and summarize the result.")
#' }
#' @export
mcp_console_tool <- function(from = "mcp-console") {
  if (!is.character(from) || length(from) != 1L || is.na(from) || !nzchar(from)) {
    stop("`from` must be one non-empty string.", call. = FALSE)
  }

  client <- new_mcp_client(resolve_mcp_console(from))
  ready <- FALSE
  on.exit(if (!ready) close_mcp_client(client), add = TRUE)

  mcp_request(
    client,
    "initialize",
    list(
      protocolVersion = "2025-11-25",
      capabilities = json_object(),
      clientInfo = list(
        name = "mcpconsole",
        version = as.character(utils::packageVersion("mcpconsole"))
      )
    )
  )
  mcp_write(client, list(jsonrpc = "2.0", method = "notifications/initialized"))

  tools <- mcp_request(client, "tools/list")$tools
  matches <- vapply(tools, \(x) identical(x$name, "send"), logical(1))
  if (sum(matches) != 1L) {
    stop("mcp-console did not expose exactly one `send` tool.", call. = FALSE)
  }
  metadata <- tools[[which(matches)]]

  send <- function(
    r = NULL,
    python = NULL,
    sql = NULL,
    control = NULL,
    requirements = NULL,
    stdin = NULL,
    timeout_ms = NULL
  ) {
    arguments <- list(
      r = r,
      python = python,
      sql = sql,
      control = control,
      requirements = requirements,
      stdin = stdin,
      timeout_ms = timeout_ms
    )
    arguments <- arguments[!vapply(arguments, is.null, logical(1))]

    if (!is.null(arguments$requirements)) {
      requirements <- arguments$requirements
      requirements <- requirements[!vapply(requirements, is.null, logical(1))]
      requirements <- lapply(requirements, \(x) unname(as.list(x)))
      arguments$requirements <- json_object(requirements)
    }

    result <- mcp_request(
      client,
      "tools/call",
      list(name = "send", arguments = json_object(arguments))
    )
    mcp_contents(result)
  }

  fields <- names(formals(send))
  properties <- metadata$inputSchema$properties
  if (!setequal(names(properties), fields)) {
    stop(
      "The installed mcp-console `send` schema is incompatible with this package.",
      call. = FALSE
    )
  }

  tool <- ellmer::tool(
    send,
    name = metadata$name,
    description = metadata$description,
    arguments = lapply(
      properties[fields],
      \(x) ellmer::TypeJsonSchema(json = x, required = FALSE)
    )
  )
  ready <- TRUE
  tool
}

resolve_mcp_console <- function(from) {
  code <- paste(
    "import os, pathlib, sys",
    "name = 'mcp-console.exe' if os.name == 'nt' else 'mcp-console'",
    "print(pathlib.Path(sys.executable).with_name(name).resolve())",
    sep = "; "
  )
  errors <- tempfile("mcp-console-uv-", fileext = ".log")
  on.exit(unlink(errors), add = TRUE)
  output <- suppressWarnings(uv_run_tool(
    "python",
    c("-c", shQuote(code)),
    from = from,
    stdout = TRUE,
    stderr = errors
  ))

  status <- attr(output, "status")
  if (!is.null(status) && status != 0L) {
    stop_with_errors(
      "Could not resolve mcp-console with reticulate::uv_run_tool().",
      errors
    )
  }
  output <- output[nzchar(output)]
  if (length(output) == 0L || !file.exists(utils::tail(output, 1L))) {
    stop(
      "reticulate::uv_run_tool() did not return an mcp-console path.",
      call. = FALSE
    )
  }
  normalizePath(utils::tail(output, 1L), mustWork = TRUE)
}

new_mcp_client <- function(binary) {
  client <- new.env(parent = emptyenv())
  client$errors <- tempfile("mcp-console-", fileext = ".log")
  client$process <- processx::process$new(
    binary,
    "serve",
    stdin = "|",
    stdout = "|",
    stderr = client$errors,
    cleanup = TRUE,
    cleanup_tree = TRUE,
    encoding = "UTF-8"
  )
  client$id <- 1L
  client$output <- ""
  reg.finalizer(client, close_mcp_client, onexit = TRUE)
  client
}

close_mcp_client <- function(client) {
  process <- client$process
  client$process <- NULL
  if (!is.null(process)) {
    try(close(process$get_input_connection()), silent = TRUE)
    try(process$wait(1000), silent = TRUE)
    try(if (process$is_alive()) process$kill_tree(), silent = TRUE)
  }
  unlink(client$errors)
  invisible()
}

mcp_request <- function(client, method, params = NULL) {
  id <- client$id
  client$id <- id + 1L
  message <- list(jsonrpc = "2.0", id = id, method = method)
  if (!is.null(params)) {
    message$params <- params
  }
  mcp_write(client, message)

  response <- mcp_read(client)
  if (is.null(response$id) || as.integer(response$id) != id) {
    stop("mcp-console sent an unexpected JSON-RPC message.", call. = FALSE)
  }
  if (!is.null(response$error)) {
    stop(
      sprintf(
        "mcp-console JSON-RPC error %s: %s",
        response$error$code,
        response$error$message
      ),
      call. = FALSE
    )
  }
  response$result
}

mcp_write <- function(client, message) {
  process <- client$process
  if (is.null(process) || !process$is_alive()) {
    stop_with_errors("mcp-console is not running.", client$errors)
  }
  payload <- as.character(jsonlite::toJSON(
    message,
    auto_unbox = TRUE,
    null = "null",
    na = "null",
    digits = NA
  ))

  remaining <- process$write_input(payload, sep = "\n")
  while (length(remaining) > 0L) {
    mcp_pump(client, 100)
    if (!process$is_alive()) {
      stop_with_errors(
        "mcp-console stopped while receiving a request.",
        client$errors
      )
    }
    remaining <- process$write_input(remaining)
  }
  invisible()
}

mcp_read <- function(client) {
  repeat {
    newline <- regexpr("\n", client$output, fixed = TRUE)[[1L]]
    if (newline > 0L) {
      line <- substr(client$output, 1L, newline - 1L)
      client$output <- substr(client$output, newline + 1L, nchar(client$output))
      return(jsonlite::fromJSON(sub("\r$", "", line), simplifyVector = FALSE))
    }

    mcp_pump(client, 100)
    if (is.null(client$process) || !client$process$is_alive()) {
      mcp_pump(client, 0)
      if (regexpr("\n", client$output, fixed = TRUE)[[1L]] < 0L) {
        stop_with_errors("mcp-console stopped before replying.", client$errors)
      }
    }
  }
}

mcp_pump <- function(client, timeout) {
  process <- client$process
  if (is.null(process)) {
    return(invisible())
  }
  process$poll_io(timeout)
  output <- process$read_output()
  if (length(output) > 0L && nzchar(output)) {
    client$output <- paste0(client$output, output)
  }
  invisible()
}

mcp_contents <- function(result) {
  content <- result$content
  if (isTRUE(result$isError)) {
    text <- vapply(
      content,
      \(x) if (identical(x$type, "text")) x$text else "",
      character(1)
    )
    text <- paste(text[nzchar(text)], collapse = "\n")
    stop(
      if (nzchar(text)) text else "mcp-console tool call failed.",
      call. = FALSE
    )
  }

  content <- lapply(content, function(x) {
    if (identical(x$type, "text")) {
      ellmer::ContentText(x$text)
    } else if (identical(x$type, "image")) {
      ellmer::ContentImageInline(type = x$mimeType, data = x$data)
    } else {
      stop("mcp-console returned an unsupported content type.", call. = FALSE)
    }
  })

  if (length(content) == 0L) {
    ""
  } else if (length(content) == 1L) {
    content[[1L]]
  } else {
    content
  }
}

json_object <- function(x = list()) {
  if (length(x) == 0L) structure(x, names = character()) else x
}

stop_with_errors <- function(message, path) {
  errors <- if (file.exists(path)) {
    utils::tail(readLines(path, warn = FALSE), 20L)
  } else {
    character()
  }
  if (length(errors) > 0L) {
    message <- paste(c(message, "", errors), collapse = "\n")
  }
  stop(message, call. = FALSE)
}
