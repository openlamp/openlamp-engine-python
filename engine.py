#!/usr/bin/env python3
"""OpenLamp engine — local control of smart LED lamps (Tuya + WLED), frontend-agnostic.

This is the CORE layer: drivers (persistent connections, one thread per lamp),
the dispatcher (OpenLamp State = WLED-compatible state patch + legacy aliases),
groups, snapshots, animations (cycle/flash/tempo), connect-time sync, the rainbow
welcome sweep, and the local API on 127.0.0.1:8377 (/cmd /status /syntax
+ /json/state WLED-compat).

It knows NOTHING about Stream Deck. Frontends embed it two ways:
- in-process : the Stream Deck plugin subclasses Engine (see plugin.py);
- out-of-process : daemon.py hosts Engine headless, and the CLI (lamp.py) or the
  MIDI overlay talk to the local API.
Run ONE host at a time (plugin OR daemon): each Tuya lamp accepts a single local
connection, and both hosts bind port 8377.

The only upward link is the on_change hook — a callable fired ~1.5 s after a
dispatch so a frontend can refresh whatever it displays. No other engine code
touches a frontend.

Config: tuya-lamps.json (shared source of truth with every frontend).
Log: ~/Library/Logs/ElgatoStreamDeck/com.openlamp.lamps.log
"""
import sys, os, json, time, threading, queue, importlib.util, socket
import subprocess
import urllib.request, urllib.parse
import http.server
import tinytuya

if getattr(sys, "frozen", False):
    # PyInstaller bundle (store/alpha): binary in <.sdPlugin root>/bin/lampsd,
    # lamp.py + tuya-lamps.json are at the ROOT of the .sdPlugin
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    _FALLBACK = HERE
else:
    # dev mode: this file lives in scripts/lamps/sd-plugin, lamp.py one level up
    HERE = os.path.dirname(os.path.abspath(__file__))
    _FALLBACK = os.path.dirname(HERE)
# Optional external source-of-truth for lamp.py + tuya-lamps.json, e.g. a synced
# folder (Google Drive/Dropbox) shared with the CLI. Set OPENLAMP_LAMPS_DIR to that
# path; otherwise the files bundled next to the plugin/script are used.
_EXT = os.path.expanduser(os.environ.get("OPENLAMP_LAMPS_DIR", ""))
LAMPS_DIR = _EXT if (_EXT and os.path.isdir(_EXT)) else _FALLBACK
CONFIG = os.path.join(LAMPS_DIR, "tuya-lamps.json")
LOGFILE = os.path.expanduser("~/Library/Logs/ElgatoStreamDeck/com.openlamp.lamps.log")

API_PORT = 8377   # local API (127.0.0.1 only) — see LocalApi

_log_lock = threading.Lock()
def log(*a):
    with _log_lock:
        try:
            with open(LOGFILE, "a") as f:
                f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
        except Exception:
            pass

# lamp.py provides COLORS, PALIERS, scale, cur_bri_pct, local_subnets, find_ip_any_subnet
spec = importlib.util.spec_from_file_location("lamp", os.path.join(LAMPS_DIR, "lamp.py"))
lamp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lamp_mod)
COLOR_ORDER = list(lamp_mod.COLORS.keys())

# lamp.py's sweep cache is designed for a CLI run of a few seconds; in a plugin
# that lives for hours, we expire it so we can find again a lamp that changed
# network along the way (observed: lamps flap between box <-> Mango)
_sweep_gate = threading.Lock()
_last_sweep = [0.0]
def allow_resweep():
    with _sweep_gate:
        if time.time() - _last_sweep[0] > 30:
            lamp_mod._swept.clear()
            _last_sweep[0] = time.time()

_cfg_lock = threading.Lock()
def load_config():
    with _cfg_lock:
        return json.load(open(CONFIG))
def save_config(cfg):
    with _cfg_lock:
        json.dump(cfg, open(CONFIG, "w"), indent=2)

SYNTAX_VERSION = "2.0"

def _maybe_enable_driver_debug(cfg):
    """Opt-in deep instrumentation ("debug": true in the config): tinytuya
    protocol dump (packets, retries, 3.5 negotiation) to a dedicated log
    — the microscope when the ack timer isn't enough."""
    if not (cfg or {}).get("debug"):
        return
    import logging
    dbg = os.path.expanduser(
        "~/Library/Logs/ElgatoStreamDeck/com.openlamp.lamps-debug.log")
    logging.basicConfig(filename=dbg, level=logging.DEBUG,
                        format="%(asctime)s %(name)s %(message)s")
    tinytuya.set_debug(True)
    log("driver debug ACTIVE ->", dbg)   # generic syntax = WLED-compatible state patch (cf SYNTAXE-MOTEUR.md)

_rejoin_last = [0.0]
def rejoin_stage_wifi(cfg):
    """Anti-zombie level 1.5 (learned 2026-07-04): when the Mac jumps off the
    stage Wi-Fi (home auto-join while the router is absent), it NEVER comes
    back on its own — ghost lease + unreachable lamps while the router is
    fine. If the router no longer responds AND an SSID is configured
    ("router": {"ssid": "BEN-MUSIC", ...}), we re-join outright. The
    networksetup -3900 error is cosmetic: the association happens anyway.
    Throttle 3 min; macOS switches back on its own if the SSID is off the air."""
    r = (cfg or {}).get("router") or {}
    host, ssid = r.get("host"), r.get("ssid")
    if not host or not ssid or sys.platform != "darwin":
        return False
    if time.time() - _rejoin_last[0] < 180:
        return False
    try:
        if subprocess.run(["ping", "-c", "1", "-t", "2", host],
                          capture_output=True, timeout=5).returncode == 0:
            return False                     # router reachable: nothing to heal
    except Exception:
        pass
    _rejoin_last[0] = time.time()
    try:
        subprocess.run(["networksetup", "-setairportnetwork", "en0", ssid],
                       capture_output=True, timeout=15)
        log("rejoin stage Wi-Fi:", ssid)
        return True
    except Exception as e:
        log("rejoin Wi-Fi failed:", e)
        return False

_deauth_last = {}
def mango_deauth(mac, cfg):
    """Anti-zombie level 1: disassociates the lamp from Wi-Fi via SSH OpenWrt on
    the router (GL.iNet). It re-associates and its IP stack restarts — the
    equivalent of a NETWORK unplug/replug, without touching the lamp. Active if
    the config contains "router": {"host": "192.168.4.1", "ssh_key": "~/.ssh/id_router"}
    (public key to install in the router: LuCI > System > Administration).
    Throttle 3 min per lamp: hammering the deauth would make the case worse."""
    r = (cfg or {}).get("router") or {}
    host = r.get("host")
    key = os.path.expanduser(r.get("ssh_key", ""))
    if not host or not key or not os.path.exists(key):
        return False
    if time.time() - _deauth_last.get(mac, 0) < 180:
        return False
    _deauth_last[mac] = time.time()
    # standard OpenWrt = hostapd via ubus; proprietary MediaTek builds
    # (GL-MT300N-V2…) = iwpriv DisConnectSta on ra0. We try both.
    script = ("for i in $(ubus list 'hostapd.*' 2>/dev/null); do ubus call $i del_client "
              "'{\"addr\":\"%s\",\"reason\":5,\"deauth\":true,\"ban_time\":0}'; done; "
              "iwpriv ra0 set DisConnectSta=%s 2>/dev/null; true" % (mac, mac))
    try:
        p = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes",
                            "-o", "StrictHostKeyChecking=accept-new",
                            "-o", "ConnectTimeout=4", "root@" + host, script],
                           capture_output=True, timeout=12)
        log("deauth", mac, "via", host, "->", "OK" if p.returncode == 0 else
            ("rc=%d %s" % (p.returncode, p.stderr.decode()[:80])))
        return p.returncode == 0
    except Exception as e:
        log("deauth failed:", e)
        return False

class BaseLamp(threading.Thread):
    """Common base: command queue + life loop. Subclasses: _connect/_exec/_heartbeat."""
    def __init__(self, conf, state):
        super().__init__(daemon=True)
        self.c = conf; self.state = state
        self.q = queue.Queue()
        self.ok = False; self.stop = False
        self.bri = 60; self.is_on = True
        self.rgb = (0, 100, 200)        # last RGB sent — used for tt/blackout/snapshot
        self.saved = None               # state saved by blackout (restore replays it)
        self._next_tt = None            # one-shot per-command fade (WLED x100 ms units) set by
                                        # "tt1:N" (a per-button fondu), else None = use config default

    def tracked_state(self):
        # photo of the engine-side tracked state (snapshots, blackout/restore)
        return {"on": self.is_on, "bri": self.bri, "rgb": list(self.rgb)}

    def snapshot_state(self):
        # state captured by a snapshot; drivers enrich it (cf WledLamp, which
        # adds RGBW colour + white temperature + effect + palette).
        return self.tracked_state()

    def snapshot_patch(self, s):
        # recall patch for a snapshot (None = do nothing). Base = on/colour/bri.
        if not s.get("on", True):
            return {"on": False}
        return {"on": True, "col": s.get("rgb"), "bri": round(s.get("bri", 60) * 2.55)}

    def _post_exec_verify(self):
        pass                                   # TuyaLamp override: colour verify

    def _greet(self):
        """Rainbow welcome sweep (Benoit 2026-07-04): a clearly visible ~2.7 s pass
        through the whole hue wheel, fired on connect / power-on before the sync
        state. BEST-EFFORT and cosmetic: if a step glitches (flaky radio) we abandon
        the sweep silently and let sync take over — the greeting must never trigger a
        reconnect loop on a fragile lamp. Returns True if the full sweep completed."""
        import colorsys
        steps = 9
        for i in range(steps + 1):
            r, g, b = colorsys.hsv_to_rgb((i / steps) % 1.0, 1.0, 1.0)
            try:
                self._exec({"on": True, "bri": 255,
                            "col": [round(r * 255), round(g * 255), round(b * 255)]})
            except Exception:
                return False          # glitch -> stop sweeping, no reconnect
            time.sleep(2.7 / steps)
        return True

    @property
    def name(self): return self.c.get("name", "?")

    def run(self):
        backoff = 3
        while not self.stop:
            if not self.ok:
                try:
                    ok = self._connect()
                except Exception as e:
                    log(self.name, "connection failed:", e)
                    ok = False
                if not ok:
                    # progressive backoff 3->30 s: hammering a stuck lamp makes
                    # its state worse (single slot); we give it room to breathe
                    self._fails = getattr(self, "_fails", 0) + 1
                    if self._fails % 2 == 0:
                        # first the Mac's NETWORK (auto-join jump?), then the lamp
                        try:
                            rejoin_stage_wifi(load_config())
                        except Exception:
                            pass
                    if self._fails % 4 == 0 and self.c.get("type", "tuya") == "tuya":
                        # ~4 failures in a row = zombie profile -> deauth via router
                        try:
                            mango_deauth(self.c.get("mac", ""), load_config())
                        except Exception:
                            pass
                    time.sleep(backoff)
                    backoff = min(30, backoff * 2)
                    continue
                self._fails = 0
                # 1) "I'm online" blink, 2) sync state (cf sync_patch)
                eng = getattr(self, "engine", None)
                try:
                    # anti-loop (2026-07-04 17h): on degraded radio, the rainbow
                    # burst re-kills the session on EVERY reconnect -> infinite
                    # greet->death loop. After 3 consecutive greeting/sync failures,
                    # we skip the greet until a connection that holds.
                    greet = eng.cfg.get("greet", True) if eng else True
                    if getattr(self, "_greet_fails", 0) >= 3:
                        greet = False
                    if getattr(self, "eph", False) and getattr(self, "_greeted", False):
                        greet = False          # ephemeral: a single greeting per startup
                    self._greeted = True
                    st = eng.sync_patch(self) if eng else None
                    prev = self.tracked_state()
                    if greet:
                        done = self._greet()
                        log(self.name, "rainbow welcome sweep" if done
                            else "rainbow welcome sweep (partial — flaky radio)")
                    if st:
                        self._exec(st)
                        log(self.name, "synced on connect")
                    elif greet and prev.get("on", True):
                        # no sync: give the lamp back its pre-greeting state
                        self._exec({"on": True, "col": prev["rgb"],
                                    "bri": round(prev["bri"] * 2.55)})
                except Exception as e:
                    # greeting/sync half-applied = mongrel state (e.g. stays full
                    # white) -> force a clean reconnect rather than leave it
                    self._greet_fails = getattr(self, "_greet_fails", 0) + 1
                    self._last_err_ts = time.time()
                    log(self.name, "connect greeting/sync failed:", e,
                        "-> reconnect (greet failure #%d)" % self._greet_fails)
                    self.ok = False
                    continue
                else:
                    self._greet_fails = 0
            backoff = 3
            try:
                cmd = self.q.get(timeout=9)
            except queue.Empty:
                try:
                    self._heartbeat()
                except Exception:
                    log(self.name, "heartbeat lost -> reconnect")
                    self.ok = False
                continue
            if cmd is None:
                break
            # synchronous state request (integration test, panel) — goes through the
            # queue so it runs AFTER the commands already pending (order guaranteed)
            if isinstance(cmd, tuple) and cmd and cmd[0] == "__status__":
                _, holder, ev = cmd
                try:
                    holder[self.name] = self._status()
                except Exception as e:
                    holder[self.name] = {"error": str(e)}
                    self.ok = False
                ev.set()
                continue
            if isinstance(cmd, tuple) and cmd and cmd[0] == "__snap__":
                _, holder, ev = cmd                        # photo AFTER the queue (FIFO)
                holder[self.name] = self.snapshot_state()  # driver-specific (WLED = full state)
                ev.set()
                continue
            # per-button fondu: "tt1:N" (ms) arms a ONE-SHOT transition for the NEXT
            # command only, without touching the lamp's persistent "transition" config.
            # dispatch() queues it right before the button's real command; the driver's
            # payload builder (_payload for WLED, _fade_to for Tuya) consumes it once.
            if isinstance(cmd, str) and cmd.startswith("tt1:"):
                try:
                    self._next_tt = max(0, round(int(cmd.split(":", 1)[1] or 0) / 100))
                except (ValueError, TypeError):
                    self._next_tt = None
                continue
            try:
                self._exec(cmd)
                self._post_exec_verify()
            except Exception as e:
                self._last_err_ts = time.time()
                log(self.name, "error:", e, "-> reconnect + retry")
                self.ok = False
                try:
                    if self._connect():
                        self._exec(cmd)
                except Exception as e2:
                    log(self.name, "retry failed:", e2); self.ok = False

# =====================================================================================
# TUYA PROTOCOL AUTOPSY — why this class is so defensive (2026-07-04)
# =====================================================================================
# A whole day of debugging (bare protocol bench + router tcpdump capture + reading the
# tinytuya issues) laid bare WHY controlling Tuya bulbs locally is a nightmare. Each
# workaround below answers one of these pathologies. Documented here so the pain serves
# a purpose, and to justify the choice to migrate to WLED (cf WledLamp, the exact
# opposite: plaintext HTTP, stateless, sessionless).
#
# 1. SINGLE CONNECTION SLOT. A Tuya lamp accepts only ONE TCP connection at a time
#    (port 6668). The Smart Life app on the same LOCAL network grabs it and starves
#    us -> "always close the app first". Hence set_socketPersistent + the lamp at the
#    top of the config, and the fragility at the slightest competitor.
#
# 2. ENCRYPTED 3.5 SESSION THAT RENEGOTIATES. The 3.5 protocol opens a session with a
#    key exchange (GCM). Under load, the lamp renegotiates in a loop (measured: ~20
#    renegos in 10 min in the debug log). Each renego costs 2-10 s -> the "horrible
#    latency". The ephemeral mode (socket per command) is WORSE: it pays the renego
#    on EVERY command (A/B measured 2026-07-04: ephemeral clear loser).
#
# 3. ACK WITHOUT EXECUTION (false acknowledgements). A half-dead session ACKNOWLEDGES
#    a send without EXECUTING it: the response buffer desyncs, the ack read belongs to
#    the previous command. Consequence: the engine believes "orange applied" while the
#    lamp stays green. The ONLY remedy: re-read the real colour (verify-after-write,
#    cf _verify_colour) — the ack is never enough.
#
# 4. EMPTY RESPONSE = ZOMBIE SESSION. The TCP socket is alive, but the lamp drops our
#    packets without responding (payload b''). Indistinguishable from a success at the
#    TCP level. So we treat "empty response" as a failure -> reconnect (cf _dps).
#
# 5. DRIFTING _set_values_check CACHE. tinytuya caches the last DP state; in a burst it
#    doesn't re-emit a DP it considers "unchanged" -> after a mode:scene, the return to
#    mode:colour was no longer sent and the lamp ignored the colours. Hence the
#    EXPLICIT multi-DP send every time (cf _dps), bypassing the cache.
#
# 6. THROUGHPUT CEILING ~4 cmd/s. Beyond that, the firmware drops the session mid-fade.
#    Hence the pacing of fades (max 12 steps, >=250 ms) and the rate-capped verify.
#
# 7. FIRMWARE MEMORY CORRUPTION after ~2 h of uptime. Documented by the tinytuya
#    maintainers (discussion #443): the lamp "forgets" its local key, code 914, until a
#    power-cycle. Network multicast/broadcast speeds up the corruption. NO software
#    fix — hence the level 1 deauth, and ultimately the WLED hardware choice.
#
# 8. LOCAL KEY VIA THE CLOUD. To talk LOCALLY to the lamp, you first have to extract its
#    "local_key" from the Tuya cloud (account + API). Local that depends on the cloud:
#    the opposite of an open protocol.
#
# VERDICT: Tuya optimises for locking the user into its app/cloud, not for local
# control. WLED does the exact opposite (cf WledLamp). These bulbs stay supported
# (hardware already bought) but are NOT recommended for the stage.
# =====================================================================================
class TuyaLamp(BaseLamp):
    """Persistent tinytuya socket + multi-network (one IP per subnet, cf lamp.py).
    Loaded with workarounds — see the AUTOPSY above for the why of each one."""
    def _probe(self, ip):
        try:
            s = socket.create_connection((ip, 6668), 1.5); s.close(); return True
        except Exception:
            return False

    def _resolve_ip(self):
        ips = self.c.setdefault("ips", {})
        last = self.c.get("last")
        subs = ([last] if last in ips else []) + \
               [s for s in lamp_mod.local_subnets() if s in ips and s != last]
        for sn in subs:
            if self._probe(ips[sn]):
                self.c["last"] = sn
                return ips[sn]
        allow_resweep()
        ip = lamp_mod.find_ip_any_subnet(self.c["mac"])
        if ip:
            sn = ".".join(ip.split(".")[:3])
            ips[sn] = ip; self.c["last"] = sn
        return ip

    def _connect(self):
        self.ok = False; self.dev = None
        ip = self._resolve_ip()
        if not ip:
            return False
        d = tinytuya.BulbDevice(self.c["device_id"], ip, self.c["local_key"], version=3.5)
        # EPHEMERAL MODE by default (2026-07-04 19h45, Benoit's decision "too slow
        # too unstable"): the persistent socket monopolised the lamp's single
        # connection slot and manufactured the day's pathologies (zombie sessions,
        # false acks, desynced buffer, 20 3.5 renegotiations in 10 min). In
        # ephemeral: a fresh connection PER command (~150-300 ms), nothing rots
        # between two presses, the slot stays free. The old behaviour:
        # "connection": "persistent" in the config.
        self.eph = (self.c.get("connection")
                    or (load_config().get("connection") or "ephemeral")) == "ephemeral"
        d.set_socketPersistent(not self.eph)
        d.set_socketTimeout(3)
        d.set_socketRetryLimit(1)
        st = d.status()
        dps = st.get("dps", {}) if isinstance(st, dict) else {}
        if not dps:
            return False
        self.bri = lamp_mod.cur_bri_pct(dps)
        self.is_on = bool(dps.get("20", True))
        # also retrieve the real COLOUR (DP24 = HHHH SSSS VVVV): without it, after a
        # reconnect the rgb tracking fell back to the default -> wrong snapshots
        cd = dps.get("24", "")
        if isinstance(cd, str) and len(cd) >= 12:
            try:
                import colorsys
                h = int(cd[0:4], 16) / 360.0
                s = int(cd[4:8], 16) / 1000.0
                r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(1, s), 1.0)
                self.rgb = (round(r * 255), round(g * 255), round(b * 255))
            except Exception:
                pass
        self.dev = d; self.ok = True
        log(self.name, "connected @", ip, "bri:", self.bri, "on:", self.is_on)
        return True

    def _heartbeat(self):
        if getattr(self, "eph", False):
            # no session to maintain; light TCP probe (1 call in 4, i.e. ~36 s)
            # so the Status key detects a lamp that's off
            self._hb_tick = getattr(self, "_hb_tick", 0) + 1
            if self._hb_tick % 4 == 0:
                ip = (self.c.get("ips") or {}).get(self.c.get("last", ""), "")
                if ip and not self._probe(ip):
                    raise RuntimeError("TCP probe: lamp off or off Wi-Fi")
            return
        self.dev.heartbeat(nowait=False)

    def _post_exec_verify(self):
        # v3 (2026-07-04 18h35, blind spot found by Benoit): the "recent error"
        # gating was blind to FALSE ACKS — a lamp that acknowledges without
        # executing looks perfectly healthy, so it was never verified.
        # New contract: RATE-CAPPED verification — at most one re-read per lamp
        # every 8 s, at the end of the queue. Cost ~8x under the firmware ceiling,
        # and no final state diverges anymore without being seen.
        now = time.time()
        recent_err = now - getattr(self, "_last_err_ts", 0) < 300
        if not recent_err and now - getattr(self, "_last_verify_ts", 0) < 8:
            return
        self._last_verify_ts = now
        if self.q.empty():                     # mashing: we only verify the end
            self._verify_colour()

    def _dps(self, **caps):
        # EXPLICIT multi-DP send in a single packet, WITHOUT going through the
        # tinytuya cache (_set_values_check): with blind sends the cache drifts,
        # and the scene->colour return was no longer sent -> the lamp stayed in
        # scene mode and ignored the colours (keys "with no effect", 2026-07-03).
        # WITH acknowledgement (nowait=False): on the persistent socket it only
        # costs one round-trip (~50-100 ms) and it detects silent losses — in a
        # burst, nowait lost commands without any error (2026-07-03).
        d = self.dev
        out = {}
        for k, v in caps.items():
            dp = d.dpset.get(k)
            if dp:
                out[dp] = v
        if out:
            t0 = time.monotonic()
            r = d.set_multiple_values(out, nowait=False)
            ms = (time.monotonic() - t0) * 1000
            # timing ALWAYS logged (Benoit instrumentation 2026-07-04): makes
            # visible the L1/L2 radio asymmetry and the real cost of each ack
            log(self.name, "ack", "+".join(sorted(caps.keys())), "in %.0f ms" % ms)
            if isinstance(r, dict) and r.get("Error"):
                raise RuntimeError("set_multiple_values: %s" % r["Error"])
            if not r:
                # EMPTY response = undead 3.5 session: the TCP socket is alive but
                # the lamp drops our packets without responding (observed on L2 on
                # 2026-07-03: zero error, zero effect). Empty = failure -> reconnect.
                raise RuntimeError("empty response (dead session?)")

    def _colour(self, r, g, b):
        d = self.dev
        self._dps(switch=True, mode="colour",
                  colour=d.rgb_to_hexvalue(r, g, b, d.dpset["value_hexformat"]))
        self.is_on = True
        # DEFERRED verification (v2 of the "ack without execution" fix, 2026-07-04):
        # the inline re-read cost up to 6 s per press (draining at full timeout)
        # — unacceptable live. We note the target; the life loop verifies as soon
        # as the queue is EMPTY, at short timeout (cf _verify_colour). Key mashing
        # => only the LAST colour is verified.
        self._want_rgb = (r, g, b)
        self._verify_tried = False

    def _verify_colour(self):
        """Verify (fast) that the last wanted colour is physically on the lamp.
        A half-dead session can ACKNOWLEDGE without EXECUTING (desynced response
        buffer, observed 2026-07-04): we re-read the hue at short timeout, we
        resend ONCE, otherwise we condemn the session."""
        want = getattr(self, "_want_rgb", None)
        if not want:
            return
        import colorsys
        r, g, b = want
        want_h = round(colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[0] * 360)
        d = self.dev
        d.set_socketTimeout(0.7)               # lightning re-read, not 3 s
        try:
            dps = {}
            empty = 0
            for _ in range(5):
                st = d.status()
                cur = st.get("dps", {}) if isinstance(st, dict) else {}
                if not cur:
                    empty += 1
                    if dps or empty >= 1:
                        break
                    continue
                dps.update(cur)
        except Exception:
            return                              # re-read impossible: no opinion
        finally:
            d.set_socketTimeout(3)
        cd = dps.get(str(d.dpset.get("colour") or 24), "")
        if not (isinstance(cd, str) and len(cd) >= 4):
            return
        got_h = int(cd[0:4], 16)
        diff = abs(got_h - want_h)
        if min(diff, 360 - diff) <= 10:
            self._want_rgb = None               # conform: nothing to do
            return
        if not getattr(self, "_verify_tried", False):
            self._verify_tried = True
            log(self.name, "colour not applied (read %d, wanted %d) -> resend" %
                (got_h, want_h))
            self._dps(switch=True, mode="colour",
                      colour=d.rgb_to_hexvalue(r, g, b, d.dpset["value_hexformat"]))
        else:
            self._want_rgb = None
            raise RuntimeError("colour not applied after resend (rotten session)")

    def _fade_to(self, rgb_target, pct_target, dur_ms):
        # tt EMULATED (native in WLED): step-by-step interpolation on the
        # persistent socket. MAX ~4 steps/s: at ~10 acked steps/s the lamps'
        # firmware chokes and drops the session mid-fade (observed 2026-07-03).
        steps = max(2, min(12, int(dur_ms / 250)))
        r0, g0, b0 = self.rgb
        p0 = self.bri
        for i in range(1, steps + 1):
            f = i / steps
            # tracking updated on EVERY step: a crash mid-fade leaves an exact
            # state (before: bri/rgb frozen -> wrong snapshots after an incident)
            self.rgb = (round(r0 + (rgb_target[0] - r0) * f),
                        round(g0 + (rgb_target[1] - g0) * f),
                        round(b0 + (rgb_target[2] - b0) * f))
            self.bri = round(p0 + (pct_target - p0) * f)
            r, g, b = lamp_mod.scale(self.rgb, self.bri)
            self._dps(switch=True, mode="colour",
                      colour=self.dev.rgb_to_hexvalue(
                          r, g, b, self.dev.dpset["value_hexformat"]))
            if i < steps:
                time.sleep(max(0.25, dur_ms / 1000 / steps))
        self.rgb = tuple(rgb_target); self.bri = pct_target; self.is_on = True

    def _apply(self, st):
        # WLED-compatible state patch (SYNTAXE-MOTEUR.md) translated into Tuya DPs.
        # Omitted fields = unchanged, as in WLED.
        if "nl" in st:                                     # nightlight = countdown DP26
            nl = st["nl"] or {}
            self._dps(timer=(int(nl.get("dur", 0)) * 60) if nl.get("on") else 0)
        if st.get("music"):                                # extension
            self._dps(switch=True, mode="music"); self.is_on = True; return
        if "scene" in st:                                  # extension: named scene
            data = (self.c.get("scenes") or {}).get(st["scene"])
            if data:
                self._dps(switch=True, mode="scene", scene=data); self.is_on = True
            return
        if "ps" in st:                                     # preset -> memorised scene
            self._dps(switch=True, mode="scene"); self.is_on = True; return
        if "cct" in st:                                    # white warm<->cool (0-255)
            bri = st.get("bri")
            pct = round(bri / 2.55) if bri is not None else self.bri
            self.bri = max(1, min(100, pct))
            self._dps(switch=True, mode="white", brightness=max(10, self.bri * 10),
                      colourtemp=max(0, min(1000, round(st["cct"] / 255 * 1000))))
            self.is_on = True; return
        rgb = tuple(st["col"][:3]) if st.get("col") else None
        bri = st.get("bri")
        pct = max(1, min(100, round(bri / 2.55))) if bri is not None else None
        if rgb or pct is not None:
            tt = st.get("tt")
            tgt_rgb = rgb or self.rgb
            tgt_pct = pct if pct is not None else self.bri
            if tt:
                self._fade_to(tgt_rgb, tgt_pct, int(tt) * 100)   # tt in x100 ms (WLED)
            else:
                self.bri = tgt_pct
                r, g, b = lamp_mod.scale(tgt_rgb, tgt_pct)
                self._colour(r, g, b)
                self.rgb = tuple(tgt_rgb)
        if "on" in st:
            v = st["on"]
            if v == "t":
                v = not self.is_on
            self._dps(switch=bool(v)); self.is_on = bool(v)

    def _status(self):
        # persistent socket + nowait sends = the acks pile up in the buffer.
        # We drain EVERYTHING (old packets contain STALE states) and keep the
        # merge — the last packet read reflects the current state.
        dps = {}
        empty = 0
        for _ in range(8):
            st = self.dev.status()
            cur = st.get("dps", {}) if isinstance(st, dict) else {}
            if not cur:
                empty += 1
                if dps or empty >= 2:                      # 2 empty reads = stop
                    break
                continue                                   # 1st empty: the lamp is slow
            dps.update(cur)
        return dps

    def _exec(self, cmd):
        d, COLORS, PALIERS = self.dev, lamp_mod.COLORS, lamp_mod.PALIERS
        if isinstance(cmd, dict):                          # v2 syntax: WLED state patch
            self._apply(cmd); return
        if cmd == "blackout":                              # black + remembers the prior state
            self.saved = self.tracked_state()
            self._dps(switch=False); self.is_on = False; return
        if cmd == "restore":                               # replays the pre-blackout state
            s = self.saved or {}
            if s.get("on", True):
                self.bri = s.get("bri", self.bri)
                self.rgb = tuple(s.get("rgb", self.rgb))
                r, g, b = lamp_mod.scale(self.rgb, self.bri)
                self._colour(r, g, b)
            return
        if cmd == "off":
            self._dps(switch=False); self.is_on = False; return
        if cmd == "on":
            self._dps(switch=True); self.is_on = True; return
        if cmd == "toggle":                                # toggle (idea borrowed from the WLED plugin)
            self._dps(switch=not self.is_on)
            self.is_on = not self.is_on; return
        if cmd.startswith("wled:"):                        # WLED-specific: skips its turn
            return
        if cmd.startswith("set:"):                         # ADVANCED mode: colour + brightness
            _, cname, pct = cmd.split(":")
            pct = max(1, min(100, int(pct)))
            self.bri = pct
            self.rgb = COLORS.get(cname, COLORS["bleu"])
            r, g, b = lamp_mod.scale(self.rgb, pct)
            self._colour(r, g, b); return
        if cmd.startswith("white:"):                       # white: brightness + temperature
            _, bri, temp = cmd.split(":")
            self.bri = max(1, min(100, int(bri)))
            self._dps(switch=True, mode="white",
                      brightness=max(10, self.bri * 10),
                      colourtemp=max(0, min(1000, int(temp) * 10)))
            self.is_on = True; return
        if cmd.startswith("scene:"):                       # NAMED scene captured (config)
            name = cmd.split(":", 1)[1]
            data = (self.c.get("scenes") or {}).get(name)
            if data:
                self._dps(switch=True, mode="scene", scene=data)
                self.is_on = True
            else:
                log(self.name, "scene unknown:", name, "- capture it first")
            return
        if cmd.startswith("countdown:"):                   # power-off timer (minutes)
            self._dps(timer=max(0, int(cmd.split(":")[1])) * 60)
            return
        if cmd.startswith("preset:"):                      # common: Tuya replays ITS scene
            self._dps(switch=True, mode="scene")           # (the N is only for WLED)
            self.is_on = True; return
        if cmd.startswith("mode:"):                        # raw modes of the Tuya app (DP21)
            self._dps(switch=True, mode=cmd.split(":")[1])
            self.is_on = True; return
        if cmd in COLORS:                                  # colour: keeps the brightness
            self.rgb = COLORS[cmd]
            tt = self._next_tt                             # per-button fondu -> smooth Tuya fade, once
            if tt:
                self._next_tt = None
                self._fade_to(COLORS[cmd], self.bri, tt * 100); return
            r, g, b = lamp_mod.scale(COLORS[cmd], self.bri)
            self._colour(r, g, b); return
        pct = PALIERS.get(cmd)
        if pct is None and cmd.startswith("bri:"):
            pct = max(1, min(100, int(cmd.split(":")[1])))
        if pct is not None:                                # brightness: keeps the colour
            self.bri = pct
            color = self.state.get("color", "bleu")
            self.rgb = COLORS.get(color, COLORS["bleu"])
            tt = self._next_tt
            if tt:
                self._next_tt = None
                self._fade_to(self.rgb, pct, tt * 100); return
            r, g, b = lamp_mod.scale(self.rgb, pct)
            self._colour(r, g, b); return
        log(self.name, "command unknown:", cmd)

class WledLamp(BaseLamp):
    """Lamps/strips on WLED firmware (ESP8266/ESP32), local JSON HTTP API.

    The EXACT OPPOSITE of TuyaLamp (cf AUTOPSY above this class). Why WLED is simple
    and reliable where Tuya is a nightmare:
    - No session: each command is a standalone, stateless HTTP POST. Nothing to
      "maintain", so nothing can "rot" (vs Tuya pathologies #2, #4, #7).
    - No single slot: WLED accepts several simultaneous connections (vs #1).
    - No encryption/negotiation: plaintext request, immediate response (vs #2).
    - No false ack: the POST /json/state replies the real state with {"v":true} — the
      lamp doesn't lie, so no acrobatic verify-after-write needed (vs #3).
    - No cloud key: nothing to extract, the local IP is enough (vs #8).
    - Trivial debug: you type http://<host>/ in a browser and see everything (vs the
      tcpdump capture at 10pm).
    A WLED command fits on one line: POST {"seg":[{"col":[[255,0,0]]}]}.

    Config: {"name": "L1", "type": "wled", "host": "192.168.8.50"} — host = IP OR mDNS
    hostname (e.g. "wled-abc123.local"). Options: "segment": N (strip zone),
    "transition": N (fade x100 ms per command)."""

    def _url(self, path):
        return "http://%s%s" % (self.c["host"], path)

    def _post(self, payload, verify=False):
        # standalone POST, sessionless. verify=True -> WLED returns the real state
        # ({"v":true}) which we re-read to refresh the tracking. An HTTP/network
        # failure raises -> the life loop reconnects (but "reconnect" = just re-ping,
        # there's no session to reopen: cf _connect).
        body = dict(payload)
        if verify:
            body["v"] = True
        req = urllib.request.Request(
            self._url("/json/state"), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        last = None
        for attempt in (1, 2):                 # 1 retry: covers an isolated Wi-Fi timeout
            try:
                raw = urllib.request.urlopen(req, timeout=3).read()
                return json.loads(raw) if verify and raw else None
            except Exception as e:
                last = e
                time.sleep(0.15)
        raise RuntimeError("WLED POST failed: %s" % last)

    def _connect(self):
        # WLED "connection" = simple HTTP sanity-check (no session to open).
        try:
            info = json.loads(urllib.request.urlopen(
                self._url("/json/info"), timeout=3).read())
            self.ok = True
            log(self.name, "(WLED) OK @", self.c["host"],
                "ver", info.get("ver", "?"), "leds", (info.get("leds") or {}).get("count", "?"))
            # retrieve the real state on connect (for the Status keys)
            try:
                self._read_into_tracked(self._status())
            except Exception:
                pass
            return True
        except Exception:
            self.ok = False
            return False

    def _heartbeat(self):
        urllib.request.urlopen(self._url("/json/info"), timeout=3).read()

    def _status(self):
        return json.loads(urllib.request.urlopen(
            self._url("/json/state"), timeout=3).read())

    def _read_into_tracked(self, st):
        # WLED always tells the truth: we align the engine tracking on its real state.
        if not isinstance(st, dict):
            return
        if "on" in st:
            self.is_on = bool(st["on"])
        if "bri" in st:
            self.bri = max(1, min(100, round(st["bri"] / 2.55)))
        segs = st.get("seg") or []
        if segs and segs[0].get("col"):
            self.rgb = tuple(segs[0]["col"][0][:3])

    def snapshot_state(self):
        # WLED tells the truth: we photograph the FULL PHYSICAL state (RGBW colour
        # with the white channel, white temperature, effect, palette) so the recall
        # reproduces the ambiance exactly — not just on/bri/rgb (cf fixed limitation).
        try:
            st = self._status()
        except Exception:
            return self.tracked_state()          # network blip: fall back on engine tracking
        seg = (st.get("seg") or [{}])[0]
        col = (seg.get("col") or [[0, 0, 0, 0]])[0]
        return {"on": bool(st.get("on", True)),
                "bri": max(1, min(100, round(st.get("bri", 128) / 2.55))),
                "rgb": list(col[:3]),            # compat old readers (Tuya, UI)
                "col": list(col[:4]),            # full RGBW (dedicated white included)
                "cct": seg.get("cct", 0),
                "fx": seg.get("fx", 0),
                "pal": seg.get("pal", 0)}

    def snapshot_patch(self, s):
        # FLAT patch (v2 syntax): _apply itself places col/cct/fx/pal into seg.
        if not s.get("on", True):
            return {"on": False}
        return {"on": True, "bri": round(s.get("bri", 60) * 2.55),
                "col": list(s.get("col") or s.get("rgb") or [0, 0, 0]),
                "cct": s.get("cct", 0), "fx": s.get("fx", 0), "pal": s.get("pal", 0)}

    def _seg(self, seg):
        # config "segment": N -> this plugin "lamp" only drives one zone of the strip
        if "segment" in self.c:
            seg["id"] = self.c["segment"]
        return seg

    def _payload(self, p):
        # tt precedence: explicit patch tt > per-button fondu (one-shot) > config default
        if self._next_tt is not None:
            p.setdefault("tt", self._next_tt); self._next_tt = None
        if "transition" in self.c:
            # config "transition": N -> fade (x100 ms) applied to each command
            p.setdefault("tt", self.c["transition"])
        return p

    def _apply(self, st):
        # v2 syntax = the WLED API: near-passthrough
        p = {}; seg = {}
        for k in ("on", "bri", "tt", "ps", "nl"):
            if k in st: p[k] = st[k]
        for k in ("fx", "sx", "ix", "pal", "cct"):
            if k in st: seg[k] = st[k]
        if st.get("col"):
            seg["col"] = [list(st["col"][:4])]     # RGBW: keeps the dedicated white W channel if provided
        if seg:
            p["seg"] = [self._seg(seg)]
        if not p:
            return
        self._post(self._payload(p))
        if "on" in st and st["on"] != "t":
            self.is_on = bool(st["on"])
        elif seg or "bri" in st:
            self.is_on = True
        if st.get("col"):
            self.rgb = tuple(st["col"][:3])
        if "bri" in st:
            self.bri = max(1, min(100, round(st["bri"] / 2.55)))

    def _exec(self, cmd):
        COLORS, PALIERS = lamp_mod.COLORS, lamp_mod.PALIERS
        if isinstance(cmd, dict):                          # v2 syntax: WLED state patch
            self._apply(cmd); return
        if cmd == "blackout":
            self.saved = self.tracked_state()
            self._post({"on": False}); self.is_on = False; return
        if cmd == "restore":
            s = self.saved or {}
            if s.get("on", True):
                self.bri = s.get("bri", self.bri)
                self.rgb = tuple(s.get("rgb", self.rgb))
                self._post(self._payload({"on": True, "bri": round(self.bri * 2.55),
                            "seg": [self._seg({"col": [list(self.rgb)]})]}))
                self.is_on = True
            return
        if cmd == "off":
            self._post(self._payload({"on": False})); self.is_on = False; return
        if cmd == "on":
            self._post(self._payload({"on": True})); self.is_on = True; return
        if cmd == "toggle":
            self._post(self._payload({"on": "t"}))        # "t" = native WLED toggle
            self.is_on = not self.is_on; return
        if cmd.startswith("set:"):                         # advanced: colour + brightness
            _, cname, pct = cmd.split(":")
            r, g, b = COLORS.get(cname, COLORS["bleu"])
            self.bri = max(1, min(100, int(pct)))
            self._post(self._payload({"on": True, "bri": round(self.bri * 2.55),
                        "seg": [self._seg({"col": [[r, g, b]]})]}))
            self.is_on = True; return
        if cmd.startswith("preset:"):                      # common: WLED has 250 presets
            self._post(self._payload({"ps": int(cmd.split(":")[1])}))
            self.is_on = True; return
        if cmd.startswith("wled:fx:"):                     # animated effect: fx[:sx[:ix[:pal]]]
            parts = cmd.split(":")[2:]
            fx = parts[0] if parts[0] in ("~", "~-", "r") else int(parts[0])
            seg = self._seg({"fx": fx})
            for k, v in zip(("sx", "ix", "pal"), parts[1:]):
                seg[k] = int(v)
            self._post(self._payload({"on": True, "seg": [seg]}))
            self.is_on = True; return
        if cmd.startswith("wled:psave:"):                  # save the state as preset N
            self._post({"psave": int(cmd.split(":")[2])}); return
        if cmd.startswith("mode:"):
            log(self.name, "(WLED) Tuya modes not applicable — use WLED presets")
            return
        if cmd in COLORS:
            r, g, b = COLORS[cmd]
            self._post(self._payload({"on": True, "seg": [self._seg({"col": [[r, g, b]]})]}))
            self.is_on = True; return
        pct = PALIERS.get(cmd)
        if pct is None and cmd.startswith("bri:"):
            pct = max(1, min(100, int(cmd.split(":")[1])))
        if pct is not None:
            self.bri = pct
            self._post(self._payload({"on": True, "bri": round(pct * 2.55)}))
            self.is_on = True; return

def make_lamp(conf, state):
    return WledLamp(conf, state) if conf.get("type") == "wled" else TuyaLamp(conf, state)

def discover_wled(subnets=None, timeout=0.4, workers=48):
    """'Broadcast' discovery of WLED lamps: HTTP scan of the local subnets
    (GET /json/info) -> zero manual config. stdlib only, cross-platform. We scan
    ONLY the subnets where the Mac is present (lamp_mod.local_subnets)."""
    import concurrent.futures
    subs = subnets if subnets is not None else lamp_mod.local_subnets()
    found = {}
    def probe(ip):
        try:
            info = json.loads(urllib.request.urlopen("http://%s/json/info" % ip, timeout=timeout).read())
        except Exception:
            return
        # WLED signature: 'ver' field + (leds or brand). Avoids false positives.
        if info.get("ver") and (info.get("leds") or info.get("brand")):
            found[ip] = {"name": info.get("name") or "WLED", "host": ip,
                         "type": "wled", "mac": info.get("mac", "")}
    ips = ["%s.%d" % (s, i) for s in subs for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(probe, ips))
    return list(found.values())

class LocalApi(threading.Thread):
    """LOCAL entry point of the plugin (architecture principle, 2026-07-03).

    Why: a Tuya lamp accepts only ONE local connection — held by this process as
    long as Stream Deck runs. Any other frontend (CLI lamp.py, Bome "Execute
    file", macOS shortcuts, scripts…) must therefore go THROUGH the plugin. This
    mini HTTP API (127.0.0.1 only, never exposed to the network) is that entry
    point: the same command vocabulary as the keys, the same latency (persistent
    connections). Stream Deck is just ONE frontend among others — the engine stays
    embeddable elsewhere without questioning anything.

      GET /cmd?c=<command>[&lamps=L1,L2]  -> {"ok": bool, "targets": [...]}
      GET /status                          -> {"L1": {"connected","on","bri"}, ...}
    """
    def __init__(self, engine):
        super().__init__(daemon=True)
        self.engine = engine

    def run(self):
        plugin = self.engine
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _reply(self, body, code=200):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                # WLED-compatible endpoint (OpenLamp State): a JSON state patch
                # applies to the targeted lamps (?lamps=..., empty = all). Tools
                # that know how to talk to a WLED can drive the Tuya ones.
                u = urllib.parse.urlparse(self.path)
                if u.path != "/json/state":
                    self.send_response(404); self.end_headers(); return
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    st = json.loads(self.rfile.read(n)) if n else {}
                    assert isinstance(st, dict)
                except Exception:
                    self._reply({"error": 9}, 400); return
                q = urllib.parse.parse_qs(u.query)
                lamps = [s for s in q.get("lamps", [""])[0].split(",") if s]
                ok = plugin.dispatch(st, {"lamps": lamps})
                self._reply({"success": bool(ok)})       # same response as WLED

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/json/state":
                    # WLED-style aggregated state + per-lamp detail as an extension
                    first = plugin.lamps[0] if plugin.lamps else None
                    body = {"on": any(l.is_on for l in plugin.lamps),
                            "bri": round((first.bri if first else 60) * 2.55),
                            "lamps": {l.name: l.tracked_state() for l in plugin.lamps}}
                    self._reply(body); return
                if u.path == "/cmd" and q.get("c"):
                    cmd = q["c"][0]
                    lamps = [s for s in q.get("lamps", [""])[0].split(",") if s]
                    settings = {"lamps": lamps}
                    ok = plugin.dispatch(cmd, settings)
                    log("api /cmd:", cmd, "->",
                        ",".join(l.name for l in plugin.targets(settings)) or "-")
                    body = {"ok": ok, "cmd": cmd,
                            "targets": [l.name for l in plugin.targets(settings)]}
                elif u.path == "/syntax":
                    # public contract of the v2 syntax (cf SYNTAXE-MOTEUR.md)
                    body = {"version": SYNTAX_VERSION,
                            "base": "WLED /json/state (patch : champs omis = inchanges)",
                            "keys": ["on", "bri", "col", "cct", "ps", "tt", "nl",
                                     "fx", "sx", "ix", "pal"],
                            "extensions": ["scene", "music"],
                            "commands": ["blackout", "restore", "snap:save:<nom>",
                                          "snap:<nom>", "beat:toggle", "beat:off",
                                          "beat:<link|midi>[:action[:colors[:sub]]]"],
                            "aliases": ["<couleur>", "<palier>", "bri:N", "set:c:p",
                                         "white:b:t", "scene:nom", "preset:N",
                                         "mode:music", "countdown:min", "wled:fx:...",
                                         "on", "off", "toggle"]}
                elif u.path == "/status":
                    body = {l.name: {"connected": l.ok, "on": l.is_on, "bri": l.bri,
                                     "type": l.c.get("type", "tuya")}
                            for l in plugin.lamps}
                    if q.get("full"):
                        # PHYSICAL re-read via the persistent connections (FIFO, so
                        # after the pending commands) — debug + tests
                        holder = {}; evs = []
                        for l in plugin.lamps:
                            ev = threading.Event(); evs.append(ev)
                            l.q.put(("__status__", holder, ev))
                        for ev in evs:
                            ev.wait(8)
                        for n, dps in holder.items():
                            body.setdefault(n, {})["dps"] = dps
                elif u.path == "/discover":
                    added = plugin.discover_now()
                    body = {"added": added, "lamps": [l.name for l in plugin.lamps]}
                else:
                    self.send_response(404); self.end_headers(); return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
            log("local API ready on 127.0.0.1:%d" % API_PORT)
            srv.serve_forever()
        except OSError as e:
            log("local API unavailable (port %d):" % API_PORT, e)


class Engine:
    """OpenLamp engine host: owns the lamps (persistent connections), the dispatcher,
    groups, snapshots, animations, sync and the local API. Frontend-agnostic — the
    only upward link is the on_change hook (see module docstring)."""
    def __init__(self):
        self.cfg = load_config()
        _maybe_enable_driver_debug(self.cfg)
        self.state = self.cfg.setdefault("state", {"color": "bleu"})
        self.lamps = []
        self.anims = {}          # lamp -> stop Event for the current animation
        self._beat_proc = None   # beatsync subprocess handle (see _start_beat)
        self.on_change = None    # frontend hook, called ~1.5 s after a dispatch
        self._dirty = False
        self._lamps_lock = threading.Lock()   # discovery mutates self.lamps off-thread
        self._start_lamps()
        LocalApi(self).start()   # entry point CLI / MIDI / Bome / other frontends
        threading.Thread(target=self._saver, daemon=True).start()
        threading.Thread(target=self._wifi_keepalive, daemon=True).start()
        # network (broadcast) discovery enabled by default -> no mandatory manual
        # config: the network's WLED lamps are added automatically at startup.
        self.discover = self.cfg.get("discover", True)
        threading.Thread(target=self._discover_startup, daemon=True).start()

    def _discover_startup(self):
        time.sleep(2)            # let the global setting (SD) arrive and be able to disable
        if self.discover:
            self.discover_now(quiet=True)

    def discover_now(self, quiet=False):
        """Scans the network and ADDS the WLED lamps missing from the list (dedup by host/mac).
        Lamp name = the device's WLED name (e.g. 'OpenLamp L1')."""
        try:
            found = discover_wled()
        except Exception as e:
            log("discovery: error", e); return 0
        added = 0
        # normalise MACs: config stores "10:00:3B:..", WLED /json/info returns "10003b.." —
        # comparing raw strings never matched, so discovery kept re-adding the SAME lamp as a
        # duplicate "L1 (86)" (Benoit 2026-07-15). Strip separators on both sides.
        nmac = lambda m: (m or "").lower().replace(":", "").replace("-", "")
        with self._lamps_lock:
            have = {(l.c.get("host") or "").lower() for l in self.lamps}
            by_mac = {nmac(l.c.get("mac")): l for l in self.lamps if l.c.get("mac")}
            names = {l.name for l in self.lamps}
            for d in found:
                if d["host"].lower() in have:
                    continue
                m = nmac(d.get("mac"))
                if m and m in by_mac:                      # SAME physical lamp (mac) at a NEW ip -> follow it
                    lamp = by_mac[m]                        # (roam-proof, and no mDNS latency). Update the
                    if (lamp.c.get("host") or "").lower() != d["host"].lower():   # config host + persist.
                        lamp.c["host"] = d["host"]; self._dirty = True
                        have.add(d["host"].lower())
                        log("discovery: ~", lamp.name, "moved to", d["host"])
                    continue
                name = d["name"] or d["host"]
                if name in names:                      # disambiguate identical names
                    name = "%s (%s)" % (name, d["host"].split(".")[-1])
                conf = {"name": name, "type": "wled", "host": d["host"]}
                if d.get("mac"):
                    conf["mac"] = d["mac"]
                lamp = make_lamp(conf, self.state)
                lamp.engine = self
                self.lamps.append(lamp); lamp.start()
                names.add(name); have.add(d["host"].lower())
                added += 1
                log("discovery: +", name, "@", d["host"])
        if added and not quiet:
            log("discovery:", added, "lamp(s) added")
        return added

    def _wifi_keepalive(self):
        """Anti-aging network heartbeat (2026-07-04 18h): the router's MTK driver
        evicts SILENT Wi-Fi clients (~15 min) — and the Mac is silent on en0 as
        soon as the lamps churn (internet goes through the cable). A ping of the
        router every 45 s keeps the association alive. Cost: nil."""
        while True:
            time.sleep(45)
            host = ((self.cfg.get("router") or {}).get("host"))
            if host:
                try:
                    subprocess.run(["ping", "-c", "1", "-t", "2", host],
                                   capture_output=True, timeout=6)
                except Exception:
                    pass

    def _start_lamps(self):
        for l in self.lamps:
            l.stop = True; l.q.put(None)
        self.lamps = [make_lamp(c, self.state) for c in self.cfg["lamps"]]
        for l in self.lamps:
            l.engine = self              # for synchronisation on connect
            l.start()

    def sync_patch(self, lamp):
        """Multi-lamp synchronisation (global "sync" config parameter, Benoit
        2026-07-03): when a lamp connects, we align it — on the state of the
        ALREADY connected lamps if possible (coming back mid-set), otherwise on
        the configured default state. The group stays coherent, and the dynamic
        display (based on the 1st targeted lamp) then tells the truth for all.
        Config: "sync": {"enabled": true, "state": {"on": true, "col": [0,100,200],
        "bri": 153}} — state = OLS patch."""
        sync = (self.cfg.get("sync") or {})
        if not sync.get("enabled"):
            return None
        others = [l for l in self.lamps if l is not lamp and l.ok]
        if others:
            o = others[0]
            return {"on": o.is_on, "col": list(o.rgb), "bri": round(o.bri * 2.55)}
        st = sync.get("state")
        return dict(st) if isinstance(st, dict) else None

    def _scene_names(self):
        return sorted({n for l in self.lamps for n in (l.c.get("scenes") or {})})

    # ----- engine animations (cycle / flash / tempo) -----
    def _stop_anims(self, tgts):
        for l in tgts:
            ev = self.anims.pop(l.name, None)
            if ev:
                ev.set()

    def _start_anim(self, tgts, worker):
        self._stop_anims(tgts)
        ev = threading.Event()
        for l in tgts:
            self.anims[l.name] = ev
        threading.Thread(target=worker, args=(ev,), daemon=True).start()

    def _anim_cmd(self, cmd, settings):
        """cycle:c1,c2[,..][@ms] | flash:couleur[@ms] | tempo:bpm | animstop.
        Capped rhythms: the lamps' firmware drops the session beyond
        ~4 acked commands/s (measured 2026-07-03)."""
        tgts = self.targets(settings)
        if cmd == "animstop":
            self._stop_anims(tgts); return True
        if cmd.startswith("cycle:"):
            cols, _, ms = cmd[6:].partition("@")
            colors = [c for c in cols.split(",") if c in lamp_mod.COLORS] or ["jaune", "bleu"]
            interval = max(0.4, int(ms or 800) / 1000)
            def worker(ev):
                i = 0
                while not (ev.wait(interval) if i else False):
                    col = lamp_mod.COLORS[colors[i % len(colors)]]
                    for l in tgts:
                        l.q.put({"col": list(col)})
                    i += 1
            self._start_anim(tgts, worker)
            return bool(tgts) and all(l.ok for l in tgts)
        if cmd.startswith("flash:"):
            cname, _, ms = cmd[6:].partition("@")
            dur = max(0.15, int(ms or 300) / 1000)
            col = list(lamp_mod.COLORS.get(cname, (255, 255, 255)))
            saved = {l.name: l.tracked_state() for l in tgts}
            def worker(ev):
                for l in tgts:
                    l.q.put({"col": col, "bri": 255})
                if ev.wait(dur):
                    return
                for l in tgts:                             # back to the previous state
                    s = saved[l.name]
                    l.q.put({"on": True, "col": s["rgb"], "bri": round(s["bri"] * 2.55)}
                            if s["on"] else {"on": False})
            self._start_anim(tgts, worker)
            return bool(tgts) and all(l.ok for l in tgts)
        if cmd.startswith("tempo:"):
            bpm = max(20, min(120, int(cmd.split(":")[1] or 100)))
            beat = 60.0 / bpm
            def worker(ev):
                while True:
                    for l in tgts:
                        l.q.put({"bri": 255})              # pulse on the beat
                    if ev.wait(beat * 0.3):
                        return
                    for l in tgts:
                        l.q.put({"bri": 50})
                    if ev.wait(beat * 0.7):
                        return
            self._start_anim(tgts, worker)
            return bool(tgts) and all(l.ok for l in tgts)
        return False

    # ----- beat-sync (Ableton Link / MIDI clock via the openlamp-midi beatsync) -----
    # beat:on|off|toggle supervises the standalone `beatsync` helper as a subprocess.
    # beatsync follows an external tempo source (Ableton Link by default) and drives
    # THIS engine's local API on the beat — latency-anticipated, downbeat accent. Kept
    # as a subprocess ON PURPOSE: the aalink / python-rtmidi deps stay out of the engine
    # core (only needed when you actually beat), matching openlamp-midi's standalone design.
    def _beatsync_cmd(self):
        """Base command to launch beatsync: explicit cfg > installed module > sibling repo."""
        bc = self.cfg.get("beat") or {}
        if bc.get("cmd"):
            return list(bc["cmd"])
        if importlib.util.find_spec("beatsync"):
            return [sys.executable, "-m", "beatsync"]
        sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "midi", "beatsync.py")
        if os.path.exists(sibling):
            return [sys.executable, os.path.abspath(sibling)]
        log("beat: beatsync not found — `pip install \"openlamp-midi[link]\"` "
            "or set cfg beat.cmd")
        return None

    def _beat_running(self):
        return self._beat_proc is not None and self._beat_proc.poll() is None

    def _start_beat(self, spec, settings):
        """spec = "toggle" | "on" | "<source>[:action[:colors[:sub]]]" (grammar aligned
        on OpenLamp; source = link|midi; defaults from cfg beat.*)."""
        base = self._beatsync_cmd()
        if not base:
            return False
        self._stop_beat()                          # restart clean if already running
        bc = self.cfg.get("beat") or {}
        parts = [] if spec in ("toggle", "on", "") else spec.split(":")
        source = parts[0] if parts else bc.get("source", "link")
        action = parts[1] if len(parts) > 1 else bc.get("action", "pulse")
        colors = parts[2] if len(parts) > 2 else bc.get("colors")
        sub = parts[3] if len(parts) > 3 else bc.get("sub")
        args = ["--source", source, "--action", action,
                "--api", "http://127.0.0.1:%d" % API_PORT]
        if bc.get("accent", True):
            args.append("--accent")
        if colors:
            args += ["--colors", str(colors)]
        if sub:
            args += ["--sub", str(sub)]
        names = (settings or {}).get("lamps") or []   # engine targeting -> --lamps
        if names:
            args += ["--lamps", ",".join(names)]
        try:
            self._beat_proc = subprocess.Popen(base + args)
        except Exception as e:
            log("beat: launch failed:", e)
            return False
        log("beat: on ->", " ".join(base + args))
        return True

    def _stop_beat(self):
        p = self._beat_proc
        self._beat_proc = None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(2)
            except Exception:
                p.kill()
            log("beat: off")

    def targets(self, settings):
        """Targeted lamps. settings.lamps = lamp OR GROUP names; empty = all.
        Groups are defined in the config: "groups": {"front": ["L1"], ...}."""
        names = (settings or {}).get("lamps") or []
        if not names:
            return self.lamps
        groups = self.cfg.get("groups") or {}
        expanded = []
        for n in names:
            expanded.extend(groups.get(n, [n]))
        return [l for l in self.lamps if l.name in expanded]

    def dispatch(self, cmd, settings):
        # v2 syntax: a WLED-compatible JSON state patch passes through as-is
        if isinstance(cmd, str) and cmd.startswith("{"):
            try:
                cmd = json.loads(cmd)
            except Exception as e:
                log("invalid JSON patch:", e); return False
        # snapshots: photo/recall of the state of all targeted lamps
        if isinstance(cmd, str) and cmd.startswith("snap:"):
            if cmd.startswith("snap:save:"):
                # capture VIA each lamp's queue: guarantees photographing the state
                # AFTER the in-flight commands (a running fade, etc.)
                name = cmd.split(":", 2)[2]
                tgts = self.targets(settings)
                holder = {}; evs = []
                for l in tgts:
                    ev = threading.Event(); evs.append(ev)
                    l.q.put(("__snap__", holder, ev))
                def _collect():
                    for ev in evs:
                        ev.wait(30)
                    self.cfg.setdefault("snapshots", {})[name] = holder
                    self._dirty = True
                    log("snapshot '%s' saved (%d lamps)" % (name, len(holder)))
                threading.Thread(target=_collect, daemon=True).start()
                return True
            name = cmd.split(":", 1)[1]
            snap = (self.cfg.get("snapshots") or {}).get(name)
            if not snap:
                log("snapshot unknown:", name); return False
            tgts = self.targets(settings)
            self._stop_anims(tgts)
            for l in tgts:
                s = snap.get(l.name)
                if not s:
                    continue
                patch = l.snapshot_patch(s)                 # driver-specific (WLED restores RGBW+cct+fx+pal)
                if patch:
                    l.q.put(patch)
            return bool(tgts) and all(l.ok for l in tgts)
        # beat-sync: supervises the external beatsync helper (see _start_beat)
        if isinstance(cmd, str) and (cmd == "beat" or cmd.startswith("beat:")):
            rest = cmd.split(":", 1)[1] if ":" in cmd else ""
            if rest in ("off", "stop") or (rest == "toggle" and self._beat_running()):
                self._stop_beat()
                return True
            return self._start_beat(rest, settings)
        # any blackout-ish command also kills the beat (it would keep repainting)
        if isinstance(cmd, str) and cmd in ("blackout", "off", "animstop"):
            self._stop_beat()
        if isinstance(cmd, str) and \
           (cmd == "animstop" or cmd.startswith(("cycle:", "flash:", "tempo:"))):
            return self._anim_cmd(cmd, settings)
        tgts = self.targets(settings)
        self._stop_anims(tgts)     # any normal command cuts the current animation
        if isinstance(cmd, str):
            if cmd in lamp_mod.COLORS:
                self.state["color"] = cmd
            elif cmd.startswith("set:"):                   # advanced: also memorises the colour
                self.state["color"] = cmd.split(":")[1]
        # per-button fondu: settings["tt"] (ms) arms a one-shot transition just for this
        # press, queued right before the command so it stays FIFO-ordered with it.
        tt_ms = (settings or {}).get("tt")
        for l in tgts:
            if tt_ms not in (None, ""):
                l.q.put("tt1:%s" % tt_ms)
            l.q.put(cmd)
        # frontend hook (e.g. refresh the Status keys) ~1.5 s later, the time for
        # the queued commands to run. Only engine -> frontend link.
        cb = self.on_change
        if cb:
            threading.Timer(1.5, cb).start()
        # NO save_config here: writing to Google Drive (File Provider) can block
        # for several seconds -> off the critical path of the key press.
        # The _saver thread persists in the background (every 2 s if modified).
        self._dirty = True
        return bool(tgts) and all(l.ok for l in tgts)

    def _saver(self):
        while True:
            time.sleep(2)
            if self._dirty:
                self._dirty = False
                try:
                    save_config(self.cfg)
                except Exception as e:
                    log("config save failed:", e)
