#!/bin/sh
set -eu

console=${1:-mcp-console}
policy='{"version":2,"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted","environment":{"MESSAGE":"value: café 雪, \"quotes\", $() and spaces"}}'

# Assignment syntax supplies this child's environment without exporting policy.
# Each positional parameter is one argument; only the explicit target is a shell.
set -- /bin/sh -c 'printf "%s\n" "$MESSAGE" "$1"' sh 'literal argument: * ; $(echo no)'
SANDBOX_POLICY="$policy" "$console" sandbox --config-env SANDBOX_POLICY -- "$@"
