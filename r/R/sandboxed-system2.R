#' Run a system command in a Console sandbox
#'
#' Calls `mcp-console sandbox` with a [SandboxPolicy()], using [base::system2()]
#' for the launch, streams, output capture, status, and timeout. Automatic
#' global and project configuration discovery is disabled. The supplied object
#' is interpreted against Console's built-in standalone-sandbox defaults.
#'
#' @section Arguments and the sandbox boundary:
#' As in `system2()`, `args` contains already-quoted command-line fragments,
#' not an argv vector. On Unix, the command, `env`, and `args` are evaluated by
#' `/bin/sh -c` **inside** the sandbox. Pipes, substitutions, and redirections
#' in those fragments therefore do not execute in the launcher's host shell.
#' On Windows, the target is launched directly; `env` is passed after the
#' command, as in Windows `system2()`. It only works with targets such as R
#' and make that understand those command-line assignments. No `cmd.exe`
#' shell is added. Nonstandard Windows command-line parsers may differ after
#' Console's argv round trip.
#'
#' `stdin`, `stdout`, `stderr`, and the temporary file for `input` are opened
#' on the host by R/the launcher. They intentionally convey those streams to
#' the sandbox, even when the policy denies opening the same paths. Granting
#' an output file is therefore a caller-authorized write, not a sandbox escape.
#' Console diagnostics share stderr with the target.
#'
#' The timeout covers the launcher as well as the target. Process retirement
#' is owned by Console's native runner; this wrapper adds no cleanup guarantee
#' beyond that backend. Executable resolution through uv, when needed, happens
#' on the host before this timeout starts. With `wait = FALSE`, the return code
#' only describes launching; it does not confirm sandbox setup or target
#' success. `input` requires `wait = TRUE`, so R cannot delete its temporary
#' input file before the asynchronous launcher has opened it.
#' Caller-death supervision is not requested: commands can outlive the R
#' process that launched them.
#'
#' This requires a Console executable that supports the current application
#' sandbox schema and `--no-config`. Old executables fail rather than falling
#' back to unsandboxed execution.
#'
#' @inheritParams base::system2
#' @param ... Must be empty. Reserved for future use.
#' @param sandbox A [SandboxPolicy()]. The default requests Console's
#'   built-in policy; `NULL` and `FALSE` are not accepted.
#' @inheritParams console_tool
#' @return The result of [base::system2()]: captured character output, or an
#'   invisible status code, with its usual warnings and status attributes.
#' @examples
#' \dontrun{
#' sandboxed_system2(
#'   file.path(R.home("bin"), "Rscript"),
#'   c("--vanilla", "-e", shQuote("cat(1 + 1, '\\n')")),
#'   stdout = TRUE,
#'   sandbox = SandboxPolicy()
#' )
#' }
#' @export
sandboxed_system2 <- function(
  command,
  args = character(),
  stdout = "",
  stderr = "",
  stdin = "",
  input = NULL,
  env = character(),
  wait = TRUE,
  minimized = FALSE,
  invisible = TRUE,
  timeout = 0,
  receive.console.signals = wait,
  ...,
  sandbox = SandboxPolicy(),
  path = NULL,
  version = NULL
) {
  if (...length() != 0L) {
    stop("`...` must be empty.", call. = FALSE)
  }
  check_sandbox_policy(sandbox)
  if (!is.null(input) && identical(wait, FALSE)) {
    stop("`input` requires `wait = TRUE`.", call. = FALSE)
  }
  if (
    !is.character(command) ||
      length(command) != 1L ||
      is.na(command) ||
      !nzchar(command)
  ) {
    stop("`command` must be one non-empty string.", call. = FALSE)
  }
  if (!is.character(args) || anyNA(args) || !is.character(env) || anyNA(env)) {
    stop(
      "`args` and `env` must be character vectors without NA.",
      call. = FALSE
    )
  }
  if (
    !missing(receive.console.signals) &&
      !"receive.console.signals" %in% names(formals(base::system2))
  ) {
    stop(
      "This R version does not support `receive.console.signals`.",
      call. = FALSE
    )
  }

  # Resolve before launching; never let the target's env choose the launcher.
  binary <- resolve_mcp_console(path, version)
  prefix <- c("sandbox", "--no-config", sandbox_cli_arguments(sandbox), "--")
  if (.Platform$OS.type == "windows") {
    # system2() does not use a shell on Windows. Its env entries belong to the
    # target command line, not the Console command line or launcher environment.
    launch_args <- c(shQuote(prefix), shQuote(command), env, args)
  } else {
    target <- paste(c(env, shQuote(command), args), collapse = " ")
    # Every outer-shell argument is literal. In particular, do not append raw
    # args to the Console launch: a semicolon would then run on the host.
    launch_args <- shQuote(c(prefix, "/bin/sh", "-c", target))
  }
  call <- list(
    command = binary,
    args = launch_args,
    stdout = stdout,
    stderr = stderr,
    stdin = stdin,
    input = input,
    wait = wait,
    timeout = timeout
  )
  # Preserve missingness so ordinary Unix calls do not emit Windows-only
  # argument messages, and older R versions need not know the signals argument.
  if (!missing(minimized)) {
    call$minimized <- minimized
  }
  if (!missing(invisible)) {
    call$invisible <- invisible
  }
  if (!missing(receive.console.signals)) {
    call$receive.console.signals <- receive.console.signals
  }
  do.call(base::system2, call)
}
