use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use libr::SEXP;

type NewPageCallback = unsafe extern "C-unwind" fn(libr::pGEcontext, libr::pDevDesc);
type CloseCallback = unsafe extern "C-unwind" fn(libr::pDevDesc);

unsafe extern "C-unwind" {
    fn mcp_console_graphics_new_page(context: libr::pGEcontext, pointer: libr::pDevDesc);
    fn mcp_console_graphics_close(pointer: libr::pDevDesc);
}

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
}, envir = base::new.env(parent = base::baseenv()))
"#;

struct ManagedDevice {
    pointer: usize,
    path_prefix: String,
    path_suffix: String,
    page: usize,
    new_page: NewPageCallback,
    close: CloseCallback,
}

impl ManagedDevice {
    fn new(
        pointer: libr::pDevDesc,
        path: String,
        new_page: NewPageCallback,
        close: CloseCallback,
    ) -> Self {
        let (path_prefix, path_suffix) = path
            .rsplit_once("%06d")
            .expect("managed plot path should contain its page placeholder");
        Self {
            pointer: pointer as usize,
            path_prefix: path_prefix.to_string(),
            path_suffix: path_suffix.to_string(),
            page: 0,
            new_page,
            close,
        }
    }

    fn current_page_path(&self) -> Option<PathBuf> {
        (self.page > 0).then(|| {
            PathBuf::from(format!(
                "{}{:06}{}",
                self.path_prefix, self.page, self.path_suffix
            ))
        })
    }
}

struct OutputState {
    active: bool,
    devices: Vec<ManagedDevice>,
}

static OUTPUT_STATE: Mutex<OutputState> = Mutex::new(OutputState {
    active: false,
    devices: Vec::new(),
});

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_plot_started(path: SEXP) -> harp::Result<libr::SEXP> {
    let path = String::try_from(harp::object::RObject::view(path))?;
    unsafe { register_managed_device(path) };
    unsafe { Ok(libr::R_NilValue) }
}

#[unsafe(no_mangle)]
pub extern "C" fn mcp_console_graphics_original_new_page(
    pointer: libr::pDevDesc,
) -> NewPageCallback {
    let state = OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned");
    find_device(&state, pointer).new_page
}

#[unsafe(no_mangle)]
pub extern "C" fn mcp_console_graphics_original_close(pointer: libr::pDevDesc) -> CloseCallback {
    let state = OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned");
    find_device(&state, pointer).close
}

#[unsafe(no_mangle)]
pub extern "C" fn mcp_console_graphics_did_new_page(pointer: libr::pDevDesc) {
    let finalized = {
        let mut state = OUTPUT_STATE
            .lock()
            .expect("R graphics output lock should not be poisoned");
        let index = find_device_index(&state, pointer);
        let finalized = state.devices[index].current_page_path();
        state.devices[index].page += 1;
        finalized
    };
    publish(finalized);
}

#[unsafe(no_mangle)]
pub extern "C" fn mcp_console_graphics_did_close(pointer: libr::pDevDesc) {
    let device = {
        let mut state = OUTPUT_STATE
            .lock()
            .expect("R graphics output lock should not be poisoned");
        let index = find_device_index(&state, pointer);
        state.devices.remove(index)
    };
    publish(device.current_page_path());
}

fn publish(path: Option<PathBuf>) {
    let Some(path) = path else {
        return;
    };
    let image = fs::read(&path)
        .map_err(|error| format!("failed to read managed plot `{}`: {error}", path.display()))
        .and_then(|data| {
            fs::remove_file(&path)
                .map_err(|error| {
                    format!(
                        "failed to remove managed plot `{}`: {error}",
                        path.display()
                    )
                })
                .map(|()| STANDARD.encode(data))
        });
    crate::worker::publish_plot(image);
}

fn find_device(state: &OutputState, pointer: libr::pDevDesc) -> &ManagedDevice {
    &state.devices[find_device_index(state, pointer)]
}

fn find_device_index(state: &OutputState, pointer: libr::pDevDesc) -> usize {
    state
        .devices
        .iter()
        .position(|device| device.pointer == pointer as usize)
        .expect("managed graphics callback should belong to a managed device")
}

unsafe fn register_managed_device(path: String) {
    let pointer = unsafe { current_device() };
    let mut state = OUTPUT_STATE
        .lock()
        .expect("R graphics output lock should not be poisoned");
    assert!(state.active, "managed plot should start during a cell");
    assert!(
        state
            .devices
            .iter()
            .all(|device| device.pointer != pointer as usize),
        "managed graphics device should be registered once"
    );
    let (new_page, close) = unsafe { replace_callbacks(pointer) };
    state
        .devices
        .push(ManagedDevice::new(pointer, path, new_page, close));
}

unsafe fn current_device() -> libr::pDevDesc {
    let graphics_device = unsafe { libr::GEcurrentDevice() };
    assert!(
        !graphics_device.is_null(),
        "managed graphics device should be current"
    );
    match unsafe { libr::R_GE_getVersion() } {
        13 => unsafe {
            (*(graphics_device.cast::<libr::GEDevDescVersion13>())).dev as libr::pDevDesc
        },
        14 => unsafe {
            (*(graphics_device.cast::<libr::GEDevDescVersion14>())).dev as libr::pDevDesc
        },
        15 => unsafe {
            (*(graphics_device.cast::<libr::GEDevDescVersion15>())).dev as libr::pDevDesc
        },
        16 => unsafe {
            (*(graphics_device.cast::<libr::GEDevDescVersion16>())).dev as libr::pDevDesc
        },
        17 => unsafe {
            (*(graphics_device.cast::<libr::GEDevDescVersion17>())).dev as libr::pDevDesc
        },
        version => panic!("R graphics engine version {version} is unsupported"),
    }
}

macro_rules! replace_versioned_callbacks {
    ($pointer:expr, $device:ty) => {{
        let device = $pointer.cast::<$device>();
        let new_page = unsafe {
            (*device)
                .newPage
                .expect("managed PNG device should provide a new-page callback")
        };
        let close = unsafe {
            (*device)
                .close
                .expect("managed PNG device should provide a close callback")
        };
        unsafe {
            (*device).newPage = Some(mcp_console_graphics_new_page);
            (*device).close = Some(mcp_console_graphics_close);
        }
        (new_page, close)
    }};
}

unsafe fn replace_callbacks(pointer: libr::pDevDesc) -> (NewPageCallback, CloseCallback) {
    match unsafe { libr::R_GE_getVersion() } {
        13 => replace_versioned_callbacks!(pointer, libr::DevDescVersion13),
        14 => replace_versioned_callbacks!(pointer, libr::DevDescVersion14),
        15 => replace_versioned_callbacks!(pointer, libr::DevDescVersion15),
        16 => replace_versioned_callbacks!(pointer, libr::DevDescVersion16),
        17 => replace_versioned_callbacks!(pointer, libr::DevDescVersion17),
        version => panic!("R graphics engine version {version} is unsupported"),
    }
}

pub(crate) struct Bridge {
    bridge: crate::r_bridge::Bridge,
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        let bridge = crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "R graphics")?;
        Ok(Self { bridge })
    }

    pub(crate) fn begin(&self) -> Result<(), String> {
        self.bridge.call0_integer(c"begin")?;
        let mut state = OUTPUT_STATE
            .lock()
            .expect("R graphics output lock should not be poisoned");
        assert!(!state.active, "R graphics output cell should not be active");
        assert!(
            state.devices.is_empty(),
            "R graphics devices should be empty before a cell"
        );
        state.active = true;
        Ok(())
    }

    pub(crate) fn finish(&self) -> Result<(), String> {
        let result = self.bridge.call0_integer(c"finish").map(|_| ());
        let mut state = OUTPUT_STATE
            .lock()
            .expect("R graphics output lock should not be poisoned");
        if result.is_err() {
            state.devices.clear();
        } else {
            assert!(
                state.devices.is_empty(),
                "R graphics devices should be empty after a cell"
            );
        }
        state.active = false;
        result
    }
}
