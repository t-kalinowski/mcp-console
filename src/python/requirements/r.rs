//! R-facing representation of the managed requirement manifest.
//!
//! Only the three operative character fields are decoded. Attributes, field
//! layout and non-operative fields (notably reticulate's history) stay opaque.
//! No retained R object contains the operative manifest. Reads reconstruct it
//! from the native values, copying metadata just as the former getter did.

use std::cell::RefCell;
use std::ffi::CStr;
use std::rc::Rc;
use std::sync::OnceLock;

use harp::object::{RObject, is_identical, r_null_or_try_into};
use libr::SEXP;

use super::{Characters, Manifest, Requirements};

#[derive(Clone, Copy)]
enum Field {
    Packages,
    PythonVersion,
    ExcludeNewer,
}

impl Field {
    fn value(self, manifest: &Manifest) -> &Option<Characters> {
        match self {
            Self::Packages => &manifest.packages,
            Self::PythonVersion => &manifest.python_version,
            Self::ExcludeNewer => &manifest.exclude_newer,
        }
    }

    fn value_mut(self, manifest: &mut Manifest) -> &mut Option<Characters> {
        match self {
            Self::Packages => &mut manifest.packages,
            Self::PythonVersion => &mut manifest.python_version,
            Self::ExcludeNewer => &mut manifest.exclude_newer,
        }
    }
}

struct VectorMetadata {
    attributes: RObject,
    encodings: Vec<libr::cetype_t>,
}

enum Entry {
    Requirement(Field, VectorMetadata),
    Metadata(RObject),
}

struct Metadata {
    attributes: RObject,
    entries: Vec<Entry>,
}

#[derive(Default)]
struct State {
    requirements: Requirements,
    current_metadata: Option<Rc<Metadata>>,
    pending_metadata: Option<Rc<Metadata>>,
}

thread_local! {
    // Metadata protection stays on the R thread. Rc snapshots let conversions
    // allocate (and run R finalizers) without holding a state borrow.
    static STATE: RefCell<State> = RefCell::new(State::default());
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_get() -> harp::Result<SEXP> {
    let snapshot = STATE.with(|state| {
        let state = state.borrow();
        state
            .requirements
            .current
            .clone()
            .zip(state.current_metadata.clone())
    });
    let (value, metadata) =
        snapshot.ok_or_else(|| harp::anyhow!("managed Python requirements are not installed"))?;
    Ok(metadata.to_r(&value)?.sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_requirements_set(
    value: SEXP,
    activation: SEXP,
) -> harp::Result<SEXP> {
    let pending = STATE.with(|state| {
        let state = state.borrow();
        state
            .requirements
            .pending_activation
            .clone()
            .zip(state.pending_metadata.clone())
    });
    if let Some((pending, metadata)) = pending
        && !is_identical(activation, metadata.to_r(&pending)?.sexp)
    {
        return Err(harp::anyhow!(
            "Python requirement update does not match pending activation"
        ));
    }
    let (value, metadata) = Metadata::from_r(value)?;
    let (previous, committed) = STATE.with(|state| {
        let mut state = state.borrow_mut();
        let previous = (
            state.current_metadata.replace(Rc::new(metadata)),
            state.pending_metadata.take(),
        );
        (previous, state.requirements.commit(value))
    });
    // Release protection and publish only after leaving the state borrow.
    drop(previous);
    if committed {
        publish_activation(activation)?;
    }
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_pending() -> harp::Result<SEXP> {
    let pending = STATE.with(|state| state.borrow().requirements.activation_pending());
    Ok(RObject::from(pending).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_activation_record(
    activation: SEXP,
) -> harp::Result<SEXP> {
    check_activation()?;
    // Called only after reticulate activation and process-environment setup
    // succeed. An earlier failure leaves ordinary snapshot restoration inert.
    let (activation, metadata) = Metadata::from_r(activation)?;
    let previous = STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.requirements.pending_activation = Some(activation);
        state.pending_metadata.replace(Rc::new(metadata))
    });
    drop(previous);
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_python_initialized(activation: SEXP) -> harp::Result<SEXP> {
    // Initial startup has its own successful reticulate hook, with no pending
    // late activation or subsequent requirement write to commit it.
    publish_activation(activation)?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
pub(super) fn check_activation() -> harp::Result<()> {
    STATE
        .with(|state| state.borrow().requirements.check_activation())
        .map_err(|error| harp::anyhow!("{error}"))
}

#[allow(clippy::result_large_err)]
fn publish_activation(activation: SEXP) -> harp::Result<()> {
    // The bridge still supplies its normalized projection in this order.
    let activation = RObject::view(activation);
    let requirements = crate::worker_protocol::PythonRequirementManifest {
        packages: activation.vector_elt(0)?.try_into()?,
        python_version: activation.vector_elt(1)?.try_into()?,
        exclude_newer: r_null_or_try_into(activation.vector_elt(2)?)?,
    };
    crate::worker::publish_python_activation(requirements).map_err(|error| harp::anyhow!("{error}"))
}

#[allow(clippy::result_large_err)]
impl Metadata {
    fn from_r(value: SEXP) -> harp::Result<(Manifest, Self)> {
        let value = RObject::view(value);
        let names: Vec<String> = value
            .get_attribute_names()
            .ok_or_else(|| harp::anyhow!("Python requirements must have named fields"))?
            .try_into()?;
        let mut manifest = Manifest::default();
        let mut entries = Vec::with_capacity(names.len());
        for (index, name) in names.iter().enumerate() {
            let value = value.vector_elt(index as isize)?;
            let field = match name.as_str() {
                "packages" => Some(Field::Packages),
                "python_version" => Some(Field::PythonVersion),
                "exclude_newer" => Some(Field::ExcludeNewer),
                _ => None,
            };
            entries.push(if let Some(field) = field {
                let (characters, metadata) = VectorMetadata::from_r(value.sexp)?;
                *field.value_mut(&mut manifest) = characters;
                Entry::Requirement(field, metadata)
            } else {
                Entry::Metadata(value.duplicate())
            });
        }
        Ok((
            manifest,
            Self {
                attributes: attributes(value.sexp),
                entries,
            },
        ))
    }

    fn to_r(&self, manifest: &Manifest) -> harp::Result<RObject> {
        let value = RObject::try_from(
            self.entries
                .iter()
                .map(|entry| match entry {
                    Entry::Requirement(field, metadata) => metadata.to_r(field.value(manifest)),
                    Entry::Metadata(value) => Ok(value.duplicate()),
                })
                .collect::<harp::Result<Vec<_>>>()?,
        )?;
        restore_attributes(&value, &self.attributes);
        Ok(value)
    }
}

#[allow(clippy::result_large_err)]
impl VectorMetadata {
    fn from_r(value: SEXP) -> harp::Result<(Option<Characters>, Self)> {
        let value = RObject::view(value);
        let mut metadata = Self {
            attributes: attributes(value.sexp),
            encodings: Vec::new(),
        };
        if value.is_null() {
            return Ok((None, metadata));
        }
        if unsafe { libr::TYPEOF(value.sexp) } != libr::STRSXP as i32 {
            return Err(harp::anyhow!(
                "Python requirement fields must be character vectors"
            ));
        }
        let get_encoding = get_char_encoding()?;
        let mut characters = Vec::with_capacity(value.length() as usize);
        for index in 0..value.length() {
            // SAFETY: value is a protected character vector, indexed in bounds.
            // Copy bytes before any R allocation, retaining NA separately.
            unsafe {
                let character = libr::STRING_ELT(value.sexp, index);
                characters.push(if character == libr::R_NaString {
                    None
                } else {
                    Some(CStr::from_ptr(libr::R_CHAR(character)).to_bytes().to_vec())
                });
                metadata.encodings.push(get_encoding(character));
            }
        }
        Ok((Some(characters), metadata))
    }

    fn to_r(&self, characters: &Option<Characters>) -> harp::Result<RObject> {
        let Some(characters) = characters else {
            return Ok(RObject::null());
        };
        // SAFETY: Runs on R's thread. Protect the vector before allocating its
        // elements; every byte string and encoding came from an R character.
        let value = unsafe {
            RObject::new(libr::Rf_allocVector(
                libr::STRSXP,
                characters.len() as isize,
            ))
        };
        for (index, (character, encoding)) in characters.iter().zip(&self.encodings).enumerate() {
            unsafe {
                let character = match character {
                    Some(bytes) => {
                        libr::Rf_mkCharLenCE(bytes.as_ptr().cast(), bytes.len() as i32, *encoding)
                    }
                    None => libr::R_NaString,
                };
                libr::SET_STRING_ELT(value.sexp, index as isize, character);
            }
        }
        restore_attributes(&value, &self.attributes);
        Ok(value)
    }
}

fn attributes(value: SEXP) -> RObject {
    // Only presentation attributes are retained, never the operative vector.
    unsafe { RObject::view(libr::ATTRIB(value)).duplicate() }
}

fn restore_attributes(value: &RObject, attributes: &RObject) {
    let attributes = attributes.duplicate();
    // SAFETY: Both objects are protected. The stored pairlist retains attribute
    // order, and setAttrib also restores the class/object bit where applicable.
    unsafe {
        let mut node = attributes.sexp;
        while node != libr::R_NilValue {
            libr::Rf_setAttrib(value.sexp, libr::TAG(node), libr::CAR(node));
            node = libr::CDR(node);
        }
    }
}

type GetCharEncoding = unsafe extern "C-unwind" fn(SEXP) -> libr::cetype_t;

#[allow(clippy::result_large_err)]
fn get_char_encoding() -> harp::Result<GetCharEncoding> {
    // libr does not expose this R API. As with the other R entry points loaded
    // by Console, libR is already loaded and remains resident for the process.
    static GET_CHAR_ENCODING: OnceLock<GetCharEncoding> = OnceLock::new();
    if let Some(function) = GET_CHAR_ENCODING.get() {
        return Ok(*function);
    }
    let library = libloading::os::unix::Library::this();
    let function = unsafe {
        *library
            .get::<GetCharEncoding>(b"Rf_getCharCE\0")
            .map_err(|error| harp::anyhow!("failed to load Rf_getCharCE: {error}"))?
    };
    Ok(*GET_CHAR_ENCODING.get_or_init(|| function))
}

// These are call-local projections, never retained beside the native store.
// Use R's vector operations for character equality/encoding and attribute
// behavior (including named and classed vectors), without delegating policy.
pub(super) struct Value(RObject);

pub(super) enum Error {
    Message(String),
    Interrupt(Value),
}

impl From<String> for Error {
    fn from(message: String) -> Self {
        Self::Message(message)
    }
}

impl From<&str> for Error {
    fn from(message: &str) -> Self {
        Self::Message(message.into())
    }
}

pub(super) struct Record(Value);

pub(super) struct Declaration {
    pub record: Record,
    pub packages: Value,
    pub python_version: Value,
    pub exclude_newer: Value,
    pub exclude_newer_supplied: bool,
    pub action: super::Action,
}

pub(super) struct Adapter(SEXP);

impl Value {
    pub(super) fn null() -> Self {
        Self(RObject::null())
    }

    pub(super) fn is_null(&self) -> bool {
        self.0.is_null()
    }

    pub(super) fn is_empty(&self) -> bool {
        self.0.length() == 0
    }

    pub(super) fn copy(&self) -> super::Result<Self> {
        harp::exec::r_sandbox(|| Self(self.0.clone())).map_err(from_r_error)
    }

    pub(super) fn identical(&self, other: &Self) -> super::Result<bool> {
        harp::exec::r_sandbox(|| is_identical(self.0.sexp, other.0.sexp)).map_err(from_r_error)
    }

    pub(super) fn text(&self) -> super::Result<String> {
        harp::exec::r_sandbox(|| String::try_from(&self.0))
            .map_err(from_r_error)?
            .map_err(from_r_error)
    }

    pub(super) fn boolean(&self) -> super::Result<bool> {
        harp::exec::r_sandbox(|| bool::try_from(self.0.clone()))
            .map_err(from_r_error)?
            .map_err(from_r_error)
    }

    pub(super) fn union(&self, other: &Self) -> super::Result<Self> {
        let combined = base_call("c", &[self, other])?;
        base_call("unique", &[&combined])
    }

    pub(super) fn difference(&self, other: &Self) -> super::Result<Self> {
        base_call("setdiff", &[self, other])
    }

    pub(super) fn disjoint(&self, other: &Self) -> super::Result<bool> {
        let matches = base_call("%in%", &[self, other])?;
        Ok(!base_call("any", &[&matches])?.boolean()?)
    }

    pub(super) fn set_equal(&self, other: &Self) -> super::Result<bool> {
        base_call("setequal", &[self, other])?.boolean()
    }
}

impl Record {
    pub(super) fn new(value: Value) -> super::Result<Self> {
        if value.is_null() {
            return Err("Python preparation did not produce a managed manifest".into());
        }
        Ok(Self(value))
    }

    pub(super) fn value(&self) -> &Value {
        &self.0
    }

    pub(super) fn get(&self, field: &str) -> super::Result<Value> {
        let field = harp::exec::r_sandbox(|| Value(RObject::from(field))).map_err(from_r_error)?;
        base_call("[[", &[&self.0, &field])
    }

    pub(super) fn set(&mut self, field: &str, value: Value) -> super::Result<()> {
        let field = harp::exec::r_sandbox(|| Value(RObject::from(field))).map_err(from_r_error)?;
        let value = base_call("list", &[&value])?;
        // Single-bracket assignment retains an explicitly NULL field and the
        // original list order/class, appending only previously absent fields.
        self.0 = base_call("[<-", &[&self.0, &field, &value])?;
        Ok(())
    }

    pub(super) fn append_history(&mut self, request: &Self) -> super::Result<()> {
        let event = base_call("list", &[request.value()])?;
        let history = base_call("c", &[&self.get("history")?, &event])?;
        self.set("history", history)
    }
}

impl Declaration {
    fn from_r(request: SEXP) -> super::Result<Self> {
        let record = Record(Value(RObject::view(request)));
        let action = match record.get("action")?.text()?.as_str() {
            "add" => super::Action::Add,
            "remove" => super::Action::Remove,
            "set" => super::Action::Set,
            _ => return Err("invalid Python requirement action".into()),
        };
        Ok(Self {
            packages: record.get("packages")?,
            python_version: record.get("python_version")?,
            exclude_newer: record.get("exclude_newer")?,
            exclude_newer_supplied: record.get("exclude_newer_supplied")?.boolean()?,
            record,
            action,
        })
    }
}

impl Adapter {
    pub(super) fn call(&self, function: &str, arguments: &[&Value]) -> super::Result<Value> {
        call(self.0, function, arguments)
    }

    pub(super) fn resolve(&self, candidate: &Record, version: &Value) -> super::Result<Value> {
        self.call(
            "resolve",
            &[
                &candidate.get("packages")?,
                version,
                &candidate.get("exclude_newer")?,
            ],
        )
    }
}

fn base_call(function: &str, arguments: &[&Value]) -> super::Result<Value> {
    call(unsafe { libr::R_BaseEnv }, function, arguments)
}

fn call(environment: SEXP, function: &str, arguments: &[&Value]) -> super::Result<Value> {
    use harp::exec::{RFunction, RFunctionExt};
    let call = harp::exec::r_sandbox(|| {
        let mut call = RFunction::new("", function);
        for argument in arguments {
            call.add(argument.0.clone());
        }
        let value = RFunction::new("base", "list")
            .param("value", call.call.build())
            .call
            .build();
        // Catch an interrupt before harp's top-level boundary turns its
        // longjump into an error. Keep the original condition until Rust has
        // unwound, then let the R-facing wrapper signal it to its caller.
        let identity = unsafe {
            RObject::view(libr::Rf_findVarInFrame(
                libr::R_BaseEnv,
                libr::Rf_install(c"identity".as_ptr()),
            ))
        };
        RFunction::new("base", "tryCatch")
            .add(value)
            .param("interrupt", identity)
            .call
            .build()
    })
    .map_err(from_r_error)?;
    // Protect allocation separately: R/Python execution and resolver callbacks
    // run in the caller's interrupt context, without a native state borrow.
    let result = harp::exec::try_eval(call.sexp, environment).map_err(from_r_error)?;
    harp::exec::r_sandbox(|| {
        if unsafe { libr::Rf_inherits(result.sexp, c"interrupt".as_ptr()) } != 0 {
            Err(Error::Interrupt(Value(result)))
        } else {
            result.vector_elt(0).map(Value).map_err(from_r_error)
        }
    })
    .map_err(from_r_error)?
}

pub(super) fn from_r_error(error: harp::Error) -> Error {
    Error::Message(match error {
        harp::Error::TryCatchError(error) => error.message,
        other => other.to_string(),
    })
}

// Unlike harp::register, these entry points do not suspend interrupts across
// environment preparation or activation. R conversions protect themselves.
#[ctor::ctor(unsafe)]
fn register_transitions() {
    type CallMethod = unsafe extern "C-unwind" fn() -> *mut libc::c_void;
    for (name, function, arity) in [
        (
            c"mcp_console_python_transition",
            python_transition as *const (),
            4,
        ),
        (
            c"mcp_console_python_prepare",
            python_prepare as *const (),
            2,
        ),
    ] {
        // SAFETY: R calls each function on its thread with the registered arity.
        unsafe {
            harp::routines::add(libr::R_CallMethodDef {
                name: name.as_ptr(),
                fun: Some(std::mem::transmute::<*const (), CallMethod>(function)),
                numArgs: arity,
            });
        }
    }
}

extern "C-unwind" fn python_transition(
    current: SEXP,
    request: SEXP,
    initialized: SEXP,
    adapter: SEXP,
) -> SEXP {
    complete(|| {
        let request = Declaration::from_r(request)?;
        let current = Record(Value(RObject::view(current)));
        let initialized = Value(RObject::view(initialized)).boolean()?;
        let (manifest, config) =
            Requirements::transition(&Adapter(adapter), current, request, initialized)?;
        named_list(&[("manifest", manifest.value()), ("config", &config)])
    })
}

extern "C-unwind" fn python_prepare(packages: SEXP, adapter: SEXP) -> SEXP {
    complete(|| {
        let failure = Requirements::prepare(&Adapter(adapter), Value(RObject::view(packages)))?;
        harp::exec::r_sandbox(|| match failure {
            None => named_list(&[("kind", &Value(RObject::from("ready")))]),
            Some(message) => named_list(&[
                ("kind", &Value(RObject::from("failed"))),
                ("message", &Value(RObject::from(message))),
            ]),
        })
        .map_err(from_r_error)?
    })
}

fn named_list(fields: &[(&str, &Value)]) -> super::Result<Value> {
    use harp::exec::{RFunction, RFunctionExt};
    harp::exec::r_sandbox(|| {
        let mut call = RFunction::new("base", "list");
        for (name, value) in fields {
            call.param(name, value.0.clone());
        }
        call.call().map(Value)
    })
    .map_err(from_r_error)?
    .map_err(from_r_error)
}

// Return interrupts as conditions only at these two private R boundaries;
// ordinary errors keep the existing message-only py_require()/prepare contract.
fn complete(operation: impl FnOnce() -> super::Result<Value>) -> SEXP {
    harp::exec::r_unwrap(|| match operation() {
        Ok(value) | Err(Error::Interrupt(value)) => Ok(value.0.sexp),
        Err(Error::Message(message)) => Err(message),
    })
}
