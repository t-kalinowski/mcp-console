const BRIDGE_INIT: &str = r#"
base::local({
  managed <- base::.libPaths()[[1L]]

  prepare <- function(library) {
    library <- base::normalizePath(
      library,
      winslash = "/",
      mustWork = TRUE
    )
    paths <- base::.libPaths()
    base::.libPaths(base::c(library, paths[paths != managed]))
    if (!base::identical(base::.libPaths()[[1L]], library)) {
      base::stop("resolved R library was not added to .libPaths()")
    }
    managed <<- library
    library
  }

  base::environment()
}, envir = base::new.env(parent = base::baseenv()))
"#;

pub(crate) struct Bridge(crate::r_bridge::Bridge);

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "R environment").map(Self)
    }

    pub(crate) fn prepare(&self, library: &std::path::Path) -> Result<String, String> {
        let library = library
            .to_str()
            .ok_or_else(|| "resolved R library path is not UTF-8".to_string())?;
        self.0
            .call1_string(c"prepare", library)?
            .ok_or_else(|| "R environment bridge returned no library".to_string())
    }
}
