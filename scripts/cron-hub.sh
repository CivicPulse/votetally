#!/bin/bash
# Scheduled `votetally hub` run for cron.
#
# Lifecycle: kill any existing Chrome on the votetally profile, launch a
# fresh one with CDP enabled, scrape both Qlik sheets, kill Chrome again.
# The fresh-Chrome cycle resets Qlik's anonymous-token rate-limit state
# between runs and avoids stale window pile-up on the user's desktop.
#
# Cron has a minimal environment, so we explicitly recreate $PATH,
# $DISPLAY, and $XAUTHORITY — without these uv won't resolve and Chrome
# can't reach the X server.

set -uo pipefail
cd /home/kwhatcher/projects/votetally

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
export DISPLAY=":0"
export XAUTHORITY="$HOME/.Xauthority"

LOG="$HOME/projects/votetally/scripts/cron-hub.log"
PROFILE_DIR="$HOME/.config/chrome-votetally"

log() { echo "$(date -Is) $*" >>"$LOG"; }

kill_chrome() {
  # Match only the votetally-profile Chrome via its unique --user-data-dir
  # flag — leaves the user's regular Chrome windows alone.
  if pgrep -f "user-data-dir=$PROFILE_DIR" >/dev/null; then
    log "killing existing votetally Chrome"
    pkill -TERM -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
    # Give it 5s to exit cleanly, then SIGKILL stragglers.
    for _ in 1 2 3 4 5; do
      pgrep -f "user-data-dir=$PROFILE_DIR" >/dev/null || return 0
      sleep 1
    done
    pkill -KILL -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
    sleep 1
  fi
}

log "=== run start ==="
kill_chrome

log "launching fresh Chrome"
uv run votetally chrome --launch >>"$LOG" 2>&1
# fetcher's CDP-connect retry handles the launch race up to ~15s, but we
# give a small head-start so the first probe finds an open port.
sleep 4

log "running hub"
if uv run votetally hub >>"$LOG" 2>&1; then
  log "hub run succeeded"
else
  log "hub run failed (exit $?) — see lines above"
fi

kill_chrome
log "=== run end ==="
