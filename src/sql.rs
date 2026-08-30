mod dbi;

/// Worker-facing SQL runtime facade.
///
/// The current backend owns DuckDB through DBI in embedded R. Keeping that
/// backend behind this facade leaves worker dispatch independent of the SQL
/// host without changing the persistent catalog or preview behavior.
pub(crate) struct Bridge(dbi::Backend);

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        dbi::Backend::initialize().map(Self)
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }
}
