use std::ffi::{CStr, CString};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

// A loaded handle is retained for the process lifetime. SQL calls copy its
// immutable function table under this lock, then release the guard before
// invoking Python so Python-to-R callbacks can re-enter library access.
static PYTHON_LIBRARY: Mutex<Option<LoadedLibrary>> = Mutex::new(None);

type PyIsInitialized = unsafe extern "C" fn() -> libc::c_int;
type PySetProgramName = unsafe extern "C" fn(*const libc::wchar_t);
type PySetPythonHome = unsafe extern "C" fn(*const libc::wchar_t);
type PyInitializeEx = unsafe extern "C" fn(libc::c_int);
type PySysSetArgv = unsafe extern "C" fn(libc::c_int, *mut *mut libc::wchar_t);
type PyOsSetSignal = unsafe extern "C" fn(libc::c_int, libc::sighandler_t) -> libc::sighandler_t;
type PyEvalSaveThread = unsafe extern "C" fn() -> *mut libc::c_void;
type PyObject = libc::c_void;
type PyGilState = libc::c_int;
type PyGilStateEnsure = unsafe extern "C" fn() -> PyGilState;
type PyGilStateRelease = unsafe extern "C" fn(PyGilState);
type PyImportAddModule = unsafe extern "C" fn(*const libc::c_char) -> *mut PyObject;
type PyModuleGetDict = unsafe extern "C" fn(*mut PyObject) -> *mut PyObject;
type PyDictNew = unsafe extern "C" fn() -> *mut PyObject;
type PyDictGetItemString =
    unsafe extern "C" fn(*mut PyObject, *const libc::c_char) -> *mut PyObject;
type PyRunStringFlags = unsafe extern "C" fn(
    *const libc::c_char,
    libc::c_int,
    *mut PyObject,
    *mut PyObject,
    *mut libc::c_void,
) -> *mut PyObject;
type PyObjectCallNoArgs = unsafe extern "C" fn(*mut PyObject) -> *mut PyObject;
type PyObjectCallFunctionObjArgs = unsafe extern "C" fn(*mut PyObject, ...) -> *mut PyObject;
type PyUnicodeFromStringAndSize = unsafe extern "C" fn(*const libc::c_char, isize) -> *mut PyObject;
type PyLongAsLong = unsafe extern "C" fn(*mut PyObject) -> libc::c_long;
type PyDecRef = unsafe extern "C" fn(*mut PyObject);
type PyErrFetch = unsafe extern "C" fn(*mut *mut PyObject, *mut *mut PyObject, *mut *mut PyObject);
type PyErrNormalizeException =
    unsafe extern "C" fn(*mut *mut PyObject, *mut *mut PyObject, *mut *mut PyObject);
type PyErrDisplay = unsafe extern "C" fn(*mut PyObject, *mut PyObject, *mut PyObject);
type PyErrClear = unsafe extern "C" fn();
type PyErrPrint = unsafe extern "C" fn();

const PY_FILE_INPUT: libc::c_int = 257;
const SQL_PROVIDER_R: libc::c_long = 0;
const SQL_PROVIDER_MANAGED: libc::c_long = 1;
const SQL_PROVIDER_HANDLED: libc::c_long = 2;

struct LoadedLibrary {
    path: PathBuf,
    _library: libloading::os::unix::Library,
    api: PythonApi,
    interpreter: Interpreter,
    configuration: Option<Configuration>,
    sql_runtime_installed: bool,
}

#[derive(Clone, Copy)]
struct PythonApi {
    is_initialized: PyIsInitialized,
    set_program_name: PySetProgramName,
    set_python_home: PySetPythonHome,
    initialize_ex: PyInitializeEx,
    set_argv: PySysSetArgv,
    set_signal: PyOsSetSignal,
    save_thread: PyEvalSaveThread,
    gil_state_ensure: PyGilStateEnsure,
    gil_state_release: PyGilStateRelease,
    import_add_module: PyImportAddModule,
    module_get_dict: PyModuleGetDict,
    dict_new: PyDictNew,
    dict_get_item_string: PyDictGetItemString,
    run_string_flags: PyRunStringFlags,
    call_no_args: PyObjectCallNoArgs,
    call_function_obj_args: PyObjectCallFunctionObjArgs,
    unicode_from_string_and_size: PyUnicodeFromStringAndSize,
    long_as_long: PyLongAsLong,
    dec_ref: PyDecRef,
    err_fetch: PyErrFetch,
    err_normalize_exception: PyErrNormalizeException,
    err_display: PyErrDisplay,
    err_clear: PyErrClear,
    err_print: PyErrPrint,
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

pub(super) fn load(path: &Path) -> Result<bool, String> {
    with_library(path, LoadedLibrary::attach)
}

pub(super) fn initialize(
    path: &Path,
    program_name: &str,
    python_home: &str,
) -> Result<bool, String> {
    with_library(path, |library| {
        library.initialize(program_name, python_home)
    })
}

pub(super) fn install_runtime(source: &str) -> Result<(), String> {
    let mut library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    let library = library_slot
        .as_mut()
        .ok_or_else(|| "Python shared library is not loaded".to_string())?;
    library.install_runtime(source)
}

pub(super) fn install_sql_runtime(source: &str) -> Result<(), String> {
    let mut library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    let library = library_slot
        .as_mut()
        .ok_or_else(|| "Python shared library is not loaded".to_string())?;
    library.install_sql_runtime(source)
}

pub(super) fn dispatch_sql(source: &str) -> Result<super::SqlProvider, String> {
    let Some(api) = installed_sql_api()? else {
        return Ok(super::SqlProvider::R);
    };
    api.with_gil(|api| api.call_sql_dispatch(source))
}

pub(super) fn use_r_sql() -> Result<(), String> {
    let Some(api) = installed_sql_api()? else {
        return Ok(());
    };
    api.with_gil(|api| api.call_unit(c"_mcp_console_sql", c"use_r"))
}

fn installed_sql_api() -> Result<Option<PythonApi>, String> {
    let library_slot = PYTHON_LIBRARY
        .lock()
        .map_err(|_| "Python shared library state is unavailable".to_string())?;
    let Some(library) = library_slot.as_ref() else {
        return Ok(None);
    };
    Ok(library.sql_runtime_installed.then_some(library.api))
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
        let library = unsafe { libloading::os::unix::Library::open(Some(path.as_os_str()), flags) }
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
            sql_runtime_installed: false,
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

    fn attach(&mut self) -> Result<bool, String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } == 0 {
            return Err("cannot attach to Python before it is initialized".to_string());
        }
        if self.interpreter == Interpreter::Uninitialized {
            self.interpreter = Interpreter::External;
        }
        Ok(matches!(self.interpreter, Interpreter::RustOwned { .. }))
    }

    fn initialize(&mut self, program_name: &str, python_home: &str) -> Result<bool, String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.api.is_initialized)() } != 0 {
            if self.interpreter == Interpreter::Uninitialized {
                self.interpreter = Interpreter::External;
            }
            self.ensure_configuration(program_name, python_home)?;
            return Ok(matches!(self.interpreter, Interpreter::RustOwned { .. }));
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
            (self.api.set_signal)(libc::SIGPIPE, libc::SIG_IGN);
        }

        self.configuration = Some(configuration);
        self.interpreter = Interpreter::RustOwned {
            gil_released: false,
        };
        Ok(true)
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

    fn install_runtime(&self, source: &str) -> Result<(), String> {
        let source = CString::new(source)
            .map_err(|_| "embedded Python runtime source contains NUL".to_string())?;
        self.api.with_gil(|api| {
            // SAFETY: The GIL is held and the source is a valid NUL-terminated
            // buffer for the duration of the call.
            unsafe { api.run_runtime(&source) }
        })
    }

    fn install_sql_runtime(&mut self, source: &str) -> Result<(), String> {
        let source = CString::new(source)
            .map_err(|_| "embedded Python SQL runtime source contains NUL".to_string())?;
        self.api.with_gil(|api| {
            // SAFETY: The GIL is held and both strings are valid for the call.
            unsafe { api.run_module(c"_mcp_console_sql", &source) }
        })?;
        self.sql_runtime_installed = true;
        Ok(())
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
        // SAFETY: Rust initialized CPython on this thread, and reticulate's
        // wrapped C initializer leaves this initial thread state attached on
        // both its return and unwind paths.
        let thread_state = unsafe { (self.api.save_thread)() };
        if thread_state.is_null() {
            return Err("CPython did not return its initial thread state".to_string());
        }
        self.interpreter = Interpreter::RustOwned { gil_released: true };
        Ok(())
    }
}

impl PythonApi {
    fn with_gil<T>(
        &self,
        operation: impl FnOnce(&PythonApi) -> Result<T, String>,
    ) -> Result<T, String> {
        // SAFETY: The resolved function has no preconditions.
        if unsafe { (self.is_initialized)() } == 0 {
            return Err("Python interpreter is not initialized".to_string());
        }
        // SAFETY: CPython is initialized. PyGILState_Ensure permits this call
        // both while reticulate holds the GIL and after it has released it.
        let gil_state = unsafe { (self.gil_state_ensure)() };
        let result = operation(self);
        // SAFETY: This state was returned by the matching ensure call above.
        unsafe { (self.gil_state_release)(gil_state) };
        result
    }

    unsafe fn run_runtime(&self, source: &CStr) -> Result<(), String> {
        // Match reticulate::py_run_string(local = TRUE): definitions are
        // isolated in a fresh locals dictionary while functions retain the
        // persistent __main__ globals used by Python cells.
        let main = unsafe { (self.import_add_module)(c"__main__".as_ptr()) };
        if main.is_null() {
            unsafe { (self.err_print)() };
            return Err("failed to access Python's main module".to_string());
        }
        let globals = unsafe { (self.module_get_dict)(main) };
        if globals.is_null() {
            unsafe { (self.err_print)() };
            return Err("failed to access Python's main namespace".to_string());
        }
        let locals = unsafe { (self.dict_new)() };
        if locals.is_null() {
            unsafe { (self.err_print)() };
            return Err("failed to create the private Python runtime namespace".to_string());
        }
        let result = unsafe {
            (self.run_string_flags)(
                source.as_ptr(),
                PY_FILE_INPUT,
                globals,
                locals,
                std::ptr::null_mut(),
            )
        };
        if result.is_null() {
            unsafe {
                (self.err_print)();
                (self.dec_ref)(locals);
            }
            return Err("failed to install MCP Console's private Python runtime".to_string());
        }
        unsafe {
            (self.dec_ref)(result);
            (self.dec_ref)(locals);
        }
        Ok(())
    }

    unsafe fn run_module(&self, name: &CStr, source: &CStr) -> Result<(), String> {
        let module = unsafe { (self.import_add_module)(name.as_ptr()) };
        if module.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to create Python module `{}`",
                name.to_string_lossy()
            ));
        }
        let namespace = unsafe { (self.module_get_dict)(module) };
        if namespace.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to access Python module `{}`",
                name.to_string_lossy()
            ));
        }
        let result = unsafe {
            (self.run_string_flags)(
                source.as_ptr(),
                PY_FILE_INPUT,
                namespace,
                namespace,
                std::ptr::null_mut(),
            )
        };
        if result.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to install Python module `{}`",
                name.to_string_lossy()
            ));
        }
        unsafe { (self.dec_ref)(result) };
        Ok(())
    }

    fn call_unit(&self, module: &CStr, name: &CStr) -> Result<(), String> {
        // SAFETY: The GIL is held for the complete call and reference release.
        unsafe {
            let function = self.function(module, name)?;
            let result = (self.call_no_args)(function);
            if result.is_null() {
                (self.err_print)();
                return Err(python_function_error(module, name));
            }
            (self.dec_ref)(result);
            Ok(())
        }
    }

    fn call_sql_dispatch(&self, source: &str) -> Result<super::SqlProvider, String> {
        // SAFETY: The GIL is held and PyUnicode_FromStringAndSize copies the
        // UTF-8 source before the Rust buffer can be released.
        unsafe {
            let function = self.function(c"_mcp_console_sql", c"dispatch")?;
            let argument =
                (self.unicode_from_string_and_size)(source.as_ptr().cast(), source.len() as isize);
            if argument.is_null() {
                (self.err_print)();
                return Err("failed to create Python SQL source string".to_string());
            }
            let result =
                (self.call_function_obj_args)(function, argument, std::ptr::null_mut::<PyObject>());
            (self.dec_ref)(argument);
            if result.is_null() {
                // Database errors are normally caught by the Python adapter.
                // Escaping exceptions such as KeyboardInterrupt remain ordinary
                // console output and must not fall through to an R provider.
                self.display_pending_exception();
                return Ok(super::SqlProvider::Handled);
            }
            let provider = (self.long_as_long)(result);
            (self.dec_ref)(result);
            match provider {
                SQL_PROVIDER_R => Ok(super::SqlProvider::R),
                SQL_PROVIDER_MANAGED => Ok(super::SqlProvider::Managed),
                SQL_PROVIDER_HANDLED => Ok(super::SqlProvider::Handled),
                _ => {
                    if provider == -1 {
                        (self.err_print)();
                    }
                    Err("Python SQL dispatch returned an invalid provider".to_string())
                }
            }
        }
    }

    fn display_pending_exception(&self) {
        // PyErr_Print exits the process for SystemExit. Fetch and display the
        // pending exception directly so every Python language exception remains
        // ordinary worker output.
        unsafe {
            let mut exception_type = std::ptr::null_mut();
            let mut exception_value = std::ptr::null_mut();
            let mut traceback = std::ptr::null_mut();
            (self.err_fetch)(&mut exception_type, &mut exception_value, &mut traceback);
            if !exception_type.is_null() {
                (self.err_normalize_exception)(
                    &mut exception_type,
                    &mut exception_value,
                    &mut traceback,
                );
                if !exception_type.is_null() && !exception_value.is_null() {
                    (self.err_display)(exception_type, exception_value, traceback);
                }
            }
            for object in [exception_type, exception_value, traceback] {
                if !object.is_null() {
                    (self.dec_ref)(object);
                }
            }
            (self.err_clear)();
        }
    }

    unsafe fn function(&self, module: &CStr, name: &CStr) -> Result<*mut PyObject, String> {
        let module_object = unsafe { (self.import_add_module)(module.as_ptr()) };
        if module_object.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to access Python module `{}`",
                module.to_string_lossy()
            ));
        }
        let namespace = unsafe { (self.module_get_dict)(module_object) };
        if namespace.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "failed to access Python module `{}`",
                module.to_string_lossy()
            ));
        }
        let function = unsafe { (self.dict_get_item_string)(namespace, name.as_ptr()) };
        if function.is_null() {
            unsafe { (self.err_print)() };
            return Err(format!(
                "Python module `{}` is missing `{}`",
                module.to_string_lossy(),
                name.to_string_lossy()
            ));
        }
        Ok(function)
    }

    unsafe fn load(library: &libloading::os::unix::Library, path: &Path) -> Result<Self, String> {
        Ok(Self {
            // SAFETY: Symbol types match the documented CPython C API.
            is_initialized: unsafe { load_symbol(library, path, b"Py_IsInitialized\0")? },
            set_program_name: unsafe { load_symbol(library, path, b"Py_SetProgramName\0")? },
            set_python_home: unsafe { load_symbol(library, path, b"Py_SetPythonHome\0")? },
            initialize_ex: unsafe { load_symbol(library, path, b"Py_InitializeEx\0")? },
            set_argv: unsafe { load_symbol(library, path, b"PySys_SetArgv\0")? },
            set_signal: unsafe { load_symbol(library, path, b"PyOS_setsig\0")? },
            save_thread: unsafe { load_symbol(library, path, b"PyEval_SaveThread\0")? },
            gil_state_ensure: unsafe { load_symbol(library, path, b"PyGILState_Ensure\0")? },
            gil_state_release: unsafe { load_symbol(library, path, b"PyGILState_Release\0")? },
            import_add_module: unsafe { load_symbol(library, path, b"PyImport_AddModule\0")? },
            module_get_dict: unsafe { load_symbol(library, path, b"PyModule_GetDict\0")? },
            dict_new: unsafe { load_symbol(library, path, b"PyDict_New\0")? },
            dict_get_item_string: unsafe { load_symbol(library, path, b"PyDict_GetItemString\0")? },
            run_string_flags: unsafe { load_symbol(library, path, b"PyRun_StringFlags\0")? },
            call_no_args: unsafe { load_symbol(library, path, b"PyObject_CallNoArgs\0")? },
            call_function_obj_args: unsafe {
                load_symbol(library, path, b"PyObject_CallFunctionObjArgs\0")?
            },
            unicode_from_string_and_size: unsafe {
                load_symbol(library, path, b"PyUnicode_FromStringAndSize\0")?
            },
            long_as_long: unsafe { load_symbol(library, path, b"PyLong_AsLong\0")? },
            dec_ref: unsafe { load_symbol(library, path, b"Py_DecRef\0")? },
            err_fetch: unsafe { load_symbol(library, path, b"PyErr_Fetch\0")? },
            err_normalize_exception: unsafe {
                load_symbol(library, path, b"PyErr_NormalizeException\0")?
            },
            err_display: unsafe { load_symbol(library, path, b"PyErr_Display\0")? },
            err_clear: unsafe { load_symbol(library, path, b"PyErr_Clear\0")? },
            err_print: unsafe { load_symbol(library, path, b"PyErr_Print\0")? },
        })
    }
}

fn python_function_error(module: &CStr, name: &CStr) -> String {
    format!(
        "Python function `{}.{}` failed",
        module.to_string_lossy(),
        name.to_string_lossy()
    )
}

unsafe fn load_symbol<T: Copy>(
    library: &libloading::os::unix::Library,
    path: &Path,
    name: &'static [u8],
) -> Result<T, String> {
    let display_name =
        std::str::from_utf8(&name[..name.len() - 1]).expect("CPython symbol names should be UTF-8");
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
