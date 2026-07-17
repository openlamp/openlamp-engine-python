#!/usr/bin/env python3
"""Unit tests for midi.py — headless, no lamps, no MIDI hardware.

Stubs the native `rtmidi` import, then feeds MIDI messages through Bridge.dispatch
while capturing the OLS/WLED commands it would POST. Asserts the emitted JSON matches
the wled-midi convention (group mode + MPE mode).

Run:  python3 test_midi.py
"""
import sys, types, os, json

# Stub rtmidi so importing midi.py works without the native extension.
sys.modules.setdefault("rtmidi", types.ModuleType("rtmidi"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import midi  # noqa: E402

FAILS = []


def bridge(**over):
    cfg = json.loads(json.dumps(midi.DEFAULT))   # deep copy
    cfg.update(over)
    br = midi.Bridge(cfg)
    sent = []
    midi.send = lambda cmd, lamps: sent.append((cmd, lamps))
    return br, sent


def check(label, got, want):
    if got != want:
        FAILS.append(f"{label}\n     got:  {got}\n     want: {want}")


# ---- group mode ----------------------------------------------------------------

def test_looks_and_util():
    br, sent = bridge()
    br.dispatch([0x90, 60, 100])                  # note-on ch1 note60 (red look)
    check("look red", sent[-1], ('{"col":[255,0,0],"fx":0}', []))
    br.dispatch([0x90, 67, 100])                  # white
    check("look white", sent[-1], ('{"col":[255,255,255],"fx":0}', []))
    br.dispatch([0x90, 68, 100])                  # effect look
    check("look effect", sent[-1], ('{"fx":1,"sx":128,"ix":128,"pal":0}', []))
    br.dispatch([0x90, 48, 100])                  # off
    check("util off", sent[-1], ('{"on":false}', []))
    br.dispatch([0x90, 53, 100])                  # blackout
    check("util blackout", sent[-1], ("blackout", []))


def test_note_off_ignored():
    br, sent = bridge()
    br.dispatch([0x80, 60, 0])                    # note-off: no action
    check("note-off ignored", len(sent), 0)
    br.dispatch([0x90, 60, 0])                    # note-on vel0 = note-off: no action
    check("vel0 ignored", len(sent), 0)


def test_cc():
    br, sent = bridge()
    br.dispatch([0xB0, 1, 100])                   # CC1 bri
    check("cc bri", sent[-1], ('{"bri":201}', []))
    br.dispatch([0xB0, 5, 64])                    # CC5 fx (fallback fxcount 118)
    check("cc fx", sent[-1], ('{"fx":59,"sx":128,"ix":128,"pal":0}', []))


def test_program_change():
    br, sent = bridge()
    br.dispatch([0xC0, 4])                        # PC 4 -> preset 5
    check("pc preset", sent[-1], ('{"ps":5}', []))


def test_channel_routing():
    br, sent = bridge()
    br.dispatch([0x91, 60, 100])                  # ch2 -> group "front"
    check("ch2 -> front", sent[-1][1], ["front"])
    br.dispatch([0x9F, 60, 100])                  # ch16 unmapped -> ignored
    check("ch16 unmapped ignored", sent[-1][1], ["front"])   # unchanged


def test_velocity_to_bri():
    br, sent = bridge(velocity_to_bri=True)
    br.dispatch([0x90, 60, 64])                   # red at velocity 64 (round(64/127*255)=129)
    check("velocity->bri", sent[-1], ('{"col":[255,0,0],"fx":0,"bri":129}', []))


def test_beat_toggle():
    br, sent = bridge()
    br.dispatch([0x90, 72, 100])                  # beat on
    check("beat on state", br.beat.get(1), True)
    br.dispatch([0x90, 72, 100])                  # beat off -> settle bri
    check("beat off state", br.beat.get(1), False)
    check("beat off settles bri", sent[-1], ('{"bri":255}', []))


def test_beat_pulse_on_clock():
    br, sent = bridge()
    br.dispatch([0x90, 72, 100])                  # beat on (ch1)
    sent.clear()
    for _ in range(24):                           # one beat of clock ticks
        br.dispatch([0xF8])
    bries = [c for c, _ in sent if c.startswith('{"bri"')]
    check("beat pulses (accent+trough)", len(bries) >= 2, True)


# ---- MPE mode ------------------------------------------------------------------

def test_mpe_voice():
    br, sent = bridge(mode="mpe")
    br.dispatch([0x91, 60, 100])                  # member ch2, note60 -> voice L1, red
    check("mpe note-on", sent[-1], ('{"on":true,"col":[255,0,0],"bri":201}', ["L1"]))
    br.dispatch([0xD1, 64])                       # channel pressure ch2 -> bri (round(64/127*255)=129)
    check("mpe pressure->bri", sent[-1], ('{"bri":129}', ["L1"]))
    br.dispatch([0xB1, 74, 0])                    # CC74 ch2 sat=0 -> white (any hue)
    check("mpe slide->sat(white)", sent[-1], ('{"col":[255,255,255]}', ["L1"]))
    before = len(sent)
    br.dispatch([0xE1, 127, 127])                 # pitch bend ch2 -> hue shift (fires a col)
    check("mpe bend fires col", len(sent) > before, True)
    br.dispatch([0x81, 60, 0])                    # note-off -> release
    check("mpe note-off", sent[-1], ('{"bri":0}', ["L1"]))


def test_mpe_voice_pool_rotates():
    br, sent = bridge(mode="mpe")
    br.dispatch([0x92, 60, 100])                  # member ch3 -> voice index 1 -> L2
    check("mpe ch3 -> L2", sent[-1][1], ["L2"])


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("all engine/midi.py tests passed")


if __name__ == "__main__":
    main()
