#!/usr/bin/env python3
"""OpenLamp engine daemon — run the engine WITHOUT the Stream Deck app.

Hosts engine.Engine headless: lamp connections come up, the local API serves on
127.0.0.1:8377, and the CLI (lamp.py) / MIDI overlay / any OpenLamp State client
can drive the lamps — no Stream Deck needed, no latency change (same in-process
dispatch and same persistent sockets to the lamps).

RULE — run ONE host at a time, never both:
the daemon and the Stream Deck plugin both own the single local connection each
Tuya lamp allows, and both bind port 8377. Stream Deck for the deck; this daemon
for CLI/MIDI-only sessions (see run-headless.sh).

  python3 daemon.py            # foreground (Ctrl-C to stop)
  launchd: com.openlamp.daemon.plist
"""
import os, time, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("engine", os.path.join(HERE, "engine.py"))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

def main():
    engine.log("daemon: starting engine (headless, no Stream Deck)")
    eng = engine.Engine()
    engine.log("daemon: engine up — local API on 127.0.0.1:%d" % engine.API_PORT)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        engine.log("daemon: stopping")

if __name__ == "__main__":
    main()
