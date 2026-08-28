send <- function(
  r = NULL,
  sql = NULL,
  control = NULL,
  requirements = NULL,
  stdin = NULL,
  timeout_ms = NULL
) {
  if (!is.null(requirements$r) && length(requirements$r) == 0L) {
    return("at least one of `requirements.r` must be supplied")
  }

  if (!is.null(r)) {
    r
  } else if (!is.null(sql)) {
    sql
  } else {
    "<poll>"
  }
}

nullable <- function(type, ...) {
  ellmer::TypeJsonSchema(
    json = list(type = as.list(c(type, "null")), ...),
    required = FALSE
  )
}

requirement <- list(
  type = "array",
  items = list(type = "string"),
  default = list()
)
requirements <- rep(list(requirement), 3L)
names(requirements) <- c("duckdb", "r", "python")

list(ellmer::tool(
  send,
  name = "send",
  description = "Persistent test console.",
  arguments = list(
    r = nullable("string"),
    sql = nullable("string"),
    control = nullable(
      "string",
      enum = list("interrupt", "restart", NULL)
    ),
    requirements = nullable(
      "object",
      properties = requirements,
      required = list(),
      additionalProperties = FALSE
    ),
    stdin = nullable("string"),
    timeout_ms = nullable("integer")
  )
))
