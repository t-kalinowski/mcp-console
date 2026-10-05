//! Windows R startup and interrupt callback; idle R event servicing is unsupported.

use std::error::Error;
use std::ffi::{CString, c_char, c_int};
use std::io;
use std::sync::OnceLock;

use super::{mcp_r_read_console, r_busy, r_show_message, r_write_console};

pub(super) extern "C-unwind" fn process_events() {
    super::super::interrupt::deliver_windows_r_interrupt();
}

pub(super) fn initialize_r(
    r_home: &std::path::Path,
    arguments: &mut [*mut c_char],
) -> Result<Option<Option<std::ffi::OsString>>, Box<dyn Error>> {
    use std::mem::MaybeUninit;
    static STARTUP_PATHS: OnceLock<(CString, CString)> = OnceLock::new();

    let r_home = CString::new(r_home.to_string_lossy().as_bytes())?;
    let user_home = ["R_USER", "HOME"]
        .into_iter()
        .filter_map(std::env::var_os)
        .find(|value| !value.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(std::env::home_dir)
        .ok_or_else(|| io::Error::other("cannot determine R user directory"))?;
    let user_home = CString::new(user_home.to_string_lossy().as_bytes())?;
    // Older Windows R versions retain startup path pointers in R_SetParams.
    // R lives until worker exit, so its backing strings must do the same.
    STARTUP_PATHS
        .set((r_home, user_home))
        .map_err(|_| io::Error::other("R startup paths were already initialized"))?;
    let (r_home, user_home) = STARTUP_PATHS.get().expect("R startup paths initialized");
    unsafe {
        libr::set(libr::R_SignalHandlers, 0);
        libr::cmdlineoptions(1, arguments.as_mut_ptr());
        let mut params = MaybeUninit::<libr::structRstart>::uninit();
        libr::R_DefParamsEx(params.as_mut_ptr(), 0);
        let mut params = params.assume_init();
        let mut count = arguments.len() as c_int;
        libr::R_common_command_line(&mut count, arguments.as_mut_ptr(), &mut params);
        params.R_Interactive = 1;
        params.CharacterMode = libr::UImode_RGui;
        params.LoadInitFile = libr::Rboolean_FALSE;
        params.LoadSiteFile = libr::Rboolean_FALSE;
        params.rhome = r_home.as_ptr().cast_mut();
        params.home = user_home.as_ptr().cast_mut();
        params.WriteConsole = None;
        params.WriteConsoleEx = Some(r_write_console);
        params.ReadConsole = Some(mcp_r_read_console);
        params.ShowMessage = Some(r_show_message);
        params.Busy = Some(r_busy);
        params.CallBack = Some(process_events);
        libr::R_SetParams(&mut params);
        libr::graphapp::GA_initapp(0, std::ptr::null_mut());
        libr::readconsolecfg();
    }
    let deferred = crate::python::defer_r_startup()?;
    unsafe {
        libr::setup_Rmainloop();
    }
    Ok(deferred)
}
