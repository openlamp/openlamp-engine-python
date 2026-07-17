# OpenLamp State (OLS) — a WLED-compatible lamp-state protocol

**Version 2.0 · Status: draft, implemented in OpenLamp**

OLS is the command language of the OpenLamp engine. Rather than inventing a new
schema, OLS **adopts the de-facto standard of the maker world**: the
[WLED JSON state API](https://kno.wled.ge/interfaces/json-api/) — and extends it
where cheap consumer lamps (Tuya) need it.

> OLS is *WLED-compatible*. WLED is an independent open-source project; OLS and
> OpenLamp are not affiliated with or endorsed by the WLED project, Tuya Inc.,
> or Elgato.

## 1. The model: a state **patch**

A command is a JSON object. **Every field you omit stays unchanged** — exactly
like posting to a WLED device:

```json
{"col": [230, 0, 40]}                 // red — brightness untouched
{"bri": 204}                          // 80% — color untouched
{"on": true, "col": [0,100,200], "bri": 153, "tt": 20}   // blue 60%, 2s fade
```

## 2. Keys

### WLED-native (same names, same ranges)

| Key | Range | WLED | Tuya (engine translation) |
|---|---|---|---|
| `on` | `true` / `false` / `"t"` (toggle) | native | DP20 (toggle emulated) |
| `bri` | 0-255 | native | colour V / DP22 (×≈4) |
| `col` | `[r,g,b]` 0-255 | `seg[0].col[0]` | DP24 + colour mode implied |
| `cct` | 0-255 (warm→cold) | native | DP23 + white mode implied |
| `ps`  | 1-250 | preset recall | replays the lamp's stored scene |
| `tt`  | ×100 ms | native transition | **emulated fade** (paced stepping) |
| `nl`  | `{"on": true, "dur": min}` | nightlight | countdown (DP26) |
| `fx` `sx` `ix` `pal` | 0-255 / ids | effects engine | skipped (no effects engine) |

### OLS extensions (documented, namespaced by absence from WLED)

| Key | Meaning |
|---|---|
| `scene` | replay a **named scene** previously captured from the lamp (Tuya DP25 data) |
| `music` | Tuya music-reactive mode (driven by the Smart Life app's audio stream) |

### Engine commands (strings, not patches)

`blackout` (off + remember) · `restore` (undo blackout) ·
`snap:save:<name>` / `snap:<name>` (whole-rig snapshots) ·
`cycle:<c1,c2…>@ms` · `flash:<color>@ms` · `tempo:<bpm>` · `animstop` ·
`tt1:<ms>` (one-shot transition for the **next** command only — a per-action
*fondu* — vs the persistent `"transition"` config; consumed once, config untouched)

## 3. Targeting

Commands address **lamps or named groups**; empty = all. Groups live in the
config: `"groups": {"front": ["L1"], "back": ["L2"]}`.

## 4. Transport

- **Local HTTP API** (loopback only): `GET /cmd?c=<command>[&lamps=a,b]`,
  `GET /status[?full=1]`, `GET /syntax` (this contract, machine-readable)
- **WLED-compatible endpoint**: `GET /json/state` (aggregated state) and
  `POST /json/state` (apply a patch, `?lamps=` to target) — so **tools that can
  talk to a WLED can drive Tuya lamps** through the engine.

## 5. Behavior contract

- A lamp that doesn't support a key **skips it silently** (no lowest-common-
  denominator: WLED keeps its effects, Tuya keeps music/scenes).
- All sends are **acknowledged** on persistent connections; failures trigger
  reconnect + replay.
- Rate discipline: engines must pace ≤ ~4 acked commands/s per Tuya lamp
  (firmware sessions collapse beyond — measured).

## 6. Aliases (legacy tokens, frozen)

`<colorname>` · `<level>` · `bri:N` · `set:color:pct` · `white:bri:temp` ·
`scene:name` · `preset:N` · `mode:music` · `countdown:min` · `wled:fx:id[:sx[:ix]]`
· `on` `off` `toggle`

## Why 8-bit values (0-255)

OLS uses 8-bit for `bri`, `col` channels and effect params:
1. **WLED compatibility** — WLED's JSON API is 8-bit; OLS is a compatible patch.
2. **Hardware match** — RGB LEDs are driven 8 bits per channel (16.7M colors);
   Tuya's internal 0-1000 scale adds no perceptible precision.
3. **Perception match** — ~1% brightness steps sit at the eye's discrimination
   threshold; 256 levels cover it.
MIDI frontends (7-bit, 0-127) scale up x2.
