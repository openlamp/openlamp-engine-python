#!/bin/sh
# Headless mode for CLI / MIDI (no Stream Deck).
# Quits the Stream Deck app (it would fight the daemon for the single lamp
# connection and port 8377), then runs the engine daemon in the foreground.
# The CLI (lamp.py) and the MIDI overlay then drive the lamps through the daemon.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
osascript -e 'tell application "Elgato Stream Deck" to quit' 2>/dev/null || true
# wait for port 8377 to be free
i=0; while "$PY" - <<'PYCHK' 2>/dev/null && [ $i -lt 15 ]; do sleep 2; i=$((i+1)); done
import socket,sys
s=socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1",8377))==0 else 1)
PYCHK
echo "Starting the OpenLamp engine daemon (headless). Ctrl-C to stop."
echo "Then use: lamp.py <color>  or the MIDI overlay (openlamp-midi)"
exec "$PY" "$HERE/daemon.py"
