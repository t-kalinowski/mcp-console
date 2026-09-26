use std::ffi::{c_int, c_void};
use std::sync::OnceLock;

use super::{ConsoleChannel, emit_output};

type ParseError = unsafe extern "C-unwind" fn(libr::SEXP, c_int) -> !;

static PARSE_ERROR: OnceLock<ParseError> = OnceLock::new();

pub(super) fn initialize(
    library: &libloading::os::unix::Library,
) -> Result<(), Box<dyn std::error::Error>> {
    let parse_error = unsafe { *library.get::<ParseError>(b"parseError\0")? };
    PARSE_ERROR
        .set(parse_error)
        .map_err(|_| std::io::Error::other("R parse error reporter was already initialized"))?;
    Ok(())
}

struct Preflight {
    source: libr::SEXP,
    status: libr::ParseStatus,
    warning_options: [(libr::SEXP, libr::SEXP); 2],
    muted: bool,
}

pub(super) fn complete(source: &str) -> Result<bool, String> {
    let length = c_int::try_from(source.len())
        .map_err(|_| "R source exceeds R's maximum string size".to_string())?;
    // This main-thread boundary contains parser errors and interrupts. The
    // callbacks hold no Rust values with destructors across R's non-local jumps.
    let result = harp::top_level_exec(|| unsafe {
        let text = libr::Rf_mkCharLenCE(source.as_ptr().cast(), length, libr::cetype_t_CE_UTF8);
        let text = libr::Rf_protect(libr::Rf_ScalarString(text));
        let muted_warn = libr::Rf_protect(libr::Rf_ScalarInteger(-1));
        let muted_expression = libr::Rf_protect(libr::Rf_allocVector(libr::EXPRSXP, 0));
        let mut protected = 3;
        let mut preflight = Preflight {
            source: text,
            status: libr::ParseStatus_PARSE_NULL,
            warning_options: [(libr::R_NilValue, libr::R_NilValue); 2],
            muted: false,
        };
        let names = [
            libr::Rf_install(c"warn".as_ptr()),
            libr::Rf_install(c"warning.expression".as_ptr()),
        ];
        let mut node =
            libr::Rf_findVarInFrame(libr::R_BaseEnv, libr::Rf_install(c".Options".as_ptr()));
        while node != libr::R_NilValue {
            for (index, name) in names.iter().enumerate() {
                if libr::TAG(node) == *name {
                    preflight.warning_options[index] =
                        (libr::Rf_protect(node), libr::Rf_protect(libr::CAR(node)));
                    protected += 2;
                }
            }
            node = libr::CDR(node);
        }
        // R requires the warn option; warning.expression is optional. Change
        // only their values, using the native pairlist without evaluating R.
        // An existing option cannot contain NULL; use an empty expression.
        if preflight.warning_options[0].0 == libr::R_NilValue {
            libr::Rf_error(c"R's mandatory 'warn' option is missing".as_ptr());
        }
        for ((node, _), value) in preflight
            .warning_options
            .iter()
            .zip([muted_warn, muted_expression])
        {
            if *node != libr::R_NilValue {
                libr::SETCAR(*node, value);
            }
        }
        preflight.muted = true;
        let data = (&raw mut preflight).cast();
        libr::R_ExecWithCleanup(Some(parse), data, Some(restore_warning_options), data);
        libr::Rf_unprotect(protected);
        if preflight.status == libr::ParseStatus_PARSE_ERROR {
            // Use the same source-context diagnostic as the native REPL.
            PARSE_ERROR
                .get()
                .expect("R parse error reporter should be initialized")(
                libr::R_NilValue, 0
            );
        }
        preflight.status
    });
    match result {
        Ok(libr::ParseStatus_PARSE_OK) => Ok(true),
        Ok(libr::ParseStatus_PARSE_INCOMPLETE) => {
            emit_output(ConsoleChannel::Diagnostic, b"Error: Incomplete code\n");
            Ok(false)
        }
        // R already reported a parser condition, allocation error, or interrupt.
        Err(_) => Ok(false),
        Ok(status) => Err(format!(
            "R preflight received unexpected parse status {status}"
        )),
    }
}

unsafe extern "C-unwind" fn parse(data: *mut c_void) -> libr::SEXP {
    unsafe {
        libr::R_withCallingErrorHandler(Some(parse_vector), data, Some(restore_before_error), data)
    }
}

unsafe extern "C-unwind" fn parse_vector(data: *mut c_void) -> libr::SEXP {
    let preflight = data.cast::<Preflight>();
    unsafe {
        // No source references or retained AST are needed for validation. The
        // REPL owns warnings and evaluation after the whole source is accepted.
        libr::R_ParseVector(
            (*preflight).source,
            -1,
            &raw mut (*preflight).status,
            libr::R_NilValue,
        );
        libr::R_NilValue
    }
}

unsafe extern "C-unwind" fn restore_before_error(
    _condition: libr::SEXP,
    data: *mut c_void,
) -> libr::SEXP {
    // Restore before options(error) runs, preserving changes that handler makes.
    unsafe {
        restore_warning_options(data);
        libr::R_NilValue
    }
}

unsafe extern "C-unwind" fn restore_warning_options(data: *mut c_void) {
    let preflight = unsafe { &mut *data.cast::<Preflight>() };
    if preflight.muted {
        preflight.muted = false;
        for (node, value) in preflight.warning_options {
            unsafe {
                if node != libr::R_NilValue {
                    libr::SETCAR(node, value);
                }
            }
        }
    }
}
