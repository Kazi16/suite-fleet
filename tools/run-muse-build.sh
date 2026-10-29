#!/bin/bash
# run-muse-build.sh <workdir> <prompt-file> [timeout-seconds]
#
# Hardened Muse lane launcher (Muse 1, 2026-10-29). Fixes the silent stall
# class seen on the Oct 28 day brief builds: launches that depended on the
# caller's shell died without a trace when the session went away, and launches
# from a non login shell could not even find the muse binary.
#
# Guarantees:
# - Runs detached in its own session (setsid), stdin from /dev/null, so SSH
#   teardown or a dropped exec channel cannot kill or hang the build.
# - Absolute path to the muse binary; runs as the invoking user (must be kazi,
#   whose saved auth the lane uses). Refuses to run as root.
# - build.log captures the full run, progress.log gets a heartbeat line every
#   60 seconds, build.pid holds the inner pid, and build.exit records the
#   terminal exit code and end time. A build either finishes with artifacts or
#   leaves a named failure; it cannot vanish silently.
# - Preflight records MemAvailable and SwapFree in build.log, because this box
#   runs with swap near full and memory pressure is a stall suspect.
set -u
MUSE_BIN=/home/kazi/.local/bin/muse
if [ "$(whoami)" = "root" ]; then
  echo "refusing to run as root: Muse lane builds run as kazi" >&2
  exit 2
fi
WORKDIR=${1:?usage: run-muse-build.sh <workdir> <prompt-file> [timeout-seconds]}
PROMPT=${2:?usage: run-muse-build.sh <workdir> <prompt-file> [timeout-seconds]}
TMO=${3:-3600}
case "$PROMPT" in
  /*) ;;
  *) PROMPT="$(pwd)/$PROMPT" ;;
esac
[ -d "$WORKDIR" ] || { echo "workdir missing: $WORKDIR" >&2; exit 2; }
[ -f "$PROMPT" ] || { echo "prompt file missing: $PROMPT" >&2; exit 2; }
[ -x "$MUSE_BIN" ] || { echo "muse binary missing: $MUSE_BIN" >&2; exit 2; }

if [ "${RUN_MUSE_DETACHED:-}" != "1" ]; then
  RUN_MUSE_DETACHED=1 setsid nohup "$0" "$WORKDIR" "$PROMPT" "$TMO" >/dev/null 2>&1 </dev/null &
  echo "launched: workdir=$WORKDIR prompt=$PROMPT timeout=${TMO}s (see build.log, build.pid, build.exit in workdir)"
  exit 0
fi

cd "$WORKDIR" || exit 2
echo $$ > build.pid
rm -f build.exit progress.log
{
  echo "start $(date -Is) user=$(whoami) workdir=$WORKDIR timeout=${TMO}s"
  echo "preflight: $(grep MemAvailable /proc/meminfo) | $(grep SwapFree /proc/meminfo)"
} > build.log
( while :; do echo "$(date -Is) alive" >> progress.log; sleep 60; done ) &
HB=$!
trap 'echo "launcher received SIGTERM $(date -Is)" >> build.log' TERM
trap 'echo "launcher received SIGINT $(date -Is)" >> build.log' INT
timeout --signal=TERM --kill-after=30 "$TMO" "$MUSE_BIN" exec --prompt-file "$PROMPT" >> build.log 2>&1 </dev/null
CODE=$?
kill "$HB" 2>/dev/null
echo "exit=$CODE end=$(date -Is)" > build.exit
echo "end $(date -Is) exit=$CODE" >> build.log
exit "$CODE"
