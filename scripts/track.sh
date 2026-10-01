#!/usr/bin/env bash
# Runs a command, measures its peak RAM (macOS `/usr/bin/time -l`), and appends
# a row to logs/resource_usage.md automatically.
#
# Usage: scripts/track.sh "label for the log" -- command arg1 arg2 ...
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$DIR/logs/resource_usage.md"

LABEL="$1"; shift
if [ "${1:-}" = "--" ]; then shift; fi

TMPERR="$(mktemp)"
/usr/bin/time -l "$@" 2> "$TMPERR"
STATUS=$?

PEAK_BYTES="$(grep 'maximum resident set size' "$TMPERR" | awk '{print $1}')"
REAL_TIME="$(grep '^ *[0-9.]* real' "$TMPERR" | awk '{print $1}')"
PEAK_MB="n/a"
if [ -n "${PEAK_BYTES:-}" ]; then
  PEAK_MB="$(( PEAK_BYTES / 1024 / 1024 )) MB"
fi

DATE="$(date +%Y-%m-%d)"
CMD_STR="$*"
STATUS_NOTE=""
if [ "$STATUS" -ne 0 ]; then
  STATUS_NOTE=" (exit code $STATUS)"
fi

echo "| $DATE | $LABEL | \`$CMD_STR\` | peak RSS: $PEAK_MB | ${REAL_TIME:-n/a}s wall |$STATUS_NOTE auto-logged |" >> "$LOG"

# Surface the wrapped command's own stderr + time's report, since we captured it above.
cat "$TMPERR" >&2
rm -f "$TMPERR"
exit "$STATUS"
