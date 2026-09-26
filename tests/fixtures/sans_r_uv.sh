#!/bin/sh
# Public resolver fixture: failures and causal interruption checkpoints.
root=$(dirname "$0")
checkpoint() {
    trap 'echo "$message" >&2; exit 1' INT
    mkfifo "$root/wait-$$"
    exec 3>"$root/alive"
    printf 1 >&3
    cat "$root/wait-$$" >/dev/null &
    waiter=$!
    printf 1 > "$root/started"
    wait "$waiter"
    exit 85
}
if [ "$(basename "$0")" = invalid-python ]; then
    [ "$#" = 3 ] && exit 0
    if [ "$(cat "$root/mode")" = inspection-interrupt ]; then
        message="fixture Python inspection interrupted"
        checkpoint
    fi
    cat "$root/invalid-inspection.json" > "$4"
    exit 0
fi
printf '%s\n' "$@" >> "$root/resolutions.log"
needs_preparation=false
for output do
    [ "$output" = py-yaml12 ] && needs_preparation=true
done
if $needs_preparation; then
    case $(cat "$root/mode") in
        failure) echo "fixture Python resolution failed" >&2; exit 1 ;;
        inspection|inspection-interrupt) printf '%s' "$root/invalid-python" > "$output"; exit 0 ;;
        unsafe-candidate) cat "$root/candidate" > "$output"; exit 0 ;;
        interrupt) message="fixture Python resolution interrupted"; checkpoint ;;
    esac
fi
exec "$root/real-uv" "$@"
