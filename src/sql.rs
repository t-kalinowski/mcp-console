const SQL_BRIDGE_SOURCE: &str = include_str!("sql/bridge.R");

pub(crate) struct Bridge(crate::r_bridge::Bridge);

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(SQL_BRIDGE_SOURCE, "SQL").map(Self)
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }
}
