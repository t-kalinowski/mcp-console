use std::error::Error;
use std::ffi::c_int;

use harp::object::RObject;

pub(super) struct Parser {
    helper: RObject,
}

impl Parser {
    pub(super) fn initialize() -> Result<Self, Box<dyn Error>> {
        Ok(Self {
            helper: harp::parse_eval_base(include_str!("parse.R"))?,
        })
    }

    pub(super) fn complete(&self, source: &str) -> Result<bool, String> {
        let length = c_int::try_from(source.len())
            .map_err(|_| "R source exceeds R's maximum string size".to_string())?;
        // Direct evaluation bypasses REPL task callbacks, history, and .Last.value.
        // R_ToplevelExec restores interpreter bookkeeping and contains errors and
        // interrupts. Keep Rust values with destructors outside this closure.
        let result = harp::top_level_exec(|| unsafe {
            let text = libr::Rf_mkCharLenCE(source.as_ptr().cast(), length, libr::cetype_t_CE_UTF8);
            let text = libr::Rf_protect(libr::Rf_ScalarString(text));
            let call = libr::Rf_protect(libr::Rf_lang2(self.helper.sexp, text));
            libr::Rf_eval(call, libr::R_BaseEnv);
            libr::Rf_unprotect(2);
        });
        // R already reported any parser error or interrupt through the console.
        Ok(result.is_ok())
    }
}
