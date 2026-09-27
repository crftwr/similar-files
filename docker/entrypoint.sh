# Entry point of the NAS container: scan the folders given as arguments once
# per method, write each method's playlists to $OUT_DIR/<method>, and repeat
# every $INTERVAL seconds (0: run once and exit).
#
# Arguments that do not start with "/" go straight to the CLI, so
#   docker compose run --rm similar-files extractors
# works as well.
#
# Environment:
#   METHODS     space-separated methods (default "identical")
#   OUT_DIR     where the playlists go (default /out)
#   INTERVAL    seconds between runs; 0 runs once (default 0)
#   NICE        CPU priority of the scan, 0-19 (default 10)
#   WORKERS     parallel readers (default: the CLI's own)
#   EXTRA_ARGS  more options for every `similar-files scan`, e.g. "-v"
#
# SIGTERM (docker stop) and SIGINT cancel the running scan cleanly: the
# groups found so far are written, and the cache keeps what was extracted.

set -u

: "${METHODS:=identical}"
: "${OUT_DIR:=/out}"
: "${INTERVAL:=0}"
: "${NICE:=10}"
: "${WORKERS:=}"
: "${EXTRA_ARGS:=}"

if [ "$#" -eq 0 ]; then
    echo "similar-files-nas: name the folders to scan (compose 'command:')" >&2
    exit 2
fi
case "$1" in
    /*) ;;
    *) exec similar-files "$@" ;;
esac

stopping=0
child=
child_signal=
on_signal() {
    stopping=1
    if [ -n "$child" ]; then kill "-$child_signal" "$child" 2>/dev/null; fi
}
trap on_signal INT TERM

# Run a command in the background, so a signal reaches this shell while it
# waits, and wait until the command has really finished. The first argument
# is the signal that stops it: the CLI treats SIGINT as "cancel and write
# what you have", while other background commands ignore SIGINT here.
run() {
    child_signal=$1
    shift
    "$@" &
    child=$!
    while :; do
        wait "$child"
        status=$?
        kill -0 "$child" 2>/dev/null || break
    done
    child=
    return "$status"
}

while :; do
    failed=0
    for method in $METHODS; do
        [ "$stopping" -eq 1 ] && break
        echo "similar-files-nas: $(date '+%F %T') scanning for $method" >&2
        # shellcheck disable=SC2086  # EXTRA_ARGS is split on purpose
        run INT nice -n "$NICE" similar-files scan -m "$method" -o "$OUT_DIR/$method" --replace \
            ${WORKERS:+--workers "$WORKERS"} $EXTRA_ARGS "$@" || failed=1
    done
    if [ "$stopping" -eq 1 ] || [ "$INTERVAL" -eq 0 ]; then
        break
    fi
    echo "similar-files-nas: $(date '+%F %T') next run in $INTERVAL s" >&2
    run TERM sleep "$INTERVAL"
    [ "$stopping" -eq 1 ] && break
done
exit "$failed"
