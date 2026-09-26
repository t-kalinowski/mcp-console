# Configuration layering

`serve` and ordinary `sandbox` launches read `.agents/console/config.yaml` beneath the current directory, then apply each `-c KEY=VALUE` or `--config KEY=VALUE` in command-line order.
Options may appear before or after the subcommand.
Only the current directory is searched; an absent file starts with an empty configuration.
An unreadable file or malformed YAML prevents launch.
Overrides change the configuration for this launch without editing the file.

```sh
mcp-console serve -c extends=:workspace
mcp-console -c extends=:workspace serve -c sandbox.network=enabled
mcp-console sandbox -c 'sandbox.environment={LABEL: analysis, MODE: "batch"}' -- Rscript analysis.R
```

## Python environment selection

For a local built-in session, select an existing Python environment with:

```yaml
python: .venv/bin/python
```

Paths are relative to the launch directory.
The equivalent CLI override is `mcp-console serve -c python=.venv/bin/python`.
This setting takes precedence over inherited `RETICULATE_PYTHON` and is retained across worker restarts.
It is unavailable with custom workers and execution targets.

In a [session without R](BUILTIN_RUNTIME.md#python-sessions-without-r), omitting both selections uses uv-managed Python and enables explicit startup/restart package preparation.
An explicit Python selection bypasses uv entirely and disables package preparation.
Configure the existing environment's packages before starting Console.

## Keys and values

Dotted keys address nested mappings.
For example, `-c foo.bar=baz` contributes `{foo: {bar: baz}}`, and `-c 'foo.bar={baz: [far, faz]}'` contributes a nested mapping and list.
The application validates the resulting configuration against its current schema; these illustrative `foo` keys are not current application settings.
See [sandbox configuration](SANDBOX_CONFIGURATION.md), [SSH](SSH.md), [Docker](DOCKER.md), and [Docker Sandbox](DOCKER_SANDBOX.md) for supported settings.

Inline values accept:

- Bare strings, quoted strings, YAML booleans, finite numbers, and `null`.
- Lists such as `[far, faz]` or `["far", "faz"]`.
- Objects such as `{baz: [far, faz]}`, `{"baz":["far","faz"]}`, or `{baz = [far, faz]}`.
  Objects and lists can nest, and trailing commas are accepted.

Object entries may use either `:` or `=`; the same value can mix them.
Scalar types follow the project's YAML loader.
Single and double quotes use YAML quoting rules, including doubled single quotes and double-quoted escapes.
This is an inline value syntax, not a full TOML document parser.
There is no variable expansion or file inclusion.

Quote a value that must remain a string, such as `-c 'sandbox.environment.COUNT="42"'` or `-c 'sandbox.environment.EMPTY=""'`.
An assignment without a value is an error.
The shell removes its own quotes first, so quote the entire assignment when it contains spaces or shell punctuation.

Dots in the assignment key separate mapping keys; they do not index lists.
For a literal dotted key, supply its containing object: `-c 'sandbox.environment={"APP.VERSION": "v1"}'`.
Object keys are literal strings, including unquoted keys; whitespace around assignment keys, path components, and values is ignored.

## Merge rules

Each override forms a nested mapping and merges into the previous configuration:

- Mappings merge recursively by key, preserving keys omitted from the override.
- Lists replace the entire previous value; they do not append or merge by index.
- Scalars and `null` replace the previous value.
  `null` remains an explicit value, rather than deleting the key.
- A mapping replaces a previous scalar, list, or `null` with a mapping.
  An empty mapping merges without clearing an existing mapping.

Later assignments take precedence over earlier assignments.
For example, `-c 'sandbox.environment={KEEP: project, CHANGE: first}' -c sandbox.environment.CHANGE=last` retains `KEEP` and changes `CHANGE` to `last`.
To clear an inherited mapping before rebuilding it, replace it with `null`, then supply the new mapping in a later override.
Only the final configuration is validated against the application schema.

These rules apply to every key, including `kind` and `extends`; the layering module has no field-specific merge rules.
Changing a discriminator does not remove sibling fields inherited from the project file.
Configuration defaults, built-in profile expansion, target selection, and native policy validation happen after layering.
Existing `--writable-root` grants are added after layering; `--no-sandbox` retains its documented execution behavior.

The server captures the resulting settings once and retains them across worker restarts.
Selected execution hosts consume that captured configuration without rediscovering the controller's project file or CLI arguments.
The explicit native `sandbox --config-env` interface and the internal `--settings-env` interface already supply a complete captured input and reject `-c` overrides.

## Implementation

`src/config.rs` loads an optional mapping from a caller-supplied path and applies ordered overrides to a JSON-compatible value tree.
`src/config/inline.rs` parses inline structure, while `src/config/yaml.rs` owns YAML loading and scalar conversion.
These modules contain no application field names or configuration types.
`src/settings.rs` selects the project path and decodes the merged value into the current application schema.
