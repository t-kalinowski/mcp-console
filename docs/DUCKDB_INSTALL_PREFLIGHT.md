# DuckDB INSTALL preflight contract

`duckdb_install::recognize()` is a pure Rust lexical recognizer exported by the same-package library.
It is a prerequisite for later integration, and is not called by SQL dispatch.
Console still passes complete SQL cells to the selected DBI or DB-API connection.
There are no installation, networking, resolver, authorization, connection-ownership, or execution changes in this slice.

## Recognized grammar

The complete cell must contain only these forms, comments, SQL whitespace, and optional empty semicolon statements:

```sql
INSTALL json;
INSTALL 'httpfs' FROM core;
INSTALL "httpfs" FROM "core";
```

Keywords are case-insensitive.
Names contain only ASCII letters, digits and underscores, starting with a letter or underscore.
Extension names may be bare, single-quoted or double-quoted.
Repository aliases may be bare or double-quoted.
Quoted names must contain the same safe characters; escapes and doubled quotes are outside this restricted grammar.
Names retain their original case.
DuckDB repository alias lookup is case-sensitive: `core` and `CORE` are different.

Omitted `FROM` produces `Repository::Default`; it is never rewritten to `core`.
An explicit alias remains explicit.
A single-quoted `FROM 'core'` denotes a repository path in DuckDB, so it is declined rather than treated as an alias.
Paths, URLs, versions and `FORCE INSTALL` are also declined.

Line comments and nested block comments are trivia.
Spaces, tabs, CR, LF and form feeds are accepted whitespace.
The source is borrowed unchanged, and each INSTALL and name has an exact UTF-8 byte range into that source.
Statement ranges start at `INSTALL` and end at the last name; separators and surrounding trivia remain in the source gaps.
These ranges identify text; they do not authorize splitting or regenerating SQL.

## Conservative whole-cell decision

`Preflight::Recognized` is returned only after analysis of the entire restricted cell.
`Preflight::Passthrough` contains a reason and no partial plan:

| Reason               | Boundary                                              |
| -------------------- | ----------------------------------------------------- |
| `UncertainGrammar`   | Ordinary SQL, mixed cells, or unknown grammar         |
| `UnsupportedInstall` | Install-like forms outside the restricted grammar     |
| `IncompleteInput`    | Missing names, unterminated quotes/comments, or NUL   |
| `ResourceLimit`      | More than 1 MiB of source or 1,024 INSTALL statements |

The reasons describe why interception was declined, not a DuckDB diagnostic.
A caller must pass declined source unchanged to the real driver.
Unknown ordinary SQL must remain usable even when this optional recognizer cannot understand it.
Non-DuckDB connections must bypass recognition entirely.

Any ordinary or unknown statement declines the whole cell.
A semicolon alone cannot prove a top-level boundary inside procedural or extension grammar.
Consequently literals, escaped or dollar-quoted strings, quoted identifiers, compound bodies, and unknown syntax cannot expose an embedded INSTALL as a request.
A valid INSTALL prefix followed by uncertain or malformed input also produces no plan.
Empty/comment-only cells produce an empty plan.

Analysis uses an iterative, forward-only byte scanner with no recursion, regex, AST, external parser process, or DuckDB dependency.
Time is linear in source bytes; borrowed names avoid source copies, and plan memory is bounded by the statement limit.
The limits apply before any caller can obtain a plan.

The released [sqlparser 0.63.0 tokenizer](https://docs.rs/sqlparser/0.63.0/sqlparser/tokenizer/struct.Tokenizer.html) and [DuckDbDialect](https://docs.rs/sqlparser/0.63.0/sqlparser/dialect/struct.DuckDbDialect.html) were considered.
General tokens and locations do not prove statement boundaries in arbitrary grammar.
The small complete-cell grammar needs neither the general parser/AST nor a conversion from its line/column locations to byte ranges, so this slice uses the dedicated scanner without adding a dependency.

## Engine authority and integration handoff

Lexical recognition does not guarantee DuckDB parse, bind or execution success.
For example, an ASCII name can be a reserved keyword, and a lexically safe alias can be unknown to the selected engine.
Resolver authorization and revalidation remain required before any host effect.
No successful-prefix host work is permitted when the complete cell is declined.

Public binding conformance tests cover released DuckDB-R/Python 1.5.6 on macOS.
Quoted safe-name syntax is checked without installing extensions: Python uses `extract_statements`; R adds a deliberately malformed final statement and checks that the whole batch fails at that final syntax error, with external access also disabled.
Both bindings reject whole-input syntax errors before executing the first statement.
A later catalog error stops the batch but preserves earlier changes; explicit rollback removes changes made inside an open transaction.
These observations do not justify splitting cells into separate driver calls.

The subsequent integration must provide a public regression, preserve full SQL source/order/diagnostics and selected-provider ownership, and choose execution sequencing using the real binding's transaction and error behavior.
Coordinate submission/newline handling with that work; there are no renderer or protocol version changes here.
