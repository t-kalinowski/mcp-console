# Sessions and languages

Console gives your agent an interactive workspace for R, Python, and SQL.
You can ask it to explore data, refine an analysis, or try another approach while keeping the work already in memory.

## Build on earlier work

Variables, imports, functions, and database tables remain available between calls.
For example, after the agent fits a statistical model, you can ask it to inspect the residuals or plot predictions without loading the data and fitting the model again.
An error does not undo changes made before the failure.

The agent can respond to interactive prompts and step through code in a debugger.
Long computations can keep running between calls while the agent collects results as they become available.
A call's wait timeout does not stop the computation.
The agent can request an interrupt or restart an unresponsive session.

<span id="current-limitations"></span>

Each connection has one session and runs one code cell at a time.
Restarting clears live objects and in-memory database state; reconnecting starts a new session.
Recordings preserve a record of the work, not a saved session that can be resumed.

## Combine languages in one session {#r-and-python-interoperability}

Console embeds R, Python, and DuckDB in the same process.
Your agent can switch languages between calls, choosing the libraries and syntax that fit each step.
It can reuse data across languages without exporting and reloading intermediate files.

For example, with R and the required packages available, the agent could load complete observations from the Palmer Penguins dataset in R:

```r
penguins <- na.omit(palmerpenguins::penguins)
```

Then summarize body mass by species with SQL:

```sql
SELECT species, avg(body_mass_g) AS mean_body_mass_g
FROM penguins
GROUP BY species
ORDER BY species;
```

And use Python to plot the relationship between flipper length and body mass:

```python
r.penguins.plot.scatter(x="flipper_length_mm", y="body_mass_g")
```

Here, SQL queries the R data frame through the default R-owned DuckDB connection, and Python accesses it through reticulate as `r.penguins`.
Conversions follow reticulate's rules; some conversions need additional Python packages, such as pandas for data frames.
The [runtime reference](RUNTIME_REFERENCE.md#r-and-python-interoperability) describes the language bridge and its helpers.

## Choose languages and packages {#runtime-selection}

<span id="r"></span> <span id="python"></span>

R and Python can each run without the other.
SQL uses a database connection supplied by one of them.
By default, Console prepares a Python environment and uses an in-memory DuckDB database for SQL.
To let your agent use R, [install it separately](getting-started.qmd#install).

You can select an [existing Python environment](CONFIGURATION.md#python-environment-selection) or an [R installation](CONFIGURATION.md#r-executable-selection).
Console can prepare packages for managed environments; an existing Python environment uses the packages installed there.
[Packages and environments](REQUIREMENTS.md) explains the defaults and how to control preparation.

## Work with databases {#sql-and-duckdb}

The default DuckDB database keeps tables and views available for later queries.
The agent can query R data frames by name when R owns the managed connection, as in the example above.
With a Python-owned connection, it can register Python data frames for SQL queries.

<span id="select-a-native-connection"></span>

Your agent can also use a database connection opened from R or Python, including a connection to an existing database.
The selected connection determines the SQL dialect and transaction behavior.
Access to database files and servers depends on the session's [filesystem and network permissions](SANDBOX.md).

The [SQL connection reference](RUNTIME_REFERENCE.md#select-a-native-connection) covers supported connection types and selection.
You can also [configure a database connection at startup](CONFIGURATION.md#session-startup-source).

## Inspect help, plots, and results

Console formats help and output for agents, with affordances for inspecting results without accidentally flooding the model's context.

### Help and documentation {#installed-documentation}

Your agent can consult installed R help and package vignettes while it works.
Help pages, search results, and installed HTML vignettes are presented as text, including code examples and evaluated results from the vignettes.
Vignette figures are represented by captions and available alternative text.
The [documentation reference](RUNTIME_REFERENCE.md#installed-documentation) explains other formats and access to the original files.

### Plots and images

Console captures R plots and Matplotlib pyplot figures and returns them as images for the agent to inspect.
You can ask the agent to revise labels, change the scale, or try a different view of the same data.
It can generate a new plot using objects already in the session.

The [plotting reference](RUNTIME_REFERENCE.md#plots-and-images) covers image dimensions and which plotting methods are captured automatically.

### Large results {#output-and-notices}

Console limits how much text it sends back at once and collapses progress bar redraws.
For oversized output, it shows the beginning and recent end and identifies the retained log.
The agent can use its client's file tools to inspect that log without rerunning the computation.

<span id="result-previews"></span>

SQL results show a limited number of rows and columns.
Rows that were never printed are not present in the log, so ask the agent to save the full result when you need the whole dataset.

## Review and reuse the work

[Recordings](RECORDING.md) retain the code, output, and plots so you can see what the agent did and inspect its results.
Ask the agent for the recording's location.
The recorded Quarto document provides a starting point for a report; review its code, inputs, and package requirements before rendering it.

For precise language behavior, see the [runtime reference](RUNTIME_REFERENCE.md).
[Client integration](CLIENTS.md) covers applications that call Console directly.
