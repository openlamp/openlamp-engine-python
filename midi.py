#!/usr/bin/env python3
"""OpenLamp MIDI — reference implementation of the wled-midi convention, in the engine.

Opens a virtual MIDI input port (default "OpenLamp") and translates incoming MIDI into
OpenLamp State (OLS = WLED-compatible JSON patch) commands, per the
github.com/openlamp/wled-midi convention (v0.2). It POSTs to the engine's local API
(127.0.0.1:8377/cmd); the engine owns the persistent device connections.

Three modes (config "mode"):
  - "lamp" (default; alias "group") — CHANNEL = a lamp group; NOTES/CC/PC as below. MIDI-1.0-native.
  - "strip" — note pitch -> a LED POSITION on a strip (piano-guide / strip instrument).
    Polyphonic, velocity -> brightness, individual-LED payload. Four position functions:
    "interpolate" (note range proportional to the strip), "keymap" (piano-aligned via
    LEDs-per-key), "direct" (note = LED index), "zone" (hold notes -> light the LED RANGE
    between the lowest and highest held note, in the channel's colour — split-zone display).
    See wled-midi SPEC §13 "strip mode".
    Prior art (attribution, not affiliation): the LEDs-per-key note->LED alignment is the
    idea established by onlaj's Piano-LED-Visualizer (github.com/onlaj/Piano-LED-Visualizer),
    the reference open-source piano->WS2812 project. We reuse that idea and name the origin;
    onlaj is not affiliated with this project and has not reviewed or endorsed it.
  - "mpe" — CHANNEL = a per-note voice: play the lamps expressively. Note-on lights a
    voice (pitch -> base hue), channel pressure -> brightness, CC74 slide -> saturation,
    pitch-bend -> hue shift. See wled-midi SPEC "mpe mode".

Lamp-mode model (wled-midi v0.6 — the default config of the unified syntax):
  - CHANNEL = a lamp group/target (1-16 per port; channel 1 = "all").
  - NOTES are organised in zones:
      LOOKS (59-68)      what the lamp SHOWS, mutually exclusive: black + 7 hues + white + effect.
      POWER/UTIL (48-56) on/off/toggle/blackout/restore/solid.
      MODIFIERS (72-75)  overlay the current look: beat toggle, flash.
  - CC (1-8) shapes the current look: bri/cct/hue/sat/fx/sx/ix/pal.
  - VELOCITY (optional, off by default) sets the look's brightness.
  - PROGRAM CHANGE recalls a WLED preset (ps = n+1).
  - MIDI CLOCK derives tempo.

Feedback (optional, config "feedback"): opens a virtual MIDI OUTPUT port "OpenLamp Feedback"
that reflects state back in the SAME wled-midi language — so a control surface lights its pad
LEDs / moves its faders to match. A look note-on echoes that look (and a note-off of the
previously-active look on that channel, so exactly one look shows as "on"); util on/off and
CC 1-8 echo too. Route this port back into your controller (or Bome) to map it to LEDs.

Beat (note 72) toggles a per-channel beat flag; the pulsing is driven here from the
incoming MIDI clock (accent on each beat, trough between, modulating the look's
brightness). Enable clock output to the port in your DAW. A richer Link-based version
(phase-accurate downbeat accent via openlamp-midi beatsync) is a future refinement.
In-process dispatch (skipping loopback HTTP) is another future optimisation.

Run:  python3 midi.py            (Ctrl-C to quit)
Config: midi-mapping.json (auto-written next to this file; remappable).
"""
import json, os, time, colorsys, threading, urllib.request, urllib.parse
import rtmidi

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "midi-mapping.json")
API = "http://127.0.0.1:8377"

# Fallbacks when the device's fxcount/palcount aren't known (wled-midi SPEC §5).
FALLBACK_FXCOUNT = 118
FALLBACK_PALCOUNT = 71

# wled-midi v0.2.0 default map. Numbers remappable in midi-mapping.json; the CC
# transforms and zone semantics are the stable contract.
DEFAULT = {
    "port_name": "OpenLamp",
    # ONE MIDI CHANNEL PER TARGET (device / segment / group), or "all".
    "channels": {"1": "all", "2": "front", "3": "back", "4": "L1", "5": "L2"},
    # LOOKS (what the lamp shows) — note -> [r,g,b], or "effect".
    "looks": {
        "59": [0, 0, 0],
        "60": [255, 0, 0], "61": [255, 85, 0], "62": [255, 200, 0], "63": [0, 255, 0],
        "64": [0, 200, 255], "65": [0, 0, 255], "66": [255, 0, 170], "67": [255, 255, 255],
        "68": "effect",
    },
    # POWER / UTIL — note -> OLS command string.
    "util": {
        "48": '{"on":false}', "50": '{"on":true}', "52": '{"on":"t"}',
        "53": "blackout", "55": "restore", "56": '{"fx":0}',
    },
    # MODIFIERS (overlay the current look) — note -> modifier name.
    "modifiers": {"72": "beat", "73": "flash"},
    "cc": {"1": "bri", "2": "cct", "3": "hue", "4": "sat",
           "5": "fx", "6": "sx", "7": "ix", "8": "pal"},
    # high-res CC (opt-in): ingest 14-bit values from MSB+LSB pairs (CC N + CC N+32, the
    # standard MIDI-1.0 high-resolution scheme most DAWs emit) for step-free fades — 16384
    # levels instead of 128. The true MIDI-2.0 UMP 32-bit path is a future profile (needs a
    # UMP transport; rtmidi is MIDI 1.0). See wled-midi SPEC §11.
    "highres_cc": False,
    "feedback": False,           # true -> open "OpenLamp Feedback" MIDI OUT reflecting state
    "feedback_port": "OpenLamp Feedback",
    # real-state feedback (opt-in): subscribe to a WLED device's WebSocket (ws://<host>/ws) and
    # reflect its ACTUAL state as MIDI on the feedback port — so external changes (the WLED app, a
    # physical toggle, effects the device computes) also show on the controller, not just the
    # commands the engine sent. Needs `pip install websocket-client`. host "" = disabled.
    "feedback_wled": {"host": "", "channel": 1},
    "programs": [],              # empty -> Program Change n maps to WLED preset n+1
    "velocity_to_bri": False,    # true -> a look note-on also sets brightness from velocity
    "clock_tempo": False,        # true -> report BPM from MIDI clock as tempo:<bpm>
    "clock_channel": 1,
    "beat_width_ticks": 6,       # ticks (of 24/beat) the accent lasts before the trough
    "beat_base_ratio": 0.25,     # trough brightness = ratio x the look's set brightness
    # per-window batching for the beat pulse (SPEC §7): coalesce the pulse's brightness
    # sends into ONE per target per window, so many beat-mode channels don't burst past
    # WLED's ~30 req/s. 0 disables (send each pulse immediately). ~33 ms = ~30 Hz.
    "beat_batch_ms": 33,
    # "lamp" (default, channel = lamp group; "group" is accepted as an alias), "strip"
    # (note -> LED position) or "mpe" (channel = per-note voice).
    "mode": "lamp",
    # strip mode — note pitch -> LED position (wled-midi SPEC §13). The lo/hi/lpk/firstnote
    # values are CALIBRATION: measure your own strip once (dense strips have no physical
    # 1-LED-per-key; software maps notes onto pixels). Defaults assume an 88-key range
    # (A0=21 .. C8=108) on a ~176-px strip (144 LED/m over ~1.22 m).
    "strip": {
        "ledcount": 176,           # number of LEDs on the strip
        "posfn": "keymap",         # "interpolate" | "keymap" | "direct"
        "lo": 21, "hi": 108,       # interpolate: MIDI note range mapped across the strip
        "lpk": 2.0,                # keymap: LEDs per key (measured: ledcount / key-span)
        "firstnote": 21,           # keymap/direct: MIDI note sitting at LED index 0
        "color": [0, 255, 128],    # the lit colour (per note), used when the channel isn't in hand_colors
        # Synthesia-style: MIDI channel -> colour (e.g. left hand ch1 blue, right hand ch2 green).
        # Empty = every channel uses "color". {"1":[0,120,255], "2":[0,255,120]} = L/R hands.
        "hand_colors": {},
        "velocity_to_bri": True,   # scale the colour by note velocity (playing dynamics)
        # note-off fade: pixels ramp to black over fade_ms instead of cutting instantly.
        # WLED has no per-pixel fade for individual-LED writes, so a background ticker
        # ramps them here — coalesced into ONE /json/state per frame (respects §7 batching).
        # 0 disables (instant off). ~30 fps => ~fade_ms/33 frames per release.
        "fade_ms": 250,
        "fade_fps": 30,
    },
    "mpe": {
        "master": 1,             # MPE master channel (global; ignored for per-voice)
        "first_member": 2,       # member channels start here (voices)
        "voices": ["L1", "L2", "L3", "L4"],   # lamp pool, one per active voice
        "bend_hue_range": 0.5,   # pitch-bend at full = this much hue shift (0..1 spectrum)
    },
}


def load_cfg():
    if os.path.exists(CONFIG):
        c = dict(DEFAULT); c.update(json.load(open(CONFIG))); return c
    json.dump(DEFAULT, open(CONFIG, "w"), indent=2)
    return dict(DEFAULT)


def send(cmd, lamps):
    q = urllib.parse.quote(cmd)
    if lamps:
        q += "&lamps=" + ",".join(lamps)
    try:
        urllib.request.urlopen(API + "/cmd?c=" + q, timeout=3).read()
    except Exception as e:
        print("  ! engine API unreachable (is the daemon running?):", e)


class Feedback:
    """Reflects state back out a MIDI OUT port, in the wled-midi language, so a control
    surface can mirror it (pad LEDs / faders). No-op when no port is configured."""
    def __init__(self, out):
        self.out = out
    def note_on(self, chan, note):
        if self.out: self.out.send_message([0x90 | (chan - 1), note & 0x7F, 127])
    def note_off(self, chan, note):
        if self.out: self.out.send_message([0x80 | (chan - 1), note & 0x7F, 0])
    def cc(self, chan, num, val):
        if self.out: self.out.send_message([0xB0 | (chan - 1), num & 0x7F, val & 0x7F])


class WledStateReflector:
    """Maps a WLED /json/state object to feedback MIDI, emitting only what changed. This is the
    testable core of real-state feedback: on/off -> util note (50/48), bri -> CC1, the primary
    segment's colour -> CC3 hue + CC4 sat, its effect -> CC5. Independent of the WS transport."""
    def __init__(self, fb, chan=1, fxcount=FALLBACK_FXCOUNT):
        self.fb, self.chan, self.fxcount = fb, chan, fxcount
        self.prev = {}
    def reflect(self, state):
        c = self.chan
        on = state.get("on")
        if on is not None and on != self.prev.get("on"):
            self.fb.note_on(c, 50 if on else 48)         # 50 = on, 48 = off
        bri = state.get("bri")
        if bri is not None and bri != self.prev.get("bri"):
            self.fb.cc(c, 1, round(bri / 255 * 127))
        seg = (state.get("seg") or [{}])[0]
        col = (seg.get("col") or [None])[0]
        if col and col[:3] != self.prev.get("col"):
            r, g, b = col[:3]
            h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            self.fb.cc(c, 3, round(h * 127))             # hue
            self.fb.cc(c, 4, round(s * 127))             # sat
            self.prev["col"] = col[:3]
        fx = seg.get("fx")
        if fx is not None and fx != self.prev.get("fx"):
            self.fb.cc(c, 5, round(fx / max(1, self.fxcount - 1) * 127))
        self.prev.update({"on": on, "bri": bri, "fx": fx})


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.fb = Feedback(None)      # replaced in main() when "feedback" is enabled
        self.active_look = {}         # per-channel currently-shown look note (for exclusive LED)
        self.clock_ticks = 0
        self.clock_t0 = None
        self.last_bpm = None
        self.hue = {}                 # per-channel hue (continuous colour)
        self.sat = {}                 # per-channel saturation
        self.cc_msb = {}              # high-res CC: chan -> {base-cc -> last MSB value}
        self.fx = {}                  # per-channel WLED effect {fx,sx,ix,pal}
        self.beat = {}                # per-channel beat-mode on/off
        self.bri = {}                 # per-channel set brightness (for beat pulse + settle)
        self.voices = {}              # MPE mode: member-channel -> voice {lamp,base,hue,sat}
        self.strip_held = {}          # strip mode: note -> {"leds":[..], "col":(r,g,b)} currently lit
        self.zone_held = {}           # strip "zone" posfn: chan -> [held notes]
        self.zone_lit = {}            # strip "zone" posfn: chan -> [led indices currently lit]
        self.strip_fading = {}        # strip mode: led index -> {"col":(r,g,b), "t0":monotonic} ramping to black
        self.strip_lock = threading.Lock()   # guards strip_held/strip_fading (MIDI cb + fade thread)
        self.beat_pending = {}        # per-window beat batching: target-key -> (cmd, target); latest wins
        self.beat_lock = threading.Lock()

    def _target(self, chan):
        chans = self.cfg.get("channels")
        if chans is not None:
            tgt = chans.get(str(chan))
            if tgt is None:
                return None            # unmapped channel -> ignore
            return [] if tgt in ("all", "*", "") else [tgt]
        want = self.cfg.get("channel", 1)
        if want and chan != want:
            return None
        return self.cfg.get("lamps") or []

    def _fb_look(self, chan, note):
        """Feedback: looks are mutually exclusive, so unlight the previously-active look on
        this channel and light the new one — the controller shows exactly one look 'on'."""
        prev = self.active_look.get(chan)
        if prev is not None and prev != note:
            self.fb.note_off(chan, prev)
        self.fb.note_on(chan, note)
        self.active_look[chan] = note

    def _effect_cmd(self, chan):
        f = self.fx.setdefault(chan, {"fx": 1, "sx": 128, "ix": 128, "pal": 0})
        if f["fx"] == 0:               # entering the effect look needs a visible effect
            f["fx"] = 1
        return '{"fx":%d,"sx":%d,"ix":%d,"pal":%d}' % (f["fx"], f["sx"], f["ix"], f["pal"])

    def on_note(self, note, vel, chan, target):
        if vel <= 0:                   # note-on only
            return
        n = str(note)
        looks = self.cfg.get("looks", {})
        util = self.cfg.get("util", {})
        mods = self.cfg.get("modifiers", {})
        if n in looks:
            v = looks[n]
            if v == "effect":
                cmd = self._effect_cmd(chan)
            else:
                r, g, b = v
                if self.cfg.get("velocity_to_bri"):
                    cmd = '{"col":[%d,%d,%d],"fx":0,"bri":%d}' % (r, g, b, round(vel / 127 * 255))
                else:
                    cmd = '{"col":[%d,%d,%d],"fx":0}' % (r, g, b)
            print("  ch->%s  look note %d -> %s" % (target or "all", note, cmd))
            send(cmd, target)
            self._fb_look(chan, note)                # reflect: light this look, unlight the last
        elif n in util:
            send(util[n], target)
            self.fb.note_on(chan, note)              # reflect the util press (momentary confirm)
        elif n in mods:
            m = mods[n]
            if m == "flash":
                send("flash:blanc@300", target)     # engine handles the momentary flash
            elif m == "beat":
                st = not self.beat.get(chan, False)  # toggle per channel
                self.beat[chan] = st
                print("  ch->%s  beat %s" % (target or "all", "on" if st else "off"))
                if not st:                           # leaving beat mode: settle brightness
                    send('{"bri":%d}' % self.bri.get(chan, 255), target)

    def _apply_cc(self, kind, f, target, chan):
        """Apply a CC by its normalised fraction f in [0,1] — shared by 7-bit and 14-bit paths."""
        if kind == "bri":
            self.bri[chan] = round(f * 255)
            send('{"bri":%d}' % self.bri[chan], target)
        elif kind == "cct":
            send('{"cct":%d}' % round(f * 255), target)
        elif kind in ("hue", "sat"):
            if kind == "hue":
                self.hue[chan] = f
            else:
                self.sat[chan] = f
            r, g, b = colorsys.hsv_to_rgb(self.hue.get(chan, 0.0), self.sat.get(chan, 1.0), 1.0)
            send('{"col":[%d,%d,%d]}' % (round(r*255), round(g*255), round(b*255)), target)
        elif kind in ("fx", "sx", "ix", "pal"):
            fx = self.fx.setdefault(chan, {"fx": 0, "sx": 128, "ix": 128, "pal": 0})
            if kind == "fx":
                fx["fx"] = round(f * (FALLBACK_FXCOUNT - 1))
            elif kind == "pal":
                fx["pal"] = round(f * (FALLBACK_PALCOUNT - 1))
            else:
                fx[kind] = round(f * 255)
            send('{"fx":%d,"sx":%d,"ix":%d,"pal":%d}' % (fx["fx"], fx["sx"], fx["ix"], fx["pal"]), target)

    def on_cc(self, num, val, target, chan):
        hr = self.cfg.get("highres_cc")
        if hr and 33 <= num <= 40:                 # LSB of a high-res pair (base CC = num - 32)
            base = num - 32
            kind = self.cfg["cc"].get(str(base))
            if not kind:
                return
            msb = self.cc_msb.get(chan, {}).get(base, 0)
            self._apply_cc(kind, ((msb << 7) | val) / 16383.0, target, chan)   # 14-bit, step-free
            return
        kind = self.cfg["cc"].get(str(num))
        if not kind:
            return
        self.fb.cc(chan, num, val)                 # reflect value (motor faders / LED rings)
        if hr:
            self.cc_msb.setdefault(chan, {})[num] = val   # remember MSB; the LSB (num+32) refines it
        self._apply_cc(kind, val / 127.0, target, chan)   # 7-bit (provisional when high-res)

    def on_program(self, prog, target):
        progs = self.cfg.get("programs") or []
        if not progs:
            send('{"ps":%d}' % (prog + 1), target)    # wled-midi core: PC n -> preset n+1
            return
        if not (0 <= prog < len(progs)):
            return
        name = str(progs[prog])
        if name.startswith("snap:") or name.startswith("preset:"):
            cmd = name
        elif name.startswith("ps:"):
            cmd = "preset:" + name[3:]
        elif name.isdigit():
            cmd = "preset:" + name
        else:
            cmd = "scene:" + name
        send(cmd, target)

    def _beat_emit(self, cmd, target):
        """Route a beat-pulse brightness send through the per-window batcher (latest value
        per target wins), or send immediately when batching is disabled (beat_batch_ms=0)."""
        if not self.cfg.get("beat_batch_ms", 0):
            send(cmd, target); return
        key = ",".join(target) if target else "*"      # "*" = all lamps
        with self.beat_lock:
            self.beat_pending[key] = (cmd, target)      # coalesce: one send per target per window

    def flush_beat(self):
        """Flush the batched beat pulses — one /json/state per distinct target this window."""
        with self.beat_lock:
            if not self.beat_pending:
                return
            items = list(self.beat_pending.values())
            self.beat_pending.clear()
        for cmd, target in items:
            send(cmd, target)

    def on_clock(self):
        self.clock_ticks += 1
        pos = self.clock_ticks % 24                    # 24 ticks = 1 beat

        # Beat modifier (note 72): pulse each beat-mode channel — accent on the beat,
        # trough between — by modulating the look's brightness. Needs MIDI clock flowing.
        # Sends go through the per-window batcher (SPEC §7) so many channels don't burst.
        if any(self.beat.values()):
            width = self.cfg.get("beat_width_ticks", 6)
            ratio = self.cfg.get("beat_base_ratio", 0.25)
            for chan, on in self.beat.items():
                if not on:
                    continue
                base = self.bri.get(chan, 255)
                if pos == 0:                           # downbeat of this beat: accent
                    self._beat_emit('{"bri":%d}' % base, self._target(chan) or [])
                elif pos == width:                     # shortly after: trough
                    self._beat_emit('{"bri":%d}' % max(1, round(base * ratio)), self._target(chan) or [])

        # Tempo derivation (optional): report BPM once per beat.
        if self.cfg.get("clock_tempo") and pos == 0:
            now = time.monotonic()
            if self.clock_t0 is not None:
                bpm = round(60.0 / (now - self.clock_t0))
                if 20 <= bpm <= 300 and bpm != self.last_bpm:
                    self.last_bpm = bpm
                    tgt = self._target(self.cfg.get("clock_channel", 1)) or []
                    send("tempo:%d" % max(20, min(120, bpm)), tgt)
            self.clock_t0 = now

    # ----- strip mode: note pitch -> LED position (piano-guide / strip instrument) -----

    def _strip_leds(self, note):
        """note -> the LED indices it lights, per the configured position function."""
        s = self.cfg.get("strip", {})
        N = int(s.get("ledcount", 176))
        fn = s.get("posfn", "interpolate")
        if fn == "direct":                             # note = LED index (sequencer/pixel)
            i = note - int(s.get("firstnote", 0))
            return [i] if 0 <= i < N else []
        if fn == "keymap":                             # piano-aligned: LEDs-per-key
            # LEDs-per-key alignment — prior art: onlaj/Piano-LED-Visualizer (no affiliation).
            lpk = float(s.get("lpk", 2.0))
            first = int(s.get("firstnote", 21))
            center = round((note - first) * lpk)
            span = max(1, round(lpk))                   # light ~one key's worth of pixels
            return [i for i in range(center, center + span) if 0 <= i < N]
        lo = int(s.get("lo", 21)); hi = int(s.get("hi", 108))   # interpolate: proportional
        if hi <= lo:
            return []
        i = round((note - lo) / (hi - lo) * (N - 1))
        return [i] if 0 <= i < N else []

    def _strip_paint(self, leds, rgb):
        """Set specific LEDs to a colour via WLED's individual-LED payload seg[].i."""
        if not leds:
            return
        r, g, b = rgb
        body = ",".join("%d,[%d,%d,%d]" % (i, r, g, b) for i in leds)
        send('{"on":true,"seg":[{"i":[%s]}]}' % body, [])

    def on_strip_note_on(self, note, vel, chan=1):
        s = self.cfg.get("strip", {})
        leds = self._strip_leds(note)
        if not leds:
            return
        # channel -> hand colour (Synthesia L/R), falling back to the single "color".
        r, g, b = s.get("hand_colors", {}).get(str(chan)) or s.get("color", [0, 255, 128])
        if s.get("velocity_to_bri", True):             # dynamics: dim the pixel by velocity
            k = vel / 127.0
            r, g, b = round(r * k), round(g * k), round(b * k)
        with self.strip_lock:
            self.strip_held[note] = {"leds": leds, "col": (r, g, b)}
            for i in leds:                             # re-lit: cancel any in-flight fade
                self.strip_fading.pop(i, None)
        self._strip_paint(leds, (r, g, b))

    def on_strip_note_off(self, note):
        fade_ms = self.cfg.get("strip", {}).get("fade_ms", 0)
        with self.strip_lock:
            entry = self.strip_held.pop(note, None)
            if not entry:
                return
            if fade_ms and fade_ms > 0:                # hand the pixels to the fade ticker
                now = time.monotonic()
                for i in entry["leds"]:
                    self.strip_fading[i] = {"col": entry["col"], "t0": now}
                return
        self._strip_paint(entry["leds"], (0, 0, 0))    # fade disabled: instant off

    def fade_tick(self):
        """Called ~fade_fps/s by a background thread: ramp fading pixels to black,
        coalescing all of them into ONE individual-LED write per frame (§7 batching)."""
        fade_ms = self.cfg.get("strip", {}).get("fade_ms", 0)
        if not fade_ms or fade_ms <= 0:
            return
        now = time.monotonic()
        payload = []
        with self.strip_lock:
            if not self.strip_fading:
                return
            done = []
            for i, st in self.strip_fading.items():
                k = 1.0 - (now - st["t0"]) * 1000.0 / fade_ms
                if k <= 0:
                    payload.append((i, (0, 0, 0))); done.append(i)
                else:
                    r, g, b = st["col"]
                    payload.append((i, (round(r * k), round(g * k), round(b * k))))
            for i in done:
                del self.strip_fading[i]
        if payload:                                    # one POST for the whole frame's pixels
            body = ",".join("%d,[%d,%d,%d]" % (i, c[0], c[1], c[2]) for i, c in payload)
            send('{"seg":[{"i":[%s]}]}' % body, [])

    # --- strip "zone" posfn: hold notes -> light the LED range between lowest & highest held ---
    def _zone_pos(self, note):
        s = self.cfg.get("strip", {}); N = int(s.get("ledcount", 176))
        lo = int(s.get("lo", 21)); hi = int(s.get("hi", 108))
        if hi <= lo:
            return None
        i = round((note - lo) / (hi - lo) * (N - 1))
        return max(0, min(N - 1, i))

    def zone_update(self, chan):
        notes = self.zone_held.get(chan, [])
        prev = self.zone_lit.get(chan, [])
        ps = [p for p in (self._zone_pos(n) for n in notes) if p is not None]
        nxt = list(range(min(ps), max(ps) + 1)) if ps else []
        nset = set(nxt)
        r, g, b = self.cfg.get("strip", {}).get("hand_colors", {}).get(str(chan)) \
            or self.cfg.get("strip", {}).get("color", [0, 255, 128])
        parts = ["%d,[0,0,0]" % i for i in prev if i not in nset]      # clear pixels leaving the zone
        parts += ["%d,[%d,%d,%d]" % (i, r, g, b) for i in nxt]         # paint the zone in the channel colour
        self.zone_lit[chan] = nxt
        if parts:
            send('{"on":true,"seg":[{"i":[%s]}]}' % ",".join(parts), [])

    def zone_on(self, note, chan):
        self.zone_held.setdefault(chan, []).append(note); self.zone_update(chan)

    def zone_off(self, note, chan):
        a = self.zone_held.get(chan)
        if a and note in a:
            a.remove(note)
        self.zone_update(chan)

    def dispatch_strip(self, msg):
        typ, chan = msg[0] & 0xF0, (msg[0] & 0x0F) + 1
        zone = self.cfg.get("strip", {}).get("posfn") == "zone"
        if typ == 0x90 and msg[2] > 0:
            self.zone_on(msg[1], chan) if zone else self.on_strip_note_on(msg[1], msg[2], chan)
        elif typ == 0x80 or (typ == 0x90 and msg[2] == 0):
            self.zone_off(msg[1], chan) if zone else self.on_strip_note_off(msg[1])
        elif typ == 0xB0 and self.cfg["cc"].get(str(msg[1])) == "bri":
            send('{"bri":%d}' % round(msg[2] / 127 * 255), [])   # CC1 -> global brightness

    # ----- MPE mode: channel = per-note voice (pressure->bri, CC74->sat, bend->hue) -----

    def _mpe_voice_lamp(self, chan):
        mpe = self.cfg.get("mpe", {})
        pool = mpe.get("voices") or ["L1", "L2", "L3", "L4"]
        first = mpe.get("first_member", 2)
        return pool[(chan - first) % len(pool)]

    def _mpe_send_col(self, chan):
        v = self.voices.get(chan)
        if not v:
            return
        r, g, b = colorsys.hsv_to_rgb(v["hue"] % 1.0, v["sat"], 1.0)
        send('{"col":[%d,%d,%d]}' % (round(r*255), round(g*255), round(b*255)), [v["lamp"]])

    def on_mpe_note_on(self, chan, note, vel):
        lamp = self._mpe_voice_lamp(chan)
        base = (note % 12) / 12.0                      # pitch class -> base hue
        self.voices[chan] = {"lamp": lamp, "base": base, "hue": base, "sat": 1.0}
        r, g, b = colorsys.hsv_to_rgb(base, 1.0, 1.0)
        send('{"on":true,"col":[%d,%d,%d],"bri":%d}' % (
            round(r*255), round(g*255), round(b*255), round(vel / 127 * 255)), [lamp])

    def on_mpe_note_off(self, chan):
        v = self.voices.pop(chan, None)
        if v:
            send('{"bri":0}', [v["lamp"]])

    def on_mpe_pressure(self, chan, val):              # Z axis -> brightness
        v = self.voices.get(chan)
        if v:
            send('{"bri":%d}' % round(val / 127 * 255), [v["lamp"]])

    def on_mpe_slide(self, chan, val):                 # Y axis (CC74) -> saturation
        v = self.voices.get(chan)
        if v:
            v["sat"] = val / 127.0
            self._mpe_send_col(chan)

    def on_mpe_bend(self, chan, lsb, msb):             # X axis -> hue shift
        v = self.voices.get(chan)
        if v:
            bend = ((msb << 7) | lsb) - 8192           # -8192..+8191
            rng = self.cfg.get("mpe", {}).get("bend_hue_range", 0.5)
            v["hue"] = v["base"] + (bend / 8192.0) * rng
            self._mpe_send_col(chan)

    def dispatch_mpe(self, msg):
        typ, chan = msg[0] & 0xF0, (msg[0] & 0x0F) + 1
        if chan == self.cfg.get("mpe", {}).get("master", 1):
            return                                     # master channel: global, ignored here
        if typ == 0x90 and msg[2] > 0:
            self.on_mpe_note_on(chan, msg[1], msg[2])
        elif typ == 0x80 or (typ == 0x90 and msg[2] == 0):
            self.on_mpe_note_off(chan)
        elif typ == 0xD0:                              # channel pressure
            self.on_mpe_pressure(chan, msg[1])
        elif typ == 0xB0 and msg[1] == 74:             # CC74 slide / timbre
            self.on_mpe_slide(chan, msg[2])
        elif typ == 0xE0:                              # pitch bend
            self.on_mpe_bend(chan, msg[1], msg[2])

    def dispatch(self, msg):
        status = msg[0]
        if self.cfg.get("mode") == "mpe":
            self.dispatch_mpe(msg); return
        if self.cfg.get("mode") == "strip":
            if status != 0xF8:                        # strip mode ignores MIDI clock
                self.dispatch_strip(msg)
            return
        if status == 0xF8:                            # clock -> global tempo
            self.on_clock(); return
        typ, chan = status & 0xF0, (status & 0x0F) + 1
        target = self._target(chan)
        if target is None:
            return
        if typ == 0x90:
            self.on_note(msg[1], msg[2], chan, target)
        elif typ == 0xB0:
            self.on_cc(msg[1], msg[2], target, chan)
        elif typ == 0xC0:
            self.on_program(msg[1], target)


def start_bridge(cfg):
    """Open the virtual MIDI port(s) and wire dispatch — NON-blocking. Returns
    (bridge, midi_in, midi_out). Used by main() and by the packaged desktop app
    (app.py) so the bridge can run alongside the in-process engine."""
    br = Bridge(cfg)
    midi_in = rtmidi.MidiIn()
    midi_in.open_virtual_port(cfg["port_name"])
    midi_in.ignore_types(timing=False)                # keep MIDI clock (0xF8)

    midi_out = None
    fw = cfg.get("feedback_wled", {})
    if cfg.get("feedback") or fw.get("host"):          # open the feedback port if either wants it
        midi_out = rtmidi.MidiOut()
        midi_out.open_virtual_port(cfg.get("feedback_port", "OpenLamp Feedback"))
        br.fb = Feedback(midi_out)
        print("  feedback: MIDI OUT '%s' open (state reflected in wled-midi language)."
              % cfg.get("feedback_port", "OpenLamp Feedback"))

    if fw.get("host"):                                 # real-state reflection from a WLED device
        reflector = WledStateReflector(br.fb, fw.get("channel", 1))
        def wled_ws_loop():
            try:
                import websocket                        # websocket-client
            except Exception:
                print("  ! feedback_wled needs: pip install websocket-client"); return
            url = "ws://%s/ws" % fw["host"]
            while True:
                try:
                    ws = websocket.create_connection(url, timeout=5)
                    ws.send('{"v":true}')               # request the full state from WLED
                    while True:
                        data = json.loads(ws.recv())
                        st = data.get("state", data)    # WLED pushes {"state":..,"info":..}
                        if isinstance(st, dict):
                            reflector.reflect(st)
                except Exception as e:
                    print("  ! WLED ws (%s) reconnecting: %s" % (url, e)); time.sleep(3)
        threading.Thread(target=wled_ws_loop, daemon=True).start()
        print("  feedback_wled: subscribing to ws://%s/ws (real-state reflection)" % fw["host"])

    def cb(event, data=None):
        msg, _ = event
        try:
            br.dispatch(msg)
        except Exception as e:
            print("  ! dispatch error:", e)

    midi_in.set_callback(cb)

    # strip-mode note-off fade: tick the ramp off the MIDI thread so releases fade
    # smoothly without blocking dispatch. Idle when nothing is fading (cheap dict check).
    if cfg.get("mode") == "strip" and cfg.get("strip", {}).get("fade_ms", 0):
        fps = max(1, int(cfg.get("strip", {}).get("fade_fps", 30)))
        def fade_loop():
            while True:
                time.sleep(1.0 / fps)
                try:
                    br.fade_tick()
                except Exception as e:
                    print("  ! fade error:", e)
        threading.Thread(target=fade_loop, daemon=True).start()

    # beat-pulse batching: flush the coalesced pulses once per window (idle when empty).
    batch_ms = cfg.get("beat_batch_ms", 0)
    if batch_ms and batch_ms > 0:
        def beat_flush_loop():
            while True:
                time.sleep(batch_ms / 1000.0)
                try:
                    br.flush_beat()
                except Exception as e:
                    print("  ! beat flush error:", e)
        threading.Thread(target=beat_flush_loop, daemon=True).start()

    return br, midi_in, midi_out


def main():
    cfg = load_cfg()
    br, midi_in, midi_out = start_bridge(cfg)
    print("OpenLamp MIDI (wled-midi v0.2) — virtual port '%s' open." % cfg["port_name"])
    print("Route your DAW/controller MIDI to it. Ctrl-C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        midi_in.close_port()
        if midi_out:
            midi_out.close_port()


if __name__ == "__main__":
    main()
