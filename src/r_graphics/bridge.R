base::local(
  {
    active <- FALSE
    devices <- list()
    device_counter <- 0L
    directory <- file.path(Sys.getenv("TMPDIR"), "mcp-console-plots")

    positive_option <- function(name, default) {
      value <- getOption(name)
      if (is.null(value)) {
        return(default)
      }
      if (
        !is.numeric(value) ||
          length(value) != 1L ||
          !is.finite(value) ||
          value <= 0
      ) {
        stop(name, " must be one positive finite number")
      }
      as.numeric(value)
    }

    device_path <- function(device) {
      device <- as.integer(device)
      registry <- get(".Devices", envir = baseenv())
      if (!is.list(registry) || length(registry) < device) {
        return(NULL)
      }
      path <- attr(registry[[device]], "filepath", exact = TRUE)
      if (!is.character(path) || length(path) != 1L || !nzchar(path)) {
        return(NULL)
      }
      path
    }

    managed_device <- function(...) {
      if (!active) {
        stop("managed plot device opened outside an R evaluation")
      }
      if (!dir.exists(directory) && !dir.create(directory, mode = "0700")) {
        stop("failed to create managed plot directory")
      }

      width <- positive_option("console.plot.width", 800 / 96)
      height <- positive_option("console.plot.height", 600 / 96)
      dpi <- positive_option("console.plot.dpi", 96)
      device_counter <<- device_counter + 1L
      path <- file.path(
        directory,
        sprintf("device-%06d-page-%%06d.png", device_counter)
      )
      grDevices::png(
        filename = path,
        width = width,
        height = height,
        units = "in",
        res = dpi
      )

      device <- as.integer(grDevices::dev.cur())
      path <- device_path(device)
      if (is.null(path)) {
        stop("managed plot device did not expose its output path")
      }
      devices[[length(devices) + 1L]] <<- list(device = device, path = path)
      invisible(.Call("mcp_console_plot_started", path))
    }

    begin <- function() {
      stopifnot(!active, length(devices) == 0L)
      device_counter <<- 0L
      active <<- TRUE
      invisible(0L)
    }

    finish <- function() {
      stopifnot(active)
      active <<- FALSE
      opened <- rev(devices)
      devices <<- list()
      for (device in opened) {
        if (identical(device_path(device$device), device$path)) {
          grDevices::dev.off(which = device$device)
        }
      }
      length(opened)
    }

    options(device = managed_device)
    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
