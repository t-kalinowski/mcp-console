base::local(
  {
    managed <- base::.libPaths()[[1L]]
    in_progress <- base::character()
    dynamic_resolution <- base::identical(
      base::Sys.getenv(
        "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION",
        unset = "1"
      ),
      "1"
    )

    original_library <- base::library
    original_load_namespace <- base::loadNamespace

    is_plain_package_name <- function(package) {
      base::is.character(package) &&
        base::length(package) == 1L &&
        !base::is.na(package) &&
        base::grepl(
          "^[A-Za-z](?:[A-Za-z0-9.]*[A-Za-z0-9])?\\z",
          package,
          perl = TRUE,
          useBytes = TRUE
        )
    }

    package_available <- function(package) {
      base::paste0("package:", package) %in%
        base::search() ||
        base::isNamespaceLoaded(package) ||
        base::length(base::find.package(package, quiet = TRUE)) != 0L
    }

    apply_managed_library <- function(library) {
      library <- base::normalizePath(
        library,
        winslash = "/",
        mustWork = TRUE
      )
      paths <- base::.libPaths()
      base::.libPaths(base::c(library, paths[paths != managed]))
      if (!base::identical(base::.libPaths()[[1L]], library)) {
        base::stop("resolved R library was not added to .libPaths()")
      }
      managed <<- library
      library
    }

    prepare <- function(library) {
      result <- base::tryCatch(
        {
          library <- apply_managed_library(library)
          base::list(kind = "prepared", library = library)
        },
        error = function(error) {
          base::list(kind = "failed", message = base::conditionMessage(error))
        }
      )
      jsonlite::toJSON(
        result,
        auto_unbox = TRUE,
        null = "null",
        na = "null"
      )
    }

    activate_managed_library <- function(library) {
      base::suspendInterrupts({
        applied <- base::tryCatch(
          apply_managed_library(library),
          error = base::identity
        )
        if (base::inherits(applied, "error")) {
          message <- base::conditionMessage(applied)
          base::invisible(
            .Call("mcp_console_r_activation_failed", library, message)
          )
          base::list(
            kind = "failed",
            failure = "activation",
            message = message
          )
        } else {
          base::invisible(.Call("mcp_console_r_activated", applied))
          base::list(kind = "ready")
        }
      })
    }

    ensure_r_package <- function(package) {
      base::stopifnot(is_plain_package_name(package))
      if (package_available(package) || package %in% in_progress) {
        return(base::list(kind = "ready"))
      }

      in_progress <<- base::c(in_progress, package)
      base::on.exit(
        in_progress <<- in_progress[in_progress != package],
        add = TRUE
      )

      response <- .Call("mcp_console_resolve_r", package)
      if (
        !base::is.character(response) ||
          base::anyNA(response) ||
          !base::length(response) %in% 2:3
      ) {
        base::stop("invalid R environment resolver response")
      }

      if (
        base::length(response) == 2L &&
          base::identical(response[[1L]], "resolved")
      ) {
        return(activate_managed_library(response[[2L]]))
      }

      if (
        base::length(response) == 3L &&
          base::identical(response[[1L]], "failed") &&
          response[[2L]] %in% base::c("host", "interrupted")
      ) {
        return(base::list(
          kind = "failed",
          failure = response[[2L]],
          message = response[[3L]]
        ))
      }

      base::stop("invalid R environment resolver response")
    }

    rewrite_argument <- function(call, name, value) {
      call[[name]] <- value
      call
    }

    delegate <- function(call, original, caller) {
      call[[1L]] <- original
      base::eval(call, envir = caller)
    }

    signal_resolution_failure <- function(outcome) {
      if (base::identical(outcome$failure, "interrupted")) {
        condition <- base::structure(
          base::list(message = outcome$message, call = NULL),
          class = base::c("interrupt", "condition")
        )
        base::stop(condition)
      }
      base::stop(outcome$message, call. = FALSE)
    }

    library_wrapper <- function(
      package,
      help,
      pos = 2,
      lib.loc = NULL,
      character.only = FALSE,
      logical.return = FALSE,
      warn.conflicts,
      quietly = FALSE,
      verbose = getOption("verbose"),
      mask.ok,
      exclude,
      include.only,
      attach.required = missing(include.only)
    ) {
      call <- base::match.call(expand.dots = FALSE)
      caller <- base::parent.frame()
      if (base::missing(package)) {
        return(delegate(call, original_library, caller))
      }

      library_paths <- lib.loc
      if (!base::missing(lib.loc)) {
        call <- rewrite_argument(call, "lib.loc", library_paths)
      }
      if (!base::is.null(library_paths)) {
        return(delegate(call, original_library, caller))
      }

      use_character <- character.only
      if (!base::missing(character.only)) {
        call <- rewrite_argument(
          call,
          "character.only",
          use_character
        )
      }
      if (!use_character) {
        package_name <- base::as.character(base::substitute(package))
      } else {
        package_name <- package
        call <- rewrite_argument(call, "package", package_name)
      }

      if (!is_plain_package_name(package_name)) {
        return(delegate(call, original_library, caller))
      }

      outcome <- ensure_r_package(package_name)
      if (base::identical(outcome$kind, "failed")) {
        if (base::identical(outcome$failure, "host")) {
          logical_return <- logical.return
          if (!base::missing(logical.return)) {
            call <- rewrite_argument(
              call,
              "logical.return",
              logical_return
            )
          }
          if (logical_return) {
            return(delegate(call, original_library, caller))
          }
        }
        signal_resolution_failure(outcome)
      }
      delegate(call, original_library, caller)
    }

    load_namespace_wrapper <- function(
      package,
      lib.loc = NULL,
      keep.source = getOption("keep.source.pkgs"),
      partial = FALSE,
      versionCheck = NULL,
      keep.parse.data = getOption("keep.parse.data.pkgs")
    ) {
      call <- base::match.call(expand.dots = FALSE)
      caller <- base::parent.frame()
      package_value <- package
      package_name <- base::as.character(package_value)[[1L]]
      if (
        !base::is.null(base::attr(
          package_value,
          "LibPath",
          exact = TRUE
        ))
      ) {
        call <- rewrite_argument(call, "package", package_value)
        return(delegate(call, original_load_namespace, caller))
      }
      call <- rewrite_argument(call, "package", package_name)

      library_paths <- lib.loc
      if (!base::missing(lib.loc)) {
        call <- rewrite_argument(call, "lib.loc", library_paths)
      }
      if (!base::is.null(library_paths)) {
        return(delegate(call, original_load_namespace, caller))
      }

      partial_load <- partial
      if (!base::missing(partial)) {
        call <- rewrite_argument(call, "partial", partial_load)
      }
      if (!base::identical(partial_load, FALSE)) {
        return(delegate(call, original_load_namespace, caller))
      }

      if (!is_plain_package_name(package_name)) {
        return(delegate(call, original_load_namespace, caller))
      }

      outcome <- ensure_r_package(package_name)
      if (base::identical(outcome$kind, "failed")) {
        signal_resolution_failure(outcome)
      }
      delegate(call, original_load_namespace, caller)
    }

    replace_base_binding <- function(name, value) {
      environment <- base::baseenv()
      base::stopifnot(base::bindingIsLocked(name, environment))
      base::unlockBinding(name, environment)
      base::on.exit(base::lockBinding(name, environment))
      base::assign(name, value, envir = environment)
    }

    if (dynamic_resolution) {
      replace_base_binding("library", library_wrapper)
      replace_base_binding("loadNamespace", load_namespace_wrapper)
    }

    base::environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
