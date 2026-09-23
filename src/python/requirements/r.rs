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
pub extern "C-unwind" fn mcp_console_python_activation_check() -> harp::Result<SEXP> {
    check_activation()?;
    unsafe { Ok(libr::R_NilValue) }
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
fn check_activation() -> harp::Result<()> {
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
