use std::path::{Path, PathBuf};
use std::sync::Mutex;

static PYTHON_LIBRARY: Mutex<Option<LoadedLibrary>> = Mutex::new(None);

type PyIsInitialized = unsafe extern "C" fn() -> libc::c_int;
type PySetProgramName = unsafe extern "C" fn(*const libc::wchar_t);
type PySetPythonHome = unsafe extern "C" fn(*const libc::wchar_t);
type PyInitializeEx = unsafe extern "C" fn(libc::c_int);
type PySysSetArgv = unsafe extern "C" fn(libc::c_int, *mut *mut libc::wchar_t);
type PyEvalSaveThread = unsafe extern "C" fn() -> *mut libc::c_void;

struct LoadedLibrary {
    path: PathBuf,
    _library: libloading::os::unix::Library,
    api: PythonApi,
    interpreter: Interpreter,
    configuration: Option<Configuration>,
}

struct PythonApi {
    is_initialized: PyIsInitialized,
    set_program_name: PySetProgramName,
    set_python_home: PySetPythonHome,
    initialize_ex: PyInitializeEx,
    set_argv: PySysSetArgv,
    save_thread: PyEvalSaveThread,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Interpreter {
    Uninitialized,
    External,
    RustOwned { gil_released: bool },
}

struct Configuration {
    program_name: String,
    python_home: String,
    program_name_wide: Vec<libc::wchar_t>,
    python_home_wide: Vec<libc::wchar_t>,
}

pub(super) fn load(path: &Path) -> Result<(), String> {
    with_library(path, LoadedLibrary::attach)
}

pub(super) fn initialize(
    path: &Path,
    program_name: &str,
    python_home: &str,
) -> Result<(), String> {
    with_library(path, |library| {
        library.initialize(program_name, python_home)
    })
}

pub(super) fn finish_initialization() -> Result<(), String> {
    let mut library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    let library = library_slot
        .as_mut()
        .ok_or_else(|| "Python shared library is not loaded".to_string())?;
    library.finish_initialization()
}

fn with_library<T>(
    path: &Path,
    operation: impl FnOnce(&mut LoadedLibrary) -> Result<T, String>,
) -> Result<T, String> {
    let path = path.canonicalize().map_err(|error| {
        format!(
            "failed to resolve Python shared library `{}`: {error}",
            path.display()
        )
    })?;
    let mut library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    if library_slot.is_none() {
        *library_slot = Some(LoadedLibrary::open(path.clone())?);
    }
    let library = library_slot
        .as_mut()
        .expect("Python shared library should have been loaded");
    library.ensure_path(&path)?;
    operation(library)
}

impl LoadedLibrary {
    fn open(path: PathBuf) -> Result<Self, String> {
        // SAFETY: The selected path comes from reticulate's interpreter
        // discovery. Global, eager loading exposes the CPython API before
        // either runtime initializes the interpreter.
        let flags = libc::RTLD_NOW | libc::RTLD_GLOBAL;
        let library =
            unsafe { libloading::os::unix::Library::open(Some(path.as_os_str()), flags) }
                .map_err(|error| {
                    format!(
                        "failed to load Python shared library `{}`: {error}",
                        path.display()
                    )
                })?;
        // SAFETY: Each requested symbol is a process-lifetime CPython API
        // function. The owning library handle is retained beside the copied
        // function pointers.
        let api = unsafe { PythonApi::load(&library, &path)? };
        // SAFETY: The resolved function has no preconditions.
        let interpreter = if unsafe { (api.is_initialized)() } == 0 {
            Interpreter::Uninitialized
        } else {
            Interpreter::External
        };
        Ok(Self {
            path,
            _library: library,
            api,
            interpreter,
            configuration: None,
        })
    }

    fn ensure_path(&self, requested: &Path) -> Result<(), String> {
        if self.path == requested {
            return Ok(());
        }
        Err(format!(
            "Python shared library is already loaded from `{}` and cannot switch to `{}`",
            self.path.display(),
            requested.display()
        ))
    }

    fn attach(&mut self) -> Result<(), String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } == 0 {
            return Err("cannot attach to Python before it is initialized".to_string());
        }
        if self.interpreter == Interpreter::Uninitialized {
            self.interpreter = Interpreter::External;
        }
        Ok(())
    }

    fn initialize(&mut self, program_name: &str, python_home: &str) -> Result<(), String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } != 0 {
            if self.interpreter == Interpreter::Uninitialized {
                self.interpreter = Interpreter::External;
            }
            return self.ensure_configuration(program_name, python_home);
        }

        match self.interpreter {
            Interpreter::Uninitialized => {}
            Interpreter::External => {
                return Err("externally owned Python interpreter was finalized".to_string());
            }
            Interpreter::RustOwned { .. } => {
                return Err("Rust-owned Python interpreter was finalized".to_string());
            }
        }

        let configuration = Configuration::new(program_name, python_home)?;
        // SAFETY: The pointers are NUL-terminated process-lifetime buffers.
        // CPython is not initialized, and these legacy configuration calls
        // must precede Py_InitializeEx for the supported Python versions.
        unsafe {
            (self.api.set_program_name)(configuration.program_name_wide.as_ptr());
            (self.api.set_python_home)(configuration.python_home_wide.as_ptr());
            (self.api.initialize_ex)(0);
        }
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } == 0 {
            return Err("CPython initialization did not complete".to_string());
        }

        let mut argv = [configuration.program_name_wide.as_ptr().cast_mut()];
        // SAFETY: CPython is initialized and argv points to the retained,
        // NUL-terminated program-name buffer.
        unsafe {
            (self.api.set_argv)(1, argv.as_mut_ptr());
            // Py_InitializeEx(0) does not install Python's signal handlers,
            // including its normal SIGPIPE disposition. Preserve the prior
            // reticulate-hosted behavior for the worker process lifetime.
            libc::signal(libc::SIGPIPE, libc::SIG_IGN);
        }

        self.configuration = Some(configuration);
        self.interpreter = Interpreter::RustOwned {
            gil_released: false,
        };
        Ok(())
    }

    fn ensure_configuration(&self, program_name: &str, python_home: &str) -> Result<(), String> {
        let Some(configuration) = self.configuration.as_ref() else {
            return Ok(());
        };
        if configuration.program_name == program_name && configuration.python_home == python_home {
            return Ok(());
        }
        Err("Python interpreter is already initialized with different configuration".to_string())
    }

    fn finish_initialization(&mut self) -> Result<(), String> {
        match self.interpreter {
            Interpreter::Uninitialized => {
                return Err("Python interpreter is not initialized".to_string());
            }
            Interpreter::External | Interpreter::RustOwned { gil_released: true } => {
                return Ok(());
            }
            Interpreter::RustOwned {
                gil_released: false,
            } => {}
        }
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } == 0 {
            return Err("Rust-owned Python interpreter was finalized".to_string());
        }
        // SAFETY: Rust initialized CPython on this thread, reticulate has
        // finished attaching, and the initial thread currently owns the GIL.
        let thread_state = unsafe { (self.api.save_thread)() };
        if thread_state.is_null() {
            return Err("CPython did not return its initial thread state".to_string());
        }
        self.interpreter = Interpreter::RustOwned { gil_released: true };
        Ok(())
    }
}

impl PythonApi {
    unsafe fn load(
        library: &libloading::os::unix::Library,
        path: &Path,
    ) -> Result<Self, String> {
        Ok(Self {
            // SAFETY: Symbol types match the documented CPython C API.
            is_initialized: unsafe { load_symbol(library, path, b"Py_IsInitialized\0")? },
            set_program_name: unsafe { load_symbol(library, path, b"Py_SetProgramName\0")? },
            set_python_home: unsafe { load_symbol(library, path, b"Py_SetPythonHome\0")? },
            initialize_ex: unsafe { load_symbol(library, path, b"Py_InitializeEx\0")? },
            set_argv: unsafe { load_symbol(library, path, b"PySys_SetArgv\0")? },
            save_thread: unsafe { load_symbol(library, path, b"PyEval_SaveThread\0")? },
        })
    }
}

unsafe fn load_symbol<T: Copy>(
    library: &libloading::os::unix::Library,
    path: &Path,
    name: &'static [u8],
) -> Result<T, String> {
    let display_name = std::str::from_utf8(&name[..name.len() - 1])
        .expect("CPython symbol names should be UTF-8");
    // SAFETY: The caller supplies the documented function-pointer type for
    // the named CPython C API symbol, and the library handle outlives it.
    unsafe { library.get::<T>(name) }
        .map(|symbol| *symbol)
        .map_err(|error| {
            format!(
                "Python shared library `{}` does not export {display_name}: {error}",
                path.display()
            )
        })
}

impl Configuration {
    fn new(program_name: &str, python_home: &str) -> Result<Self, String> {
        Ok(Self {
            program_name: program_name.to_string(),
            python_home: python_home.to_string(),
            program_name_wide: wide_string(program_name, "program name")?,
            python_home_wide: wide_string(python_home, "home")?,
        })
    }
}

fn wide_string(value: &str, label: &str) -> Result<Vec<libc::wchar_t>, String> {
    if value.contains('\0') {
        return Err(format!("Python {label} contains NUL"));
    }
    let mut wide = value
        .chars()
        .map(|character| character as libc::wchar_t)
        .collect::<Vec<_>>();
    wide.push(0);
    Ok(wide)
}

#[cfg(test)]
mod tests {
    use super::wide_string;

    #[test]
    fn python_wide_strings_are_nul_terminated() {
        assert_eq!(wide_string("pythøn", "test").unwrap().last(), Some(&0));
        assert!(wide_string("python\0home", "test").is_err());
    }
}
