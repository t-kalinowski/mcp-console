use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::sync::OnceLock;

use libr::SEXP;

use super::Provider;

const PYTHON_RUNTIME_SOURCE: &str = include_str!("dbapi.py");
const PY_FILE_INPUT: c_int = 257;

type PyObject = c_void;
type PyGilState = c_int;
type PyIsInitialized = unsafe extern "C" fn() -> c_int;
type PyGilStateEnsure = unsafe extern "C" fn() -> PyGilState;
type PyGilStateRelease = unsafe extern "C" fn(PyGilState);
type PyImportAddModule = unsafe extern "C" fn(*const c_char) -> *mut PyObject;
type PyImportImportModule = unsafe extern "C" fn(*const c_char) -> *mut PyObject;
type PyModuleGetDict = unsafe extern "C" fn(*mut PyObject) -> *mut PyObject;
type PyRunStringFlags = unsafe extern "C" fn(
    *const c_char,
    c_int,
    *mut PyObject,
    *mut PyObject,
    *mut c_void,
) -> *mut PyObject;
type PyObjectGetAttrString =
    unsafe extern "C" fn(*mut PyObject, *const c_char) -> *mut PyObject;
type PyObjectCallNoArgs = unsafe extern "C" fn(*mut PyObject) -> *mut PyObject;
type PyObjectCallOneArg =
    unsafe extern "C" fn(*mut PyObject, *mut PyObject) -> *mut PyObject;
type PyUnicodeFromStringAndSize =
    unsafe extern "C" fn(*const c_char, isize) -> *mut PyObject;
type PyObjectIsTrue = unsafe extern "C" fn(*mut PyObject) -> c_int;
type PyDecRef = unsafe extern "C" fn(*mut PyObject);
type PyErrPrint = unsafe extern "C" fn();
type PyErrClear = unsafe extern "C" fn();

static PYTHON_API: OnceLock<PythonApi> = OnceLock::new();

pub(super) struct Backend;

impl Backend {
    pub(super) fn initialize() -> Self {
        Self
    }

    pub(super) fn provider(&self) -> Result<Provider, String> {
        let Some(api) = initialized_api()? else {
            return Ok(Provider::R);
        };
        with_gil(api, |api| {
            if api.call_bool(c"has_connection", true)?.unwrap_or(false) {
                return Ok(Provider::Python);
            }
            if api
                .call_bool(c"restore_managed_requested", true)?
                .unwrap_or(false)
            {
                return Ok(Provider::Managed);
            }
            Ok(Provider::R)
        })
    }

    pub(super) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        let api = initialized_api()?
            .ok_or_else(|| "Python DB-API SQL backend is not initialized".to_string())?;
        with_gil(api, |api| api.call_string(c"evaluate", source))
    }
}

fn install_runtime() -> Result<(), String> {
    let api = initialized_api()?.ok_or_else(|| {
        "cannot install the Python SQL backend before CPython initializes".to_string()
    })?;
    with_gil(api, |api| api.run_source(PYTHON_RUNTIME_SOURCE))
}

fn use_r() -> Result<(), String> {
    let Some(api) = initialized_api()? else {
        return Ok(());
    };
    with_gil(api, |api| {
        let _ = api.call_unit(c"use_r", true)?;
        Ok(())
    })
}

fn initialized_api() -> Result<Option<&'static PythonApi>, String> {
    let Some(api) = python_api()? else {
        return Ok(None);
    };
    // SAFETY: Py_IsInitialized has no preconditions.
    if unsafe { (api.is_initialized)() } == 0 {
        return Ok(None);
    }
    Ok(Some(api))
}

fn python_api() -> Result<Option<&'static PythonApi>, String> {
    if let Some(api) = PYTHON_API.get() {
        return Ok(Some(api));
    }

    let library = libloading::os::unix::Library::this();
    let is_initialized = match unsafe { library.get::<PyIsInitialized>(b"Py_IsInitialized\0") } {
        Ok(symbol) => *symbol,
        Err(_) => return Ok(None),
    };
    let api = unsafe { PythonApi::load(&library, is_initialized)? };
    let _ = PYTHON_API.set(api);
    Ok(PYTHON_API.get())
}

fn with_gil<T>(
    api: &PythonApi,
    operation: impl FnOnce(&PythonApi) -> Result<T, String>,
) -> Result<T, String> {
    // SAFETY: CPython is initialized and PyGILState_Ensure supports calls from
    // the R worker thread whether reticulate currently owns the GIL or not.
    let state = unsafe { (api.gil_state_ensure)() };
    let result = operation(api);
    // SAFETY: This state came from the matching ensure call above.
    unsafe { (api.gil_state_release)(state) };
    result
}

struct PythonApi {
    is_initialized: PyIsInitialized,
    gil_state_ensure: PyGilStateEnsure,
    gil_state_release: PyGilStateRelease,
    import_add_module: PyImportAddModule,
    import_module: PyImportImportModule,
    module_get_dict: PyModuleGetDict,
    run_string_flags: PyRunStringFlags,
    get_attr_string: PyObjectGetAttrString,
    call_no_args: PyObjectCallNoArgs,
    call_one_arg: PyObjectCallOneArg,
    unicode_from_string_and_size: PyUnicodeFromStringAndSize,
    object_is_true: PyObjectIsTrue,
    dec_ref: PyDecRef,
    err_print: PyErrPrint,
    err_clear: PyErrClear,
}

impl PythonApi {
    unsafe fn load(
        library: &libloading::os::unix::Library,
        is_initialized: PyIsInitialized,
    ) -> Result<Self, String> {
        Ok(Self {
            is_initialized,
            gil_state_ensure: unsafe { load_symbol(library, b"PyGILState_Ensure\0")? },
            gil_state_release: unsafe { load_symbol(library, b"PyGILState_Release\0")? },
            import_add_module: unsafe { load_symbol(library, b"PyImport_AddModule\0")? },
            import_module: unsafe { load_symbol(library, b"PyImport_ImportModule\0")? },
            module_get_dict: unsafe { load_symbol(library, b"PyModule_GetDict\0")? },
            run_string_flags: unsafe { load_symbol(library, b"PyRun_StringFlags\0")? },
            get_attr_string: unsafe { load_symbol(library, b"PyObject_GetAttrString\0")? },
            call_no_args: unsafe { load_symbol(library, b"PyObject_CallNoArgs\0")? },
            call_one_arg: unsafe { load_symbol(library, b"PyObject_CallOneArg\0")? },
            unicode_from_string_and_size: unsafe {
                load_symbol(library, b"PyUnicode_FromStringAndSize\0")?
            },
            object_is_true: unsafe { load_symbol(library, b"PyObject_IsTrue\0")? },
            dec_ref: unsafe { load_symbol(library, b"Py_DecRef\0")? },
            err_print: unsafe { load_symbol(library, b"PyErr_Print\0")? },
            err_clear: unsafe { load_symbol(library, b"PyErr_Clear\0")? },
        })
    }

    fn run_source(&self, source: &str) -> Result<(), String> {
        let source = CString::new(source)
            .map_err(|_| "embedded Python SQL runtime contains NUL".to_string())?;
        // SAFETY: The GIL is held. PyImport_AddModule creates or reuses the
        // private module and returns a borrowed reference. Running with that
        // module dictionary as both globals and locals keeps connection state,
        // functions, and imports entirely in Python.
        unsafe {
            let module = (self.import_add_module)(c"_mcp_console_sql".as_ptr());
            if module.is_null() {
                (self.err_print)();
                return Err("failed to create the Python SQL module".to_string());
            }
            let namespace = (self.module_get_dict)(module);
            if namespace.is_null() {
                (self.err_print)();
                return Err("failed to access the Python SQL module namespace".to_string());
            }
            let result = (self.run_string_flags)(
                source.as_ptr(),
                PY_FILE_INPUT,
                namespace,
                namespace,
                std::ptr::null_mut(),
            );
            if result.is_null() {
                (self.err_print)();
                return Err("failed to install the Python SQL runtime".to_string());
            }
            (self.dec_ref)(result);
        }
        Ok(())
    }

    fn call_bool(&self, name: &CStr, optional_module: bool) -> Result<Option<bool>, String> {
        // SAFETY: The GIL is held for the complete import, call, conversion,
        // and reference-release sequence.
        unsafe {
            let Some((module, function)) = self.function(name, optional_module)? else {
                return Ok(None);
            };
            let result = (self.call_no_args)(function);
            (self.dec_ref)(function);
            (self.dec_ref)(module);
            if result.is_null() {
                (self.err_print)();
                return Err(format!(
                    "Python SQL backend function `{}` failed",
                    name.to_string_lossy()
                ));
            }
            let value = (self.object_is_true)(result);
            (self.dec_ref)(result);
            if value < 0 {
                (self.err_print)();
                return Err(format!(
                    "Python SQL backend function `{}` returned an invalid truth value",
                    name.to_string_lossy()
                ));
            }
            Ok(Some(value != 0))
        }
    }

    fn call_unit(&self, name: &CStr, optional_module: bool) -> Result<Option<()>, String> {
        // SAFETY: The GIL is held for the complete call and reference release.
        unsafe {
            let Some((module, function)) = self.function(name, optional_module)? else {
                return Ok(None);
            };
            let result = (self.call_no_args)(function);
            (self.dec_ref)(function);
            (self.dec_ref)(module);
            if result.is_null() {
                (self.err_print)();
                return Err(format!(
                    "Python SQL backend function `{}` failed",
                    name.to_string_lossy()
                ));
            }
            (self.dec_ref)(result);
            Ok(Some(()))
        }
    }

    fn call_string(&self, name: &CStr, value: &str) -> Result<(), String> {
        // SAFETY: The GIL is held and the UTF-8 source buffer remains valid
        // until PyUnicode_FromStringAndSize copies it.
        unsafe {
            let Some((module, function)) = self.function(name, false)? else {
                unreachable!("required Python SQL module was treated as optional");
            };
            let argument =
                (self.unicode_from_string_and_size)(value.as_ptr().cast(), value.len() as isize);
            if argument.is_null() {
                (self.err_print)();
                (self.dec_ref)(function);
                (self.dec_ref)(module);
                return Err("failed to create Python SQL source string".to_string());
            }
            let result = (self.call_one_arg)(function, argument);
            (self.dec_ref)(argument);
            (self.dec_ref)(function);
            (self.dec_ref)(module);
            if result.is_null() {
                // Runtime exceptions are ordinary SQL output. The Python
                // adapter normally catches database exceptions itself; print
                // any escaping exception and leave the worker reusable.
                (self.err_print)();
                return Ok(());
            }
            (self.dec_ref)(result);
            Ok(())
        }
    }

    unsafe fn function(
        &self,
        name: &CStr,
        optional_module: bool,
    ) -> Result<Option<(*mut PyObject, *mut PyObject)>, String> {
        let module = unsafe { (self.import_module)(c"_mcp_console_sql".as_ptr()) };
        if module.is_null() {
            if optional_module {
                unsafe { (self.err_clear)() };
                return Ok(None);
            }
            unsafe { (self.err_print)() };
            return Err("Python SQL runtime is not installed".to_string());
        }
        let function = unsafe { (self.get_attr_string)(module, name.as_ptr()) };
        if function.is_null() {
            unsafe {
                (self.err_print)();
                (self.dec_ref)(module);
            }
            return Err(format!(
                "Python SQL runtime is missing `{}`",
                name.to_string_lossy()
            ));
        }
        Ok(Some((module, function)))
    }
}

unsafe fn load_symbol<T: Copy>(
    library: &libloading::os::unix::Library,
    name: &'static [u8],
) -> Result<T, String> {
    let display_name =
        std::str::from_utf8(&name[..name.len() - 1]).expect("CPython symbols should be UTF-8");
    unsafe { library.get::<T>(name) }
        .map(|symbol| *symbol)
        .map_err(|error| format!("loaded CPython does not export {display_name}: {error}"))
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_install_python_sql_runtime() -> harp::Result<SEXP> {
    install_runtime().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_use_r() -> harp::Result<SEXP> {
    use_r().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}
