use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;

const BRIDGE_INIT: &str = r#"
base::local({
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
    invisible(.Call("mcp_console_plot_started"))
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
}, envir = base::new.env(parent = base::baseenv()))
"#;

struct OutputState {
    active: bool,
    deferred: bool,
    chunks: Vec<Vec<u8>>,
}

static OUTPUT_STATE: Mutex<OutputState> = Mutex::new(OutputState {
    active: false,
    deferred: false,
    chunks: Vec::new(),
});

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_plot_started() -> harp::Result<libr::SEXP> {
    let mut state = OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned");
    assert!(state.active, "managed plot should start during an R cell");
    state.deferred = true;
    unsafe { Ok(libr::R_NilValue) }
}

pub(crate) fn defer_output(bytes: &[u8]) -> bool {
    let mut state = OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned");
    if !state.active || !state.deferred {
        return false;
    }
    state.chunks.push(bytes.to_vec());
    true
}

pub(crate) fn take_deferred_output() -> Vec<Vec<u8>> {
    let mut state = OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned");
    std::mem::take(&mut state.chunks)
}

pub(crate) fn plot_started() -> bool {
    OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned")
        .deferred
}

pub(crate) struct Bridge {
    bridge: crate::r_bridge::Bridge,
    directory: PathBuf,
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        let bridge = crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "R graphics")?;
        let directory = std::env::temp_dir().join("mcp-console-plots");
        Ok(Self { bridge, directory })
    }

    pub(crate) fn begin(&self) -> Result<(), String> {
        self.bridge.call0_integer(c"begin")?;
        let mut state = OUTPUT_STATE
            .lock()
            .expect("R graphics output lock should not be poisoned");
        assert!(!state.active, "R graphics output cell should not be active");
        assert!(
            state.chunks.is_empty(),
            "R graphics output should be empty before a cell"
        );
        state.active = true;
        state.deferred = false;
        Ok(())
    }

    pub(crate) fn finish(&self) -> Result<Vec<String>, String> {
        if self.bridge.call0_integer(c"finish")? == 0 {
            return Ok(Vec::new());
        }
        self.take_ready()
    }

    pub(crate) fn take_ready(&self) -> Result<Vec<String>, String> {
        if !self.directory.try_exists().map_err(|error| {
            format!(
                "failed to inspect managed plot directory `{}`: {error}",
                self.directory.display()
            )
        })? {
            return Ok(Vec::new());
        }

        let mut paths = fs::read_dir(&self.directory)
            .map_err(|error| {
                format!(
                    "failed to read managed plot directory `{}`: {error}",
                    self.directory.display()
                )
            })?
            .map(|entry| entry.map(|entry| entry.path()))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("failed to list managed plot images: {error}"))?;
        paths.sort();

        let mut images = Vec::new();
        for path in paths.into_iter().filter(|path| is_managed_plot(path)) {
            if !path.is_file() || path.extension().and_then(|value| value.to_str()) != Some("png") {
                return Err(format!(
                    "managed plot directory contained unexpected entry `{}`",
                    path.display()
                ));
            }
            let data = fs::read(&path).map_err(|error| {
                format!("failed to read managed plot `{}`: {error}", path.display())
            })?;
            images.push(STANDARD.encode(data));
            fs::remove_file(&path).map_err(|error| {
                format!(
                    "failed to remove managed plot `{}`: {error}",
                    path.display()
                )
            })?;
        }
        Ok(images)
    }

    pub(crate) fn end(&self) {
        let mut state = OUTPUT_STATE
            .lock()
            .expect("R graphics output lock should not be poisoned");
        assert!(state.active, "R graphics output cell should be active");
        assert!(
            state.chunks.is_empty(),
            "R graphics output should be empty after a cell"
        );
        state.active = false;
        state.deferred = false;
    }
}

fn is_managed_plot(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let Some((device, page)) = name
        .strip_prefix("device-")
        .and_then(|name| name.split_once("-page-"))
    else {
        return false;
    };
    let Some(page) = page.strip_suffix(".png") else {
        return false;
    };
    [device, page]
        .into_iter()
        .all(|number| number.len() == 6 && number.bytes().all(|byte| byte.is_ascii_digit()))
}
