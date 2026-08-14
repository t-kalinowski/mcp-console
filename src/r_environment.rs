const BRIDGE_INIT: &str = r#"
base::local({
  managed <- base::.libPaths()[[1L]]

  prepare <- function(library) {
    result <- base::tryCatch({
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
      base::list(kind = "prepared", library = library)
    }, error = function(error) {
      base::list(kind = "failed", message = base::conditionMessage(error))
    })
    jsonlite::toJSON(
      result,
      auto_unbox = TRUE,
      null = "null",
      na = "null"
    )
  }

  base::environment()
}, envir = base::new.env(parent = base::baseenv()))
"#;

pub(crate) struct Bridge(crate::r_bridge::Bridge);

#[derive(serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum PreparationOutcome {
    Prepared { library: String },
    Failed { message: String },
}

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "R environment").map(Self)
    }

    pub(crate) fn prepare(&self, library: &std::path::Path) -> Result<PreparationOutcome, String> {
        let library = library
            .to_str()
            .ok_or_else(|| "resolved R library path is not UTF-8".to_string())?;
        let response = self
            .0
            .call1_string(c"prepare", library)?
            .ok_or_else(|| "R environment bridge returned no response".to_string())?;
        serde_json::from_str(&response)
            .map_err(|error| format!("invalid R environment preparation response: {error}"))
    }
}
