const SQL_BRIDGE_SOURCE: &str = include_str!("bridge.R");
const SQL_PROVIDER_SOURCE: &str = include_str!("provider.R");

pub(super) struct Backend(crate::r_bridge::Bridge);

impl Backend {
    pub(super) fn initialize() -> Result<Self, String> {
        let source = format!(
            "base::local(\n  {{\n    state <- ({SQL_BRIDGE_SOURCE})\n    configure <- ({SQL_PROVIDER_SOURCE})\n    configure(state)\n    state\n  }},\n  envir = base::new.env(parent = base::baseenv())\n)"
        );
        crate::r_bridge::Bridge::initialize(&source, "SQL").map(Self)
    }

    pub(super) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }

    pub(super) fn restore_managed(&self) -> Result<(), String> {
        match self.0.call0_integer(c"restore_managed_connection")? {
            1 => Ok(()),
            _ => Err("SQL DBI bridge did not restore managed DuckDB".to_string()),
        }
    }
}
