base::local(
  {
    managed <- base::.libPaths()[[1L]]
    in_progress <- base::character()
    maximum_resolution_batch <- 64L

    original_library <- base::library
    original_load_namespace <- base::loadNamespace
    original_require <- base::require
    original_require_namespace <- base::requireNamespace

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

    ensure_r_packages <- function(packages, advisory = FALSE) {
      base::stopifnot(
        base::is.character(packages),
        base::is.logical(advisory),
        base::length(advisory) == 1L,
        !base::is.na(advisory)
      )

      packages <- packages[
        !base::duplicated(packages) &
          base::vapply(packages, is_plain_package_name, base::logical(1L))
      ]
      packages <- packages[
        !base::vapply(packages, package_available, base::logical(1L))
      ]
      packages <- packages[!(packages %in% in_progress)]
      packages <- packages[
        base::seq_len(base::min(
          base::length(packages),
          maximum_resolution_batch
        ))
      ]
      if (base::length(packages) == 0L) {
        return(base::list(kind = "ready"))
      }

      in_progress <<- base::c(in_progress, packages)
      base::on.exit(
        in_progress <<- in_progress[!(in_progress %in% packages)],
        add = TRUE
      )

      response <- .Call("mcp_console_resolve_r", packages)
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
        if (advisory && base::identical(response[[2L]], "host")) {
          return(base::list(kind = "unavailable", message = response[[3L]]))
        }
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

      if (
        !is_plain_package_name(package_name) ||
          package_available(package_name) ||
          package_name %in% in_progress
      ) {
        return(delegate(call, original_library, caller))
      }

      outcome <- ensure_r_packages(package_name)
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

      if (
        base::isNamespaceLoaded(package_name) ||
          package_name %in% in_progress
      ) {
        return(delegate(call, original_load_namespace, caller))
      }

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

      if (
        !is_plain_package_name(package_name) ||
          package_available(package_name)
      ) {
        return(delegate(call, original_load_namespace, caller))
      }

      outcome <- ensure_r_packages(package_name)
      if (base::identical(outcome$kind, "failed")) {
        signal_resolution_failure(outcome)
      }
      delegate(call, original_load_namespace, caller)
    }

    static_call_name <- function(call) {
      head <- call[[1L]]
      if (base::is.symbol(head)) {
        return(base::as.character(head))
      }
      if (
        base::is.call(head) &&
          base::length(head) == 3L &&
          base::is.symbol(head[[1L]]) &&
          base::as.character(head[[1L]]) %in% base::c("::", ":::") &&
          base::is.symbol(head[[2L]]) &&
          base::identical(base::as.character(head[[2L]]), "base") &&
          base::is.symbol(head[[3L]])
      ) {
        return(base::as.character(head[[3L]]))
      }
      ""
    }

    static_match_call <- function(call, definition) {
      base::tryCatch(
        base::match.call(definition, call, expand.dots = FALSE),
        error = function(error) NULL
      )
    }

    is_scalar_string <- function(value) {
      base::is.character(value) &&
        base::length(value) == 1L &&
        !base::is.na(value)
    }

    call_argument <- function(call, name) {
      index <- base::match(name, base::names(call), nomatch = 0L)
      if (index == 0L) NULL else call[[index]]
    }

    has_argument <- function(call, name) {
      name %in% base::names(call)
    }

    scan_r_packages <- function(source) {
      expressions <- base::tryCatch(
        base::parse(text = source, keep.source = FALSE),
        error = function(error) NULL
      )
      if (base::is.null(expressions)) {
        return(base::character())
      }

      packages <- base::character()
      add <- function(package) {
        if (
          is_plain_package_name(package) &&
            !package %in% packages
        ) {
          packages <<- base::c(packages, package)
        }
      }

      walk <- function(node) {
        if (base::is.expression(node)) {
          base::lapply(node, walk)
          return(base::invisible(NULL))
        }
        if (!base::is.call(node)) {
          return(base::invisible(NULL))
        }

        name <- static_call_name(node)
        if (
          name %in%
            base::c(
              "function",
              "quote",
              "substitute",
              "expression",
              "alist",
              "bquote",
              "~"
            )
        ) {
          return(base::invisible(NULL))
        }

        head <- node[[1L]]
        if (
          base::is.call(head) &&
            static_call_name(head) %in% base::c("::", ":::")
        ) {
          walk(head)
        }

        if (name %in% base::c("library", "require")) {
          if (!base::is.null(call_argument(node, "lib.loc"))) {
            return(base::invisible(NULL))
          }
          definition <- if (base::identical(name, "library")) {
            original_library
          } else {
            original_require
          }
          matched <- static_match_call(node, definition)
          if (!base::is.null(matched)) {
            package <- matched[["package"]]
            library_paths <- matched[["lib.loc"]]
            character_only <- matched[["character.only"]]
            if (!base::is.null(library_paths)) {
              package <- NULL
            }
            if (is_scalar_string(package)) {
              add(package)
            } else if (
              base::is.symbol(package) &&
                (base::is.null(character_only) ||
                  base::identical(character_only, FALSE))
            ) {
              add(base::as.character(package))
            }
          }
        } else if (name %in% base::c("requireNamespace", "loadNamespace")) {
          if (
            !base::is.null(call_argument(node, "lib.loc")) ||
              (has_argument(node, "partial") &&
                !base::identical(call_argument(node, "partial"), FALSE))
          ) {
            return(base::invisible(NULL))
          }
          definition <- if (base::identical(name, "requireNamespace")) {
            original_require_namespace
          } else {
            original_load_namespace
          }
          matched <- static_match_call(node, definition)
          if (!base::is.null(matched)) {
            package <- matched[["package"]]
            library_paths <- matched[["lib.loc"]]
            partial <- matched[["partial"]]
            dots <- matched[["..."]]
            if (!base::is.null(dots)) {
              dots <- base::as.list(dots)
              if ("lib.loc" %in% base::names(dots)) {
                library_paths <- dots[["lib.loc"]]
              }
              if ("partial" %in% base::names(dots)) {
                partial <- dots[["partial"]]
              }
            }
            if (
              is_scalar_string(package) &&
                base::is.null(library_paths) &&
                (base::is.null(partial) || base::identical(partial, FALSE))
            ) {
              add(package)
            }
          }
        } else if (name %in% base::c("::", ":::")) {
          package <- node[[2L]]
          if (base::is.symbol(package)) {
            add(base::as.character(package))
          } else if (is_scalar_string(package)) {
            add(package)
          }
        }

        arguments <- base::as.list(node)[-1L]
        base::lapply(arguments, walk)
        base::invisible(NULL)
      }

      walk(expressions)
      packages <- packages[
        !base::vapply(packages, package_available, base::logical(1L))
      ]
      packages[
        base::seq_len(base::min(
          base::length(packages),
          maximum_resolution_batch
        ))
      ]
    }

    preflight <- function(source) {
      packages <- base::tryCatch(
        scan_r_packages(source),
        error = function(error) base::character()
      )
      if (base::length(packages) == 0L) {
        return("")
      }
      outcome <- ensure_r_packages(packages, advisory = TRUE)
      if (base::identical(outcome$kind, "failed")) {
        outcome$message
      } else {
        ""
      }
    }

    replace_base_binding <- function(name, value) {
      environment <- base::baseenv()
      base::stopifnot(base::bindingIsLocked(name, environment))
      base::unlockBinding(name, environment)
      base::on.exit(base::lockBinding(name, environment))
      base::assign(name, value, envir = environment)
    }

    replace_base_binding("library", library_wrapper)
    replace_base_binding("loadNamespace", load_namespace_wrapper)

    base::environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
