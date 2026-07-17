#!/usr/bin/env python3
"""OpenLamp desktop app — headless launcher.

Runs the engine daemon (lamp connections + local API on 127.0.0.1:8377) AND the wled-midi
MIDI bridge (virtual `OpenLamp` port → lamps) in ONE process, with an optional system-tray
status icon. No Stream Deck, no terminal, no Python install for the end user — this is the
entry point PyInstaller bundles into a signed/notarised `.app` / `.exe`.

Why a packaged app: the only real barrier on the "accessible to everyone first" path is
"launch a Python process". Bundling removes it, so any frontend (a DAW, a hardware MIDI
controller, the CLI) drives the lamps with zero setup. Scope is deliberately **headless** —
status + lamp config only, not a rich control surface (that stays out of scope here).

Build: see packaging/ (openlamp.spec + build.sh + SIGNING.md). Signing/notarisation needs
your Apple Developer ID (macOS) / code-signing cert (Windows) — documented in SIGNING.md.

Run from source (for testing before bundling):  python3 app.py
"""
import os, sys, time, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    """Load a sibling module by file path (works both from source and inside a PyInstaller
    bundle, where these ship as data files next to the executable)."""
    base = getattr(sys, "_MEIPASS", HERE)          # PyInstaller unpacks bundled files here
    spec = importlib.util.spec_from_file_location(name, os.path.join(base, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_tray(status_text, on_quit):
    """Optional system-tray/menubar icon showing status + a Quit item. Returns True if a tray
    backend was available and the loop ran; False to fall back to a headless idle loop."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return False
    img = Image.new("RGB", (64, 64), (10, 10, 12))  # a tiny lamp glyph as the tray icon
    d = ImageDraw.Draw(img)
    d.ellipse((18, 12, 46, 40), fill=(0, 255, 128))
    d.rectangle((26, 40, 38, 52), fill=(120, 120, 120))
    icon = pystray.Icon("OpenLamp", img, "OpenLamp", menu=pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("Quit", lambda i, _: (on_quit(), i.stop())),
    ))
    icon.run()
    return True


def main():
    engine = _load("engine")
    midi = _load("midi")

    engine.log("app: starting engine (headless, no Stream Deck)")
    engine.Engine()                                 # brings up lamp connections + API :8377
    cfg = midi.load_cfg()
    br, midi_in, midi_out = midi.start_bridge(cfg)  # virtual MIDI port + dispatch (non-blocking)

    status = "OpenLamp running — API 127.0.0.1:%d · MIDI '%s'" % (engine.API_PORT, cfg["port_name"])
    engine.log("app: " + status)

    def shutdown():
        midi_in.close_port()
        if midi_out:
            midi_out.close_port()
        engine.log("app: stopped")

    if not _run_tray(status, shutdown):             # no tray backend -> headless idle
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()


if __name__ == "__main__":
    main()
