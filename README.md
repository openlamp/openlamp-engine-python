# OpenLamp Engine

The **core layer** of the OpenLamp family: instant, 100% local control of smart LED
lamps (**WLED** recommended, Tuya also supported), exposed through a stable command contract —
**OpenLamp State (OLS)**, a WLED-compatible JSON state patch (see [OLS.md](OLS.md)).

[![PyPI — openlamp-lamp](https://img.shields.io/pypi/v/openlamp-lamp?label=openlamp-lamp&color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/openlamp-lamp/)

Part of the [OpenLamp](https://github.com/openlamp/openlamp) family:

| Layer | Repo | Role |
|---|---|---|
| Engine (this repo) | `openlamp/engine` | drivers + dispatcher + local API + daemon + CLI (Python reference) |
| Engine, JS port | [engine-js](https://github.com/openlamp/engine-js) | same contract on Node/tuyapi — for JS-first environments |
| Ableton Live frontend | [live](https://github.com/openlamp/live) | drive lamps from a Live set (emits the wled-midi convention) |
| MIDI convention | [wled-midi](https://github.com/openlamp/wled-midi) | the MIDI↔WLED spec this engine implements (see `midi.py`) |
| Ableton Link / tempo | [openlamp-midi](https://github.com/openlamp/midi) | beat / tempo follow (beatsync) |

## MIDI control

The engine is the **reference implementation of the
[wled-midi](https://github.com/openlamp/wled-midi) convention**. Run
[`midi.py`](midi.py) to open a virtual MIDI input port (default `OpenLamp`) and drive
the lamps from any DAW or controller — notes → colours, CC → brightness/effects,
Program Change → presets, MIDI clock → tempo:

```bash
python3 midi.py            # opens the "OpenLamp" virtual MIDI port
```

Any frontend that speaks the convention works: [Ableton](https://github.com/openlamp/live),
the Stream Deck **MIDI** plugin (`se.trevligaspel.midi`, for scripted sequences), or a
hardware pad/fader controller. (This replaces the old `openlamp-midi`
bridge — the MIDI control path now lives here; `openlamp-midi` keeps the Ableton Link
tempo-follow.)

## Install the `lamp` CLI (PyPI)

The one-shot command-line frontend is published as **[`openlamp-lamp`](https://pypi.org/project/openlamp-lamp/)** on PyPI:

```bash
pip install openlamp-lamp

lamp rouge            # set colour (keeps brightness)
lamp bri:60           # brightness 1-100
lamp veilleuse        # a brightness preset (lueur/veilleuse/tamise/moyen/fort/max)
lamp on | off
```

It finds your lamp config (with local keys) from `$OPENLAMP_LAMPS`, else
`~/.config/openlamp/tuya-lamps.json` (template at the bottom of this README). If the
OpenLamp daemon is running, the CLI routes through its local API
(`127.0.0.1:8377`) for instant response; otherwise it drives the lamps directly.

> Only the CLI (`lamp.py`) ships on PyPI. The engine + headless daemon stay in this
> repo (they couple to a local file layout — a clean engine package is a follow-up).

## What's inside

- **`engine.py`** — the engine: one thread per lamp with a *persistent* connection
  (sub-200 ms commands), the OLS dispatcher, groups, snapshots, animations
  (cycle/flash/tempo), connect-time sync, a rainbow welcome sweep, and the local
  API on `127.0.0.1:8377` (`/cmd`, `/status`, `/syntax`, plus the optional
  WLED-compat `/json/state`). Frontend-agnostic: its only upward link is an
  `on_change` hook.
- **`daemon.py`** — headless host: runs the engine without any frontend app.
- **`run-headless.sh`** — one command to switch to CLI/MIDI-only mode.
- **`lamp-doctor.sh`** (macOS) — one-command diagnosis of "lamps unreachable",
  testing the three causes in order: Mac on the wrong Wi-Fi / router down /
  lamp powered off or radio-napping. Never trust the router's web panel (it
  renders from service-worker cache even with the router dead) — this script is
  the ground truth.
- **`lamp.py`** — the CLI (also Bome-callable): `lamp.py vert`, `lamp.py bri:40`…
  Routes through the local API when a host runs, drives lamps directly otherwise.
- **`bin/lamp-bench.py`** — a small bench for WLED tinkerers: firmware/hardware info,
  command **latency** (the round-trip a press pays), and — opt-in — the **command ceiling**
  (`--ceiling`) the lamp sustains before dropping, plus a **conformance check** (`--check`,
  write commands and read them back). stdlib only. `lamp-bench.py <ip>`.
- **`com.openlamp.daemon.plist`** — launchd autostart for the daemon.
- **`app.py`** + **`packaging/`** — the **headless desktop app**: one process running the engine
  + the wled-midi bridge, with an optional tray, bundled by PyInstaller into a `.app`/`.exe`.
  See [Desktop app](#desktop-app-headless-no-install).
- **`OLS.md`** — the OpenLamp State contract. **`TUYA-KEYS.md`** — how to get your
  lamps' local keys (official Tuya cloud API, one-time).

## Desktop app (headless, no-install)

`app.py` runs the engine + the wled-midi MIDI bridge in one process (tray optional). Build a
distributable bundle:

```sh
packaging/build.sh      # -> dist/OpenLamp.app (macOS) / dist/OpenLamp/ (Windows)
```

The **build is free** and the unsigned bundle runs for personal use (right-click → Open, or
`xattr -d com.apple.quarantine OpenLamp.app`).

> **⏸ Code-signing + notarising is deferred — a *later* task, done only when we publish a public
> download.** It exists only to remove the Gatekeeper/SmartScreen warning for third-party
> downloaders, and its one cost (Apple Developer Program, 99 $/yr) isn't worth paying until there's
> a public release. The full how-to is ready in **[`packaging/SIGNING.md`](packaging/SIGNING.md)** for
> that day. Decision recorded 2026-07-16.

## Ableton Link / GPL boundary

The engine carries **no Ableton Link / aalink code** and never links it. Beat-sync from
an [Ableton Link](https://github.com/Ableton/link) session is handled by a **separate
process** — [`openlamp-midi`](https://github.com/openlamp/midi) (`beatsync.py`) — which
reaches the engine **only** over this local HTTP API on `127.0.0.1:8377` (`/cmd`,
`/status`).

Because Ableton Link (via [aalink](https://pypi.org/project/aalink/)) is **GPLv2**, that
separation matters: the GPL combined-work lives entirely inside the `openlamp-midi`
process. The engine is a genuinely separate program communicating over HTTP, so it stays
under its own permissive license and the GPL obligation does not propagate to it. Full
write-up in the [`openlamp-midi` README](https://github.com/openlamp/midi#architecture--the-ableton-link--gpl-boundary).

## One host at a time — the rule

Every host binds port 8377 (and a Tuya lamp additionally accepts only **one** local connection).
So run **either** the Stream Deck plugin (it embeds this engine in-process) **or**
`daemon.py` — never both. Deck sessions → plugin; CLI/MIDI-only sessions → daemon.

## Why 8-bit values (0–255)

OLS uses 8-bit for brightness and per-channel color, for three reasons:
1. **WLED compatibility** — WLED's JSON API is 8-bit; OLS is a compatible patch.
2. **It matches the hardware** — RGB LEDs are driven 8 bits per channel
   (16.7 M colors); Tuya's internal 0–1000 scale adds no perceptible precision.
3. **It matches perception** — ~1 % brightness steps are at the threshold of what
   the eye distinguishes; 256 levels cover that.
MIDI frontends (7-bit, 0–127) scale up ×2 — plenty for stage cues.

## Config

You can point the engine at an external, synced folder (Google Drive/Dropbox) that
holds `lamp.py` + `tuya-lamps.json` by setting `OPENLAMP_LAMPS_DIR` to that path;
otherwise `tuya-lamps.json` sits next to `lamp.py` (never committed — it contains your local
keys). Template:

```json
{
  "lamps": [
    {"name": "L1", "mac": "aa:bb:cc:dd:ee:ff", "device_id": "…", "local_key": "…",
     "ips": {"192.168.1": "192.168.1.50"}}
  ],
  "groups": {"front": ["L1"]},
  "sync": {"enabled": true, "state": {"on": true, "col": [0, 100, 200], "bri": 153}}
}
```

## Test hardware (budget setup)

The engine is developed and tested on this cheap, off-the-shelf rig —
reproduce it for well under €40 for a two-lamp stereo stage:

- **Bulbs — [Athom WLED 7 W Color Bulb](https://www.athom.tech)** (E27, **ESP32-C3**,
  RGB + tunable white, WLED-preflashed, ~€13 each). WLED ships already flashed — put the
  bulb on Wi-Fi and it's auto-discovered. This is the reference bulb every timing figure
  is measured on (~45 ms/command, the latency-comp floor beatsync uses).
- **Socket / holder — [TobeBright E27 corded lamp holder with inline switch](https://www.amazon.fr/dp/B0DXPQLJCZ)**
  (up to 100 W, a few € on Amazon). Turns a bare bulb into a standalone plug-in stage
  lamp — no fixture required, just screw the bulb in and plug it to mains.

## Publishing to PyPI (maintainer)

`openlamp-lamp` publishes via **Trusted Publishing (OIDC)** — no token in the repo.
`.github/workflows/publish.yml` builds + publishes on each GitHub **Release**. One-time:

1. **PyPI → Add a pending publisher** (<https://pypi.org> → *Publishing*): project
   `openlamp-lamp`, owner `openlamp`, repo `engine`, workflow `publish.yml`, environment `pypi`.
2. **GitHub → Settings → Environments → New** → `pypi`.
3. Bump `version` in `pyproject.toml`, commit, cut a GitHub Release (tag `v0.1.0`) →
   the workflow builds and publishes. Then `pip install openlamp-lamp` works everywhere.

## Credits

Made by **[@Beennnn](https://github.com/Beennnn)** (**[OpenLamp](https://github.com/openlamp)**) with the help of Claude. **WLED is the recommended, tested path** (validated on Athom RGBCW bulbs, ~45 ms/command). Feedback: open an issue on [engine](https://github.com/openlamp/engine/issues).
