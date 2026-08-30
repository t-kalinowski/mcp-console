const SQL_BRIDGE_SOURCE: &str = include_str!("bridge.R");

pub(super) struct Backend(crate::r_bridge::Bridge);

impl Backend {
    pub(super) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(SQL_BRIDGE_SOURCE, "SQL").map(Self)
    }

    pub(super) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }
}
