//! Native managed Python requirements and activation commits.
//!
//! Reticulate still calculates transitions and activates environments. The R
//! adapter preserves presentation metadata and supplies the existing identity
//! comparison; this owner commits a pending activation on a requirement write.

mod r;

// Character payloads retain NA and the original bytes. Decoding to UTF-8 here
// would change byte-marked or native-encoded R strings. Encoding tags and vector
// attributes belong to the adapter, alongside field presence/order and history.
type Characters = Vec<Option<Vec<u8>>>;

#[derive(Clone, Default)]
struct Manifest {
    packages: Option<Characters>,
    python_version: Option<Characters>,
    exclude_newer: Option<Characters>,
}

#[derive(Default)]
struct Requirements {
    current: Option<Manifest>,
    // A transient matching key for an environment already activated by
    // reticulate, not a second independently mutable requirement manifest.
    pending_activation: Option<Manifest>,
}

impl Requirements {
    fn activation_pending(&self) -> bool {
        self.pending_activation.is_some()
    }

    fn check_activation(&self) -> Result<(), &'static str> {
        if self.activation_pending() {
            return Err("Python activation is awaiting a requirement update");
        }
        Ok(())
    }

    fn commit(&mut self, value: Manifest) -> bool {
        self.current = Some(value);
        self.pending_activation.take().is_some()
    }
}
