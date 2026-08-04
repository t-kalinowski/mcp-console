const BRIDGE_INIT: &str = r#"
base::local({
  connection <- NULL
  source <- NULL

  evaluate <- function(id) {
    tryCatch(
      {
        if (is.null(connection)) {
          storage <- file.path(tempdir(), "mcp-console-duckdb")
          connection <<- DBI::dbConnect(
            duckdb::duckdb(
              dbdir = ":memory:",
              config = list(
                extension_directory = file.path(storage, "extensions"),
                secret_directory = file.path(storage, "stored-secrets"),
                temp_directory = file.path(storage, "spill")
              ),
              environment_scan = FALSE
            )
          )
        }

        result <- DBI::dbSendQuery(connection, source)
        tryCatch(
          {
            output <- suppressWarnings(DBI::dbFetch(result))
            if (ncol(output) > 0L) {
              print(output, row.names = FALSE)
            }
          },
          finally = DBI::dbClearResult(result)
        )
      },
      error = function(error) {
        cat("Error: ", conditionMessage(error), "\n", sep = "")
      }
    )
    invisible(NULL)
  }

  environment()
}, envir = base::new.env(parent = base::baseenv()))
"#;

pub(crate) struct Bridge(crate::r_bridge::Bridge);

impl Bridge {
    pub(crate) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(BRIDGE_INIT, "SQL").map(Self)
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        self.0.evaluate(source)
    }
}
