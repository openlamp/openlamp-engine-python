#!/usr/bin/env python3
"""lamp.py - Tuya local control. Changing the COLOR does not touch brightness;
brightness presets are separate.
  lamp.py <color>     -> change color, keep current brightness
  lamp.py <preset>    -> lueur | veilleuse | tamise | moyen | fort | max
  lamp.py bri:<1-100> -> exact brightness (keep color)
  lamp.py on | off
Colors (Kelly, most-differentiable first): jaune violet orange bleuclair rouge vert rose bleu
Multi-network: if the known IP stops answering, the lamp is re-found by its MAC (ARP)
across every active subnet of the machine (e.g. a home LAN plus a separate stage/travel router).
If the OpenLamp daemon is running it holds the single local connection each Tuya lamp
allows, so this CLI routes through the plugin's local API; otherwise it drives directly.
Callable from Bome: Program Change -> Execute file -> lamp.py <action>.
"""
import sys, os, re, json, subprocess, threading
import tinytuya

# config lookup (CLI only; the plugin has its own discovery in engine.py):
#   1) $OPENLAMP_LAMPS  2) ~/.config/openlamp/tuya-lamps.json  3) next to this script
_STD_CFG = os.path.expanduser("~/.config/openlamp/tuya-lamps.json")
CONFIG = (os.environ.get("OPENLAMP_LAMPS")
          or (_STD_CFG if os.path.isfile(_STD_CFG)
              else os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuya-lamps.json")))
COLORS = {"jaune":(255,210,0),"violet":(150,70,170),"orange":(255,125,0),
          "bleuclair":(130,195,255),"rouge":(230,0,40),"vert":(0,200,80),
          "rose":(255,130,170),"bleu":(0,100,200)}
PALIERS = {"lueur":1, "veilleuse":10, "tamise":30, "moyen":55, "fort":80, "max":100}

def norm_mac(s):
    return ":".join(str(int(o,16)) for o in s.lower().split(":"))

def local_subnets():
    # Every /24 of the machine's active interfaces: a lamp may sit on the home LAN
    # or, away, on a separate travel-router subnet. Scanning only the last known
    # subnet missed the lamp whenever it switched networks.
    out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    nets = []
    for m in re.finditer(r"inet (\d+\.\d+\.\d+)\.\d+", out):
        p = m.group(1)
        if p not in nets and not p.startswith("127.") and not p.startswith("169.254"):
            nets.append(p)
    return nets

_swept = set()   # subnets already scanned this run — 2 lamps don't rescan twice
_sweep_lock = threading.Lock()  # 2 fallback threads: one scans, the other waits

def find_ip_by_mac(mac, subnet):
    with _sweep_lock:
        if subnet not in _swept:
            _swept.add(subnet)
            ps = [subprocess.Popen(["ping","-c1","-t1","%s.%d"%(subnet,i)],
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(1,255)]
            for p in ps: p.wait()
    out = subprocess.run(["arp","-an"], capture_output=True, text=True).stdout
    t = norm_mac(mac)
    for ln in out.splitlines():
        m = re.search(r"at ([0-9a-f:]+)", ln.lower())
        ipm = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", ln)
        if m and ipm and norm_mac(m.group(1)) == t:
            return ipm.group(1)
    return None

def find_ip_any_subnet(mac):
    for sn in local_subnets():
        ip = find_ip_by_mac(mac, sn)
        if ip: return ip
    return None

def scale(rgb, pct):
    # scale brightness (V = max(r,g,b)/255) to the wanted percent, keeping hue+sat
    mx = max(rgb) or 1
    f = (255.0 * pct / 100.0) / mx
    return tuple(max(0, min(255, round(c*f))) for c in rgb)

def cur_bri_pct(dps):
    # current brightness 0-100: in color mode = V of colour_data (DP24, HHHHSSSSVVVV, 0-1000)
    mode = dps.get('21', 'colour')
    cd = dps.get('24', '')
    if mode in ('colour','color') and len(cd) >= 12:
        try: return max(1, min(100, round(int(cd[8:12],16)/10.0)))
        except Exception: pass
    if dps.get('22') is not None:
        try: return max(1, min(100, round(int(dps['22'])/10.0)))
        except Exception: pass
    return 60

def apply(bulb, action, state):
    if action == "off": return bulb.turn_off()
    if action == "on":  return bulb.turn_on()
    bulb.turn_on()
    st = bulb.status()
    dps = st.get('dps', {}) if isinstance(st, dict) else {}
    if action in COLORS:                                  # COLOR: keep current brightness
        state["color"] = action
        r,g,b = scale(COLORS[action], cur_bri_pct(dps))
        return bulb.set_colour(r,g,b)
    if action in PALIERS or action.startswith("bri:"):    # BRIGHTNESS: keep color (state)
        pct = PALIERS[action] if action in PALIERS else int(action.split(":")[1])
        pct = max(1, min(100, pct))
        color = state.get("color", "bleu")
        r,g,b = scale(COLORS.get(color, COLORS["bleu"]), pct)
        return bulb.set_colour(r,g,b)
    raise SystemExit("unknown action: " + action)

def _try(lamp, ip, action, state):
    b = tinytuya.BulbDevice(lamp["device_id"], ip, lamp["local_key"], version=3.5)
    # short timeouts: a dead IP must fail in ~3 s, not 25 s
    # (tinytuya defaults = 5 s x 5 retries -> 3 min for 2 lamps, unusable live)
    b.set_socketTimeout(3)
    b.set_socketRetryLimit(1)
    r = apply(b, action, state)
    if isinstance(r, dict) and r.get("Error"): raise RuntimeError(r["Error"])
    return ip

def control(lamp, action, state):
    ips = lamp.setdefault("ips", {})
    if lamp.get("ip"):                                    # migrate old single-IP schema
        ips.setdefault(".".join(lamp["ip"].split(".")[:3]), lamp.pop("ip"))
    # 1) known IPs of the Mac's ACTIVE subnets — the last subnet where THIS lamp
    #    answered first ("last" PER LAMP: L1 can be on one subnet while L2 is on another),
    # 2) fallback: MAC rediscovery
    last = lamp.get("last")
    subs = ([last] if last in ips else []) + \
           [s for s in local_subnets() if s in ips and s != last]
    # 2 passes over known IPs: a transient radio miss (lamp Wi-Fi power-save, flaky
    # AP) must not trigger a pointless 20 s rescan
    for _pass in (0, 1):
        for sn in subs:
            try:
                ip = _try(lamp, ips[sn], action, state)
                lamp["last"] = sn
                return ip
            except SystemExit: raise
            except Exception: continue
    ip = find_ip_any_subnet(lamp["mac"])
    if not ip: return None
    try:
        ip = _try(lamp, ip, action, state)
        sn = ".".join(ip.split(".")[:3])
        ips[sn] = ip                                      # remember for next time
        lamp["last"] = sn
        return ip
    except SystemExit: raise
    except Exception: return None

def to_action(args):
    # 2-arg MIDI form: <col|bri> <code>   col=color (index 0-7), bri=brightness 0..127 (0=off,127=max)
    if len(args) >= 2:
        kind, val = args[0].lower(), int(args[1])
        if kind in ("bri","brightness","b"):
            return "off" if val <= 0 else "bri:%d" % max(1, min(100, round(val/127.0*100)))
        if kind in ("col","color","couleur","c"):
            names = list(COLORS.keys())
            return names[max(0, min(len(names)-1, val))]
        raise SystemExit("usage 2 args: lamp.py <col 0-7|bri 0-127>")
    return args[0].lower()

def try_plugin_api(action):
    # If the Stream Deck plugin is running it holds the SINGLE local connection each
    # Tuya lamp allows -> go through its front door (API 127.0.0.1, instant via its
    # persistent connections). Otherwise: direct control. Same for Bome or any other
    # frontend: GET /cmd?c=<action>.
    import urllib.request, urllib.parse
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:8377/cmd?c=" + urllib.parse.quote(action),
                timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit("usage: lamp.py <couleur|palier|bri:N|on|off>  |  lamp.py <col|bri> <code>")
    action = to_action(args)
    api = try_plugin_api(action)
    if api is not None:
        for name in api.get("targets", []):
            print("  %s: %s %s (via plugin)" % (name, action, "OK" if api.get("ok") else "⚠"))
        return
    cfg = json.load(open(CONFIG))
    state = cfg.setdefault("state", {"color": "bleu"})
    # les 2 lampes en parallele : ~2.5 s au lieu de ~5 s en serie — reactivite scene
    results = {}
    def worker(lamp):
        results[lamp["name"]] = control(lamp, action, state)
    ts = [threading.Thread(target=worker, args=(l,)) for l in cfg["lamps"]]
    for t in ts: t.start()
    for t in ts: t.join()
    for lamp in cfg["lamps"]:
        ip = results.get(lamp["name"])
        lamp.pop("_changed", False)
        print("  %s: %s %s" % (lamp["name"], action, ("OK @ "+ip) if ip else "INJOIGNABLE"))
    json.dump(cfg, open(CONFIG, "w"), indent=2)

if __name__ == "__main__":
    main()
