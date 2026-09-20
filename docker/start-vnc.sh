#!/bin/bash
# Virtual display + VNC server, so the Goodreads login can happen in a browser
# that goodreads controls and whose cookies it can capture.
#
# There is deliberately NO websockify/noVNC listener here. x11vnc is bound to
# loopback and the app bridges to it over an authenticated WebSocket, so the
# desktop that is signed into Goodreads never gets a port of its own.
#
# Best-effort: every step is guarded, because if VNC fails the rest of
# goodreads (everything except the interactive login) still works fine.
set -u

DISPLAY_NUM=99
GEOMETRY=1280x900x24
VNC_PORT=5900

log() { echo "[vnc] $*"; }

if pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
  log "Xvfb already running on :${DISPLAY_NUM}"
else
  if ! command -v Xvfb >/dev/null 2>&1; then
    log "Xvfb not installed — the Goodreads login page will show a blank panel"
    exit 0
  fi
  Xvfb ":${DISPLAY_NUM}" -screen 0 "${GEOMETRY}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
  sleep 1
  log "started Xvfb on :${DISPLAY_NUM}"
fi

if pgrep -x x11vnc >/dev/null 2>&1; then
  log "x11vnc already running"
else
  if command -v x11vnc >/dev/null 2>&1; then
    # -localhost is load-bearing: it is what makes the app's authenticated
    # bridge the only way in. Without it VNC would be reachable from the
    # whole compose network.
    x11vnc -display ":${DISPLAY_NUM}" -rfbport "${VNC_PORT}" -localhost \
           -nopw -forever -shared -quiet >/tmp/x11vnc.log 2>&1 &
    sleep 1
    log "started x11vnc on ${VNC_PORT} (loopback only; reach it via the app)"
  else
    log "x11vnc not installed"
  fi
fi

# Confirm the loopback bind actually took, rather than trusting the flags.
if command -v ss >/dev/null 2>&1; then
  if ss -tln 2>/dev/null | grep -q "127.0.0.1:${VNC_PORT}"; then
    log "verified: ${VNC_PORT} is bound to loopback only"
  else
    log "WARNING: ${VNC_PORT} does not look loopback-bound — check the x11vnc args"
  fi
fi
