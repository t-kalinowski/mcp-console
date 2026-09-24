//! Native managed Python requirements and activation commits.
//!
//! This owner chooses declaration transitions and when to prepare a candidate.
//! The native owner checks the selected environment and activates it through
//! the retained CPython library. The R adapter supplies candidate configuration
//! and preserves presentation metadata and the activation commit boundary.

mod r;

use std::path::Path;

use r::{Adapter, Declaration, Record, Value};

type Result<T> = std::result::Result<T, r::Error>;

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
    // Console, not a second independently mutable requirement manifest.
    pending_activation: Option<Manifest>,
}

impl Requirements {
    fn activation_pending(&self) -> bool {
        self.pending_activation.is_some()
    }

    fn check_activation(&self) -> std::result::Result<(), &'static str> {
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

#[derive(Clone, Copy)]
enum Action {
    Add,
    Remove,
    Set,
}

impl Requirements {
    // Candidates are detached projections of the existing store. Only the
    // active-binding write commits them, after reticulate updates its config.
    fn transition(
        adapter: &Adapter,
        mut candidate: Record,
        request: Declaration,
        initialized: bool,
    ) -> Result<(Record, Value)> {
        let current_packages = candidate.get("packages")?;
        let current_cutoff = candidate.get("exclude_newer")?;
        let mut activate = false;
        if !initialized {
            for (field, requested) in [
                ("packages", &request.packages),
                ("python_version", &request.python_version),
            ] {
                if !requested.is_null() {
                    let current = candidate.get(field)?;
                    let value = match request.action {
                        Action::Add => current.union(requested)?,
                        Action::Remove => current.difference(requested)?,
                        Action::Set => requested.copy()?,
                    };
                    candidate.set(field, value)?;
                }
            }
            if request.exclude_newer_supplied {
                let cutoff = match request.action {
                    Action::Add => {
                        if !current_cutoff.is_null() {
                            return Err(format!(
                                "`exclude_newer` is already set to '{}', use `action = 'set'` to override",
                                current_cutoff.text()?
                            ).into());
                        }
                        request.exclude_newer.copy()?
                    }
                    Action::Remove => {
                        if request.exclude_newer.is_null()
                            || request.exclude_newer.identical(&current_cutoff)?
                        {
                            Value::null()
                        } else {
                            current_cutoff
                        }
                    }
                    Action::Set => request.exclude_newer.copy()?,
                };
                candidate.set("exclude_newer", cutoff)?;
            }
        } else {
            if !request.python_version.is_null() {
                adapter.call("check_version", &[request.record.value()])?;
            }
            if request.exclude_newer_supplied
                && !request.exclude_newer.identical(&current_cutoff)?
            {
                return Err(
                    "`exclude_newer` cannot be changed after Python has initialized.".into(),
                );
            }
            if !request.packages.is_null() {
                match request.action {
                    Action::Add => {
                        let added = request.packages.difference(&current_packages)?;
                        if !added.is_empty() {
                            adapter.call("check_packages", &[&added, &current_packages])?;
                            candidate.set("packages", added.union(&current_packages)?)?;
                            activate = true;
                        }
                    }
                    Action::Remove | Action::Set => {
                        let unchanged = match request.action {
                            Action::Remove => request.packages.disjoint(&current_packages)?,
                            Action::Set => request.packages.set_equal(&current_packages)?,
                            Action::Add => unreachable!(),
                        };
                        if !unchanged {
                            return Err(
                                "After Python has initialized, only `action = 'add'` is supported."
                                    .into(),
                            );
                        }
                    }
                }
            }
        }
        let config = if activate {
            // No state borrow survives a compatibility check, resolver, or
            // activation callback. The interpreter pin is resolver input only.
            r::check_activation().map_err(r::from_r_error)?;
            let version = adapter.call("live_python_version", &[])?;
            let python = adapter.resolve(&candidate, &version)?;
            Self::activate(adapter, &python, &candidate)?
        } else {
            Value::null()
        };
        candidate.append_history(&request.record)?;
        Ok((candidate, config))
    }

    fn activate(adapter: &Adapter, python: &Value, candidate: &Record) -> Result<Value> {
        let config = Record::config(adapter.call("candidate_config", &[python])?)?;
        let selected_libpython = config.get("libpython")?;
        let live_libpython = adapter.call("live_libpython", &[])?;
        if !selected_libpython.identical(&live_libpython)? {
            return Err(format!(
                "New environment does not use the same Python binary\nnew libpython: {}\nold libpython: {}",
                selected_libpython.text()?,
                live_libpython.text()?
            )
            .into());
        }

        let python = python.text()?;
        let script = Path::new(&python)
            .parent()
            .ok_or("selected Python executable has no parent directory")?
            .join("activate_this.py");
        let script = script
            .to_str()
            .ok_or("Python activation script path is not UTF-8")?;
        let executable = config.get("executable")?.text()?;
        let completed = super::library::activate_environment(script, &executable)?;
        if !completed {
            // CPython retained the original exception and traceback. Rethrow
            // through reticulate's existing condition and interrupt boundary.
            adapter.call("raise_python_setup_error", &[])?;
            return Err("Python activation failed without an exception".into());
        }

        let config = adapter.call("available_config", &[config.value()])?;
        // Record the transient key only after Python and process-environment
        // setup succeed. Reticulate updates its configuration from this return
        // value, then writes the active binding that publishes PythonActivated.
        adapter.call("record_activation", &[candidate.value()])?;
        Ok(config)
    }

    fn prepare(adapter: &Adapter, packages: Value) -> Result<Option<String>> {
        let snapshot = adapter.call("current_requirements", &[])?;
        let result = (|| {
            // Keep the public declaration boundary, including reticulate's
            // argument conversion, package warnings, and history provenance.
            adapter.call("declare_packages", &[&packages])?;
            let candidate = Record::new(adapter.call("declared_requirements", &[])?)?;
            if !adapter.call("python_initialized", &[])?.boolean()? {
                // Explicit preparation materializes lazy declarations without
                // initializing Python or publishing a live activation.
                adapter.resolve(&candidate, &candidate.get("python_version")?)?;
            }
            Ok(())
        })();
        match result {
            Ok(()) => Ok(None),
            Err(r::Error::Message(message)) => {
                // Restore only ordinary failures, as the former R error
                // handler did. Interrupts retain their existing boundary.
                adapter.call("restore_requirements", &[&snapshot])?;
                Ok(Some(message))
            }
            Err(interrupt) => Err(interrupt),
        }
    }
}
