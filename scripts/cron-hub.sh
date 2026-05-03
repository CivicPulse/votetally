#!/bin/bash
# Scheduled `votetally hub` run for cron.
#
# Lifecycle: kill any existing votetally-profile Chrome, launch a fresh
# graphical Chrome via `votetally chrome --launch`, scrape both Qlik
# sheets, then kill it again. Headless was tried but Cloudflare Turnstile
# fingerprints headless Chrome and blocks sos.ga.gov before the dashboard
# iframe even loads. The persistent profile dir (~/.config/chrome-votetally)
# carries the cf_clearance cookie across restarts so Turnstile only
# re-challenges if the cookie expires (~30 minutes typical), which is
# fine because the cron cadence is hours apart.
#
# Requirements: user must be logged in to a graphical session (cron
# borrows DISPLAY/XAUTHORITY from the live GDM session). If the user
# logs out or reboots without re-logging-in, runs fail loudly.

set -uo pipefail
cd /home/kwhatcher/projects/votetally

# X11 session env. Detected once: DISPLAY=:1 (Pop!_OS GDM gave the user
# session :1, leaving :0 for the login greeter), XAUTHORITY at GDM's
# per-user runtime path. UID 1000 is hardcoded — single-user laptop.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
export DISPLAY=":1"
export XAUTHORITY="/run/user/1000/gdm/Xauthority"

LOG="$HOME/projects/votetally/scripts/cron-hub.log"
PROFILE_DIR="$HOME/.config/chrome-votetally"
CDP_PORT=9222

log() { echo "$(date -Is) $*" >>"$LOG"; }

kill_chrome() {
  if pgrep -f "user-data-dir=$PROFILE_DIR" >/dev/null; then
    log "killing prior votetally Chrome"
    pkill -TERM -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      pgrep -f "user-data-dir=$PROFILE_DIR" >/dev/null || return 0
      sleep 1
    done
    pkill -KILL -f "user-data-dir=$PROFILE_DIR" 2>/dev/null || true
    sleep 1
  fi
}

log "=== run start ==="

# Sanity: bail early with a clear message if the X server isn't reachable
# (user logged out, or display number changed). This turns a silent
# "iframe never loaded" failure deep in patchright into something the
# user can spot in one log line.
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  log "X server $DISPLAY not reachable (XAUTHORITY=$XAUTHORITY) — is the user logged in?"
  log "=== run end (skipped) ==="
  exit 1
fi

kill_chrome

log "launching votetally Chrome"
uv run votetally chrome --launch >>"$LOG" 2>&1

# Wait up to 20s for CDP to bind. fetcher's own 15s retry covers anything
# past that; probing here just gives a clearer log line when it's ready.
for i in $(seq 1 20); do
  if curl -sf --max-time 1 http://127.0.0.1:$CDP_PORT/json/version >/dev/null; then
    log "CDP ready after ${i}s"
    break
  fi
  sleep 1
done

log "running hub"
if uv run votetally hub >>"$LOG" 2>&1; then
  log "hub run succeeded"
else
  log "hub run failed (exit $?) — see lines above"
fi

kill_chrome
log "=== run end ==="
