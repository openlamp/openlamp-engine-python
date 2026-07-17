#!/usr/bin/env python3
"""lamp-bench — a tiny bench for WLED lamp tinkerers.

Surfaces the numbers the OpenLamp engine learned the hard way, as a standalone tool:
firmware/hardware info, command **latency** (the round-trip a key press pays), and —
opt-in — the **command ceiling** (how many commands/second this lamp sustains before it
starts dropping them; WLED firmware crashes an ESP past a handful, which is why the
stage tools rate-limit). stdlib only, no deps.

    lamp-bench.py                      # read the config, probe every WLED lamp (safe)
    lamp-bench.py 192.168.8.128        # probe a lamp by IP/host directly
    lamp-bench.py --pings 40           # more latency samples
    lamp-bench.py --ceiling            # ALSO find the command ceiling (stresses the lamp)
    lamp-bench.py --check              # ALSO run a conformance check (write → read back)

Config: WLED lamps ("type":"wled","host":"<ip>") from $OPENLAMP_LAMPS_DIR/tuya-lamps.json,
else ~/.config/openlamp/tuya-lamps.json, else next to lamp.py. Only WLED is probed (its
local HTTP API makes this trivial); Tuya lamps are listed but skipped.
"""
import argparse, json, os, statistics, sys, time, urllib.request, urllib.error

TIMEOUT = 2.0


def _config_lamps():
    for p in (os.path.join(os.path.expanduser(os.environ.get("OPENLAMP_LAMPS_DIR", "")), "tuya-lamps.json"),
              os.path.expanduser("~/.config/openlamp/tuya-lamps.json"),
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tuya-lamps.json")):
        if p and os.path.isfile(p):
            try:
                return json.load(open(p)).get("lamps", []), p
            except Exception:
                pass
    return [], None


def _get(host, path, timeout=TIMEOUT):
    with urllib.request.urlopen(f"http://{host}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def _post(host, body, timeout=TIMEOUT):
    req = urllib.request.Request(f"http://{host}/json/state", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def info(host):
    try:
        j = _get(host, "/json/info")
    except Exception as e:
        return None, str(e)
    return {
        "name": j.get("name"), "ver": j.get("ver"), "arch": j.get("arch"),
        "leds": (j.get("leds") or {}).get("count"), "ip": j.get("ip"),
        "signal": (j.get("wifi") or {}).get("signal"), "uptime": j.get("uptime"),
    }, None


def latency(host, n):
    # POST {"v":true}: WLED returns the full state and changes NOTHING — a real write
    # round-trip with no visible flicker. Measures what a key press actually costs.
    ms = []
    for _ in range(n):
        t = time.perf_counter()
        try:
            _post(host, {"v": True})
            ms.append((time.perf_counter() - t) * 1000.0)
        except Exception:
            ms.append(None)
        time.sleep(0.05)
    ok = [x for x in ms if x is not None]
    drops = len(ms) - len(ok)
    if not ok:
        return None
    ok.sort()
    return {"min": ok[0], "avg": statistics.mean(ok), "max": ok[-1],
            "p95": ok[min(len(ok) - 1, int(len(ok) * 0.95))],
            "jitter": (statistics.pstdev(ok) if len(ok) > 1 else 0.0), "drops": drops, "n": len(ms)}


def ceiling(host, cap=8, per_rate=2.0):
    # Ramp the command rate and watch for the first drops. Gentle + bails at the first
    # sign of trouble — we approach the ceiling, we don't hammer past it (that reboots the
    # ESP). Returns (last_good_rate, hit_cap): hit_cap=True means we reached the safety cap
    # with NO drop, so the real ceiling is higher but deliberately left un-probed.
    print("    ceiling probe (safety-capped — this stresses the lamp; Ctrl-C to abort)…")
    last_ok = 0.0
    r = 2.0
    while r <= cap:
        interval = 1.0 / r
        sent = fails = 0
        end = time.monotonic() + per_rate
        while time.monotonic() < end:
            t = time.monotonic()
            try:
                _post(host, {"v": True}, timeout=1.0)
            except Exception:
                fails += 1
            sent += 1
            dt = interval - (time.monotonic() - t)
            if dt > 0:
                time.sleep(dt)
        drop_pct = 100.0 * fails / max(1, sent)
        print(f"      {r:.1f}/s → {sent} sent, {fails} dropped ({drop_pct:.0f}%)")
        if drop_pct > 10.0:
            return last_ok, False        # first real trouble → stop, don't push further
        last_ok = r
        r += 1.0
    return cap, True                     # reached the cap with no drop — not pushed further


def check(host):
    # Conformance: does the lamp actually APPLY + REFLECT commands? Write a value, read the
    # state back, verify. Catches non-compliant / buggy firmware. Restores the state after.
    try:
        saved = _get(host, "/json/state")
    except Exception as e:
        print(f"    ✗ can't read state for the check: {e}"); return
    def col0(st):
        return ((st.get("seg") or [{}])[0].get("col") or [[0, 0, 0]])[0][:3]
    tests = [
        ("set colour → red", {"on": True, "bri": 200, "seg": [{"col": [[255, 0, 0]]}]},
         lambda st: col0(st) == [255, 0, 0]),
        ("set brightness → 128", {"on": True, "bri": 128}, lambda st: abs(st.get("bri", 0) - 128) <= 4),
        ("turn off", {"on": False}, lambda st: st.get("on") is False),
        ("turn on", {"on": True}, lambda st: st.get("on") is True),
        ("crossfade (tt) → blue", {"tt": 8, "seg": [{"col": [[0, 0, 255]]}]}, lambda st: col0(st) == [0, 0, 255]),
    ]
    print("    conformance (write → read back):")
    for label, patch, verify in tests:
        ok = False
        try:
            _post(host, patch); time.sleep(0.2)
            ok = verify(_get(host, "/json/state"))
        except Exception:
            ok = False
        print(f"      {'✓' if ok else '✗'} {label}")
    try:                                                # restore what was there before
        _post(host, {"on": saved.get("on", True), "bri": saved.get("bri", 128),
                     "seg": [{"col": ((saved.get("seg") or [{}])[0].get("col") or [[0, 100, 200]])}]})
    except Exception:
        pass


def bench(host, name, pings, do_ceiling, do_check):
    label = f"{name} ({host})" if name else host
    print(f"\n■ {label}")
    inf, err = info(host)
    if err:
        print(f"    ✗ unreachable: {err}")
        return
    print(f"    WLED {inf['ver']} · {inf['arch']} · {inf['leds']} LED(s) · "
          f"wifi {inf['signal']}% · up {inf['uptime']}s")
    lat = latency(host, pings)
    if lat:
        print(f"    latency: {lat['avg']:.0f} ms avg · {lat['min']:.0f} min / {lat['max']:.0f} max · "
              f"p95 {lat['p95']:.0f} · jitter ±{lat['jitter']:.0f} ms"
              + (f" · {lat['drops']}/{lat['n']} dropped" if lat['drops'] else ""))
    if do_ceiling:
        c, hit_cap = ceiling(host)
        if hit_cap:
            print(f"    command ceiling: **≥ {c:.0f} cmd/s** — reached the {c:.0f}/s safety cap "
                  f"with NO drop; the real ceiling is higher but was NOT pushed further "
                  f"(would risk crashing the ESP). Stage tools cap at 4.2/s for headroom.")
        else:
            print(f"    command ceiling: ~{c:.0f} cmd/s sustained (started dropping above that). "
                  f"Stage tools cap at 4.2/s for headroom.")
    if do_check:
        check(host)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lamp-bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hosts", nargs="*", help="WLED IP(s)/host(s) to probe; omit to read the config")
    ap.add_argument("--pings", type=int, default=20, help="latency samples per lamp (default 20)")
    ap.add_argument("--ceiling", action="store_true", help="also probe the command ceiling (stresses the lamp)")
    ap.add_argument("--check", action="store_true", help="also run a conformance check (write commands, read them back; restores state after)")
    args = ap.parse_args(argv)

    targets = [(h, None) for h in args.hosts]
    if not targets:
        lamps, path = _config_lamps()
        if not lamps:
            print("No hosts given and no config found. Pass a WLED IP, e.g. lamp-bench.py 192.168.1.50")
            return 2
        print(f"config: {path}")
        for l in lamps:
            if l.get("type") == "wled" and l.get("host"):
                targets.append((l["host"], l.get("name")))
            else:
                print(f"  ⏭ {l.get('name')} — not WLED (skipped)")
    if not targets:
        print("No WLED lamps to probe.")
        return 1
    for host, name in targets:
        try:
            bench(host, name, args.pings, args.ceiling, args.check)
        except KeyboardInterrupt:
            print("\n  aborted."); break
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
