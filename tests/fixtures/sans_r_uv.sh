#!/bin/sh
# Public resolver fixture: failures and causal interruption checkpoints.
root=$(/usr/bin/dirname "$0")
replace_output() {
    /bin/rm "$1"
    /bin/ln -s "$root/unrelated" "$1"
}
checkpoint() {
    trap 'echo "$message" >&2; exit 1' INT
    exec 3>"$root/alive"
    printf 1 >&3
    /bin/cat "$root/wait" >/dev/null &
    waiter=$!
    printf 1 > "$root/started"
    wait "$waiter"
    exit 85
}
if [ "$(/usr/bin/basename "$0")" = invalid-python ]; then
    [ "$#" = 3 ] && exit 0
    if [ "$(/bin/cat "$root/mode")" = inspection-interrupt ]; then
        message="fixture Python inspection interrupted"
        checkpoint
    fi
    /bin/cat "$root/invalid-inspection.json" > "$4"
    [ "$(/bin/cat "$root/mode")" != replace-inspection ] || replace_output "$4"
    exit 0
fi
if [ "$2" = dir ]; then exec "$root/real-uv" "$@"; fi
printf '%s\n' "$@" >> "$root/resolutions.log"
needs_preparation=false
for output do
    [ "$output" = py-yaml12 ] && needs_preparation=true
done
if $needs_preparation; then
    case $(/bin/cat "$root/mode") in
        failure) echo "fixture Python resolution failed" >&2; exit 1 ;;
        replace-output|replace-inspection|replace-status)
            printf '%s' "$root/invalid-python" > "$output"
            case $(/bin/cat "$root/mode") in
                replace-output) replace_output "$output" ;;
                replace-status) replace_output "$MCP_CONSOLE_RESOLVER_STATUS" ;;
            esac
            exit 0 ;;
        inspection|inspection-interrupt) printf '%s' "$root/invalid-python" > "$output"; exit 0 ;;
        unsafe-candidate) /bin/cat "$root/candidate" > "$output"; exit 0 ;;
        interrupt) message="fixture Python resolution interrupted"; checkpoint ;;
    esac
fi
exec "$root/real-uv" "$@"
