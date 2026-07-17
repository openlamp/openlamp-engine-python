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
    # paquet PyInstaller (store/alpha) : binaire dans <racine .sdPlugin>/bin/lampsd,
    # lamp.py + tuya-lamps.json sont a la RACINE du .sdPlugin
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    _FALLBACK = HERE
else:
    # mode dev : ce fichier vit dans scripts/lamps/sd-plugin, lamp.py un cran au-dessus
    HERE = os.path.dirname(os.path.abspath(__file__))
    _FALLBACK = os.path.dirname(HERE)
# Optional external source-of-truth for lamp.py + tuya-lamps.json, e.g. a synced
# folder (Google Drive/Dropbox) shared with the CLI. Set OPENLAMP_LAMPS_DIR to that
# path; otherwise the files bundled next to the plugin/script are used.
_EXT = os.path.expanduser(os.environ.get("OPENLAMP_LAMPS_DIR", ""))
LAMPS_DIR = _EXT if (_EXT and os.path.isdir(_EXT)) else _FALLBACK
CONFIG = os.path.join(LAMPS_DIR, "tuya-lamps.json")
LOGFILE = os.path.expanduser("~/Library/Logs/ElgatoStreamDeck/com.openlamp.lamps.log")

API_PORT = 8377   # API locale (127.0.0.1 uniquement) — voir LocalApi

_log_lock = threading.Lock()
def log(*a):
    with _log_lock:
        try:
            with open(LOGFILE, "a") as f:
                f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
        except Exception:
            pass

# lamp.py fournit COLORS, PALIERS, scale, cur_bri_pct, local_subnets, find_ip_any_subnet
spec = importlib.util.spec_from_file_location("lamp", os.path.join(LAMPS_DIR, "lamp.py"))
lamp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lamp_mod)
COLOR_ORDER = list(lamp_mod.COLORS.keys())

# le cache de balayage de lamp.py est concu pour un run CLI de quelques secondes ;
# dans un plugin qui vit des heures, on l'expire pour pouvoir retrouver une lampe
# qui a change de reseau en cours de route (constate : les lampes flappent box <-> Mango)
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
    """Instrumentation profonde opt-in ("debug": true dans la config) : dump
    protocolaire tinytuya (paquets, retries, negociation 3.5) vers un log dedie
    — le microscope quand le chrono d'ack ne suffit pas."""
    if not (cfg or {}).get("debug"):
        return
    import logging
    dbg = os.path.expanduser(
        "~/Library/Logs/ElgatoStreamDeck/com.openlamp.lamps-debug.log")
    logging.basicConfig(filename=dbg, level=logging.DEBUG,
                        format="%(asctime)s %(name)s %(message)s")
    tinytuya.set_debug(True)
    log("driver debug ACTIF ->", dbg)   # syntaxe generique = patch d'etat compatible WLED (cf SYNTAXE-MOTEUR.md)

_rejoin_last = [0.0]
def rejoin_stage_wifi(cfg):
    """Anti-zombie niveau 1.5 (appris 2026-07-04) : quand le Mac saute du Wi-Fi
    scene (auto-join maison pendant une absence du routeur), il n'y REVIENT
    jamais seul — bail fantome + lampes injoignables alors que le routeur va
    bien. Si le routeur ne repond plus ET qu'un SSID est configure
    ("router": {"ssid": "YOUR-WIFI", ...}), on re-joint d'office. L'erreur
    -3900 de networksetup est cosmetique : l'association suit quand meme.
    Throttle 3 min ; macOS re-bascule seul si le SSID est absent de l'air."""
    r = (cfg or {}).get("router") or {}
    host, ssid = r.get("host"), r.get("ssid")
    if not host or not ssid or sys.platform != "darwin":
        return False
    if time.time() - _rejoin_last[0] < 180:
        return False
    try:
        if subprocess.run(["ping", "-c", "1", "-t", "2", host],
                          capture_output=True, timeout=5).returncode == 0:
            return False                     # routeur joignable : rien a guerir
    except Exception:
        pass
    _rejoin_last[0] = time.time()
    try:
        subprocess.run(["networksetup", "-setairportnetwork", "en0", ssid],
                       capture_output=True, timeout=15)
        log("rejoin Wi-Fi scene:", ssid)
        return True
    except Exception as e:
        log("rejoin Wi-Fi rate:", e)
        return False

_deauth_last = {}
def mango_deauth(mac, cfg):
    """Anti-zombie niveau 1 : deassocie la lampe du Wi-Fi via SSH OpenWrt sur le
    routeur (GL.iNet). Elle se re-associe et sa pile IP repart — l'equivalent d'un
    debranche/rebranche RESEAU, sans toucher la lampe. Actif si la config contient
    "router": {"host": "192.168.8.1", "ssh_key": "~/.ssh/id_ed25519_mango"}
    (cle publique a installer dans le routeur : LuCI > System > Administration).
    Throttle 3 min par lampe : marteler la deauth aggraverait le cas."""
    r = (cfg or {}).get("router") or {}
    host = r.get("host")
    key = os.path.expanduser(r.get("ssh_key", ""))
    if not host or not key or not os.path.exists(key):
        return False
    if time.time() - _deauth_last.get(mac, 0) < 180:
        return False
    _deauth_last[mac] = time.time()
    # OpenWrt standard = hostapd via ubus ; builds MediaTek proprietaires
    # (GL-MT300N-V2…) = iwpriv DisConnectSta sur ra0. On tente les deux.
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
        log("deauth ratee:", e)
        return False

class BaseLamp(threading.Thread):
    """Base commune : file de commandes + boucle de vie. Sous-classes : _connect/_exec/_heartbeat."""
    def __init__(self, conf, state):
        super().__init__(daemon=True)
        self.c = conf; self.state = state
        self.q = queue.Queue()
        self.ok = False; self.stop = False
        self.bri = 60; self.is_on = True
        self.rgb = (0, 100, 200)        # dernier RGB envoye — sert a tt/blackout/snapshot
        self.saved = None               # etat sauve par blackout (restore le rejoue)
        self._next_tt = None            # fondu one-shot par commande (unites WLED x100 ms) pose par
                                        # "tt1:N" (un fondu par bouton), sinon None = defaut config

    def tracked_state(self):
        # photo de l'etat suivi cote moteur (snapshots, blackout/restore)
        return {"on": self.is_on, "bri": self.bri, "rgb": list(self.rgb)}

    def _post_exec_verify(self):
        pass                                   # surcharge TuyaLamp : verif couleur

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
                    log(self.name, "connexion ratee:", e)
                    ok = False
                if not ok:
                    # backoff progressif 3->30 s : marteler une lampe coincee
                    # aggrave son etat (creneau unique) ; on lui laisse de l'air
                    self._fails = getattr(self, "_fails", 0) + 1
                    if self._fails % 2 == 0:
                        # d'abord le RESEAU du Mac (saut d'auto-join ?), puis la lampe
                        try:
                            rejoin_stage_wifi(load_config())
                        except Exception:
                            pass
                    if self._fails % 4 == 0 and self.c.get("type", "tuya") == "tuya":
                        # ~4 echecs d'affilee = profil zombie -> deauth via routeur
                        try:
                            mango_deauth(self.c.get("mac", ""), load_config())
                        except Exception:
                            pass
                    time.sleep(backoff)
                    backoff = min(30, backoff * 2)
                    continue
                self._fails = 0
                # 1) clignotement "je suis online", 2) etat de sync (cf sync_patch)
                eng = getattr(self, "engine", None)
                try:
                    # anti-boucle (2026-07-04 17h) : sur radio degradee, la rafale
                    # du rainbow re-tue la session a CHAQUE reconnexion -> boucle
                    # infinie greet->mort. Apres 3 echecs consecutifs de salut/sync,
                    # on saute le greet jusqu'a une connexion qui tient.
                    greet = eng.cfg.get("greet", True) if eng else True
                    if getattr(self, "_greet_fails", 0) >= 3:
                        greet = False
                    if getattr(self, "eph", False) and getattr(self, "_greeted", False):
                        greet = False          # ephemere : un seul salut par demarrage
                    self._greeted = True
                    st = eng.sync_patch(self) if eng else None
                    prev = self.tracked_state()
                    if greet:
                        done = self._greet()
                        log(self.name, "rainbow welcome sweep" if done
                            else "rainbow welcome sweep (partial — flaky radio)")
                    if st:
                        self._exec(st)
                        log(self.name, "synchronisee a la connexion")
                    elif greet and prev.get("on", True):
                        # pas de sync : on rend a la lampe son etat d'avant le salut
                        self._exec({"on": True, "col": prev["rgb"],
                                    "bri": round(prev["bri"] * 2.55)})
                except Exception as e:
                    # salut/sync a moitie applique = etat batard (ex: reste blanc
                    # plein) -> on force une reconnexion propre plutot que de laisser
                    self._greet_fails = getattr(self, "_greet_fails", 0) + 1
                    self._last_err_ts = time.time()
                    log(self.name, "salut/sync de connexion rate:", e,
                        "-> reconnexion (echec greet #%d)" % self._greet_fails)
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
                    log(self.name, "heartbeat perdu -> reconnexion")
                    self.ok = False
                continue
            if cmd is None:
                break
            # requete d'etat synchrone (test d'integration, panneau) — passe par la
            # file donc s'execute APRES les commandes deja en attente (ordre garanti)
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
                _, holder, ev = cmd                        # photo APRES la file (FIFO)
                holder[self.name] = self.tracked_state()
                ev.set()
                continue
            # fondu par bouton : "tt1:N" (ms) arme un fondu ONE-SHOT pour la commande
            # SUIVANTE uniquement, sans toucher la config "transition" persistante.
            # dispatch() le met en file juste avant la vraie commande ; le driver
            # (_payload WLED, _fade_to Tuya) le consomme une fois.
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
                log(self.name, "erreur:", e, "-> reconnexion + retry")
                self.ok = False
                try:
                    if self._connect():
                        self._exec(cmd)
                except Exception as e2:
                    log(self.name, "retry rate:", e2); self.ok = False

# =====================================================================================
# AUTOPSIE DU PROTOCOLE TUYA — pourquoi cette classe est si defensive (2026-07-04)
# =====================================================================================
# Une journee entiere de debug (banc protocole nu + capture tcpdump routeur + lecture
# des issues tinytuya) a mis a nu POURQUOI piloter des ampoules Tuya en local est un
# calvaire. Chaque contournement plus bas repond a une de ces pathologies. Documente
# ici pour que la douleur serve, et pour justifier le choix de migrer vers WLED
# (cf WledLamp, l'exact inverse : HTTP en clair, sans etat, sans session).
#
# 1. CRENEAU DE CONNEXION UNIQUE. Une lampe Tuya n'accepte qu'UNE connexion TCP a la
#    fois (port 6668). L'app Smart Life sur le meme reseau LOCAL la prend et nous
#    l'affame -> "toujours fermer l'app avant". D'ou set_socketPersistent + la lampe
#    en tete de config, et la fragilite des le moindre concurrent.
#
# 2. SESSION 3.5 CHIFFREE QUI SE RENEGOCIE. Le protocole 3.5 ouvre une session avec
#    echange de cles (GCM). Sous charge, la lampe renegocie en boucle (mesure : ~20
#    renegos en 10 min dans le log debug). Chaque renego coute 2-10 s -> la "latence
#    horrible". Le mode ephemere (socket par commande) est PIRE : il paie la renego
#    a CHAQUE commande (A/B mesure le 2026-07-04 : ephemere perdant net).
#
# 3. ACK SANS EXECUTION (faux acquittements). Une session a moitie morte ACQUITTE un
#    envoi sans l'EXECUTER : le buffer de reponses se desynchronise, l'ack lu
#    appartient a la commande precedente. Consequence : le moteur croit "orange
#    applique" alors que la lampe reste verte. SEUL remede : relire la couleur reelle
#    (verify-after-write, cf _verify_colour) — l'ack ne suffit jamais.
#
# 4. REPONSE VIDE = SESSION ZOMBIE. Le socket TCP vit, mais la lampe jette nos paquets
#    sans repondre (payload b''). Indistinguable d'un succes au niveau TCP. On traite
#    donc "reponse vide" comme un echec -> reconnexion (cf _dps).
#
# 5. CACHE _set_values_check QUI DERIVE. tinytuya cache le dernier etat DP ; en rafale
#    il ne re-emet pas un DP "inchange" selon lui -> apres un mode:scene, le retour en
#    mode:colour n'etait plus envoye et la lampe ignorait les couleurs. D'ou l'envoi
#    EXPLICITE multi-DP a chaque fois (cf _dps), en contournant le cache.
#
# 6. PLAFOND DE DEBIT ~4 cmd/s. Au-dela, le firmware lache la session en plein fondu.
#    D'ou le pacing des fades (max 12 pas, >=250 ms) et la verif a cadence plafonnee.
#
# 7. CORRUPTION MEMOIRE FIRMWARE apres ~2 h d'uptime. Documente par les mainteneurs
#    tinytuya (discussion #443) : la lampe "oublie" sa cle locale, code 914, jusqu'au
#    power-cycle. Le multicast/broadcast du reseau accelere la corruption. AUCUN
#    correctif logiciel — d'ou la deauth niveau 1, et in fine le choix materiel WLED.
#
# 8. CLE LOCALE VIA LE CLOUD. Pour parler LOCAL a la lampe, il faut d'abord extraire
#    sa "local_key" du cloud Tuya (compte + API). Local qui depend du cloud : l'inverse
#    d'un protocole ouvert.
#
# VERDICT : Tuya optimise pour enfermer l'utilisateur dans son app/cloud, pas pour le
# controle local. WLED fait l'exact oppose (cf WledLamp). Ces ampoules restent
# supportees (materiel deja achete) mais ne sont PAS recommandees pour la scene.
# =====================================================================================
class TuyaLamp(BaseLamp):
    """Socket tinytuya persistant + multi-reseau (une IP par sous-reseau, cf lamp.py).
    Bardee de contournements — voir l'AUTOPSIE ci-dessus pour le pourquoi de chacun."""
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
        # MODE EPHEMERE par defaut (2026-07-04 19h45, decision Benoit "trop lent
        # trop instable") : le socket persistant monopolisait l'unique creneau de
        # connexion de la lampe et fabriquait les pathologies du jour (sessions
        # zombies, faux acks, tampon desynchronise, 20 renegociations 3.5 en 10
        # min). En ephemere : une connexion fraiche PAR commande (~150-300 ms),
        # rien ne pourrit entre deux appuis, le creneau reste libre. L'ancien
        # comportement : "connection": "persistent" dans la config.
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
        # recupere aussi la COULEUR reelle (DP24 = HHHH SSSS VVVV) : sans ca, apres
        # une reconnexion le suivi rgb retombait sur le defaut -> snapshots faux
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
        log(self.name, "connectee @", ip, "bri:", self.bri, "on:", self.is_on)
        return True

    def _heartbeat(self):
        if getattr(self, "eph", False):
            # pas de session a entretenir ; sonde TCP legere (1 appel sur 4,
            # soit ~36 s) pour que la touche Status detecte une lampe eteinte
            self._hb_tick = getattr(self, "_hb_tick", 0) + 1
            if self._hb_tick % 4 == 0:
                ip = (self.c.get("ips") or {}).get(self.c.get("last", ""), "")
                if ip and not self._probe(ip):
                    raise RuntimeError("sonde TCP: lampe eteinte ou hors Wi-Fi")
            return
        self.dev.heartbeat(nowait=False)

    def _post_exec_verify(self):
        # v3 (2026-07-04 18h35, angle mort trouve par Benoit) : le gating "erreur
        # recente" etait aveugle aux FAUX ACKS — une lampe qui acquitte sans
        # executer a l'air parfaitement saine, donc n'etait jamais verifiee.
        # Nouveau contrat : verification A CADENCE PLAFONNEE — au plus une
        # relecture par lampe toutes les 8 s, en fin de file. Cout ~8x sous le
        # plafond firmware, et plus aucun etat final ne diverge sans etre vu.
        now = time.time()
        recent_err = now - getattr(self, "_last_err_ts", 0) < 300
        if not recent_err and now - getattr(self, "_last_verify_ts", 0) < 8:
            return
        self._last_verify_ts = now
        if self.q.empty():                     # matraquage : on ne verifie que la fin
            self._verify_colour()

    def _dps(self, **caps):
        # Envoi EXPLICITE multi-DP en 1 seul paquet, SANS passer par le cache
        # tinytuya (_set_values_check) : avec des envois aveugles le cache derive,
        # et le retour scene->colour n'etait plus envoye -> la lampe restait en
        # mode scene et ignorait les couleurs (touches "sans effet", 2026-07-03).
        # AVEC accuse (nowait=False) : sur le socket persistant ca ne coute qu'un
        # aller-retour (~50-100 ms) et ca detecte les pertes silencieuses — en
        # rafale, le nowait perdait des commandes sans aucune erreur (2026-07-03).
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
            # chrono TOUJOURS logge (instrumentation Benoit 2026-07-04) : rend
            # visible l'asymetrie radio L1/L2 et le cout reel de chaque ack
            log(self.name, "ack", "+".join(sorted(caps.keys())), "en %.0f ms" % ms)
            if isinstance(r, dict) and r.get("Error"):
                raise RuntimeError("set_multiple_values: %s" % r["Error"])
            if not r:
                # reponse VIDE = session 3.5 morte-vivante : le socket TCP vit mais
                # la lampe jette nos paquets sans repondre (constate sur L2 le
                # 2026-07-03 : zero erreur, zero effet). Vide = echec -> reconnexion.
                raise RuntimeError("reponse vide (session morte ?)")

    def _colour(self, r, g, b):
        d = self.dev
        self._dps(switch=True, mode="colour",
                  colour=d.rgb_to_hexvalue(r, g, b, d.dpset["value_hexformat"]))
        self.is_on = True
        # verification DIFFEREE (v2 du fix "ack sans execution", 2026-07-04) :
        # la relecture inline coutait jusqu'a 6 s par appui (drainage a timeout
        # plein) — inacceptable en live. On note la cible ; la boucle de vie
        # verifie des que la file est VIDE, a timeout court (cf _verify_colour).
        # Matraquage de touches => seule la DERNIERE couleur est verifiee.
        self._want_rgb = (r, g, b)
        self._verify_tried = False

    def _verify_colour(self):
        """Verifie (vite) que la derniere couleur voulue est physiquement sur la
        lampe. Une session a moitie morte peut ACQUITTER sans EXECUTER (tampon
        de reponses desynchronise, constate 2026-07-04) : on relit la teinte a
        timeout court, on renvoie UNE fois, sinon on condamne la session."""
        want = getattr(self, "_want_rgb", None)
        if not want:
            return
        import colorsys
        r, g, b = want
        want_h = round(colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)[0] * 360)
        d = self.dev
        d.set_socketTimeout(0.7)               # relecture eclair, pas 3 s
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
            return                              # relecture impossible : pas d'avis
        finally:
            d.set_socketTimeout(3)
        cd = dps.get(str(d.dpset.get("colour") or 24), "")
        if not (isinstance(cd, str) and len(cd) >= 4):
            return
        got_h = int(cd[0:4], 16)
        diff = abs(got_h - want_h)
        if min(diff, 360 - diff) <= 10:
            self._want_rgb = None               # conforme : rien a faire
            return
        if not getattr(self, "_verify_tried", False):
            self._verify_tried = True
            log(self.name, "couleur non appliquee (lu %d, voulu %d) -> renvoi" %
                (got_h, want_h))
            self._dps(switch=True, mode="colour",
                      colour=d.rgb_to_hexvalue(r, g, b, d.dpset["value_hexformat"]))
        else:
            self._want_rgb = None
            raise RuntimeError("couleur non appliquee apres renvoi (session pourrie)")

    def _fade_to(self, rgb_target, pct_target, dur_ms):
        # tt EMULE (natif chez WLED) : interpolation pas-a-pas sur le socket
        # persistant. MAX ~4 pas/s : a ~10 pas/s acquittes le firmware des lampes
        # s'etouffe et lache la session en plein fondu (constate 2026-07-03).
        steps = max(2, min(12, int(dur_ms / 250)))
        r0, g0, b0 = self.rgb
        p0 = self.bri
        for i in range(1, steps + 1):
            f = i / steps
            # suivi mis a jour A CHAQUE pas : un crash en plein fondu laisse un
            # etat exact (avant : bri/rgb figes -> snapshots faux apres incident)
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
        # patch d'etat compatible WLED (SYNTAXE-MOTEUR.md) traduit en DPs Tuya.
        # Champs omis = inchanges, comme chez WLED.
        if "nl" in st:                                     # nightlight = countdown DP26
            nl = st["nl"] or {}
            self._dps(timer=(int(nl.get("dur", 0)) * 60) if nl.get("on") else 0)
        if st.get("music"):                                # extension
            self._dps(switch=True, mode="music"); self.is_on = True; return
        if "scene" in st:                                  # extension : scene nommee
            data = (self.c.get("scenes") or {}).get(st["scene"])
            if data:
                self._dps(switch=True, mode="scene", scene=data); self.is_on = True
            return
        if "ps" in st:                                     # preset -> scene memorisee
            self._dps(switch=True, mode="scene"); self.is_on = True; return
        if "cct" in st:                                    # blanc chaud<->froid (0-255)
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
                self._fade_to(tgt_rgb, tgt_pct, int(tt) * 100)   # tt en x100 ms (WLED)
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
        # socket persistant + envois nowait = les acks s'accumulent dans le buffer.
        # On draine TOUT (les vieux paquets contiennent des etats PERIMES) et on
        # garde la fusion — le dernier paquet lu reflete l'etat courant.
        dps = {}
        empty = 0
        for _ in range(8):
            st = self.dev.status()
            cur = st.get("dps", {}) if isinstance(st, dict) else {}
            if not cur:
                empty += 1
                if dps or empty >= 2:                      # 2 lectures vides = stop
                    break
                continue                                   # 1re vide : la lampe est lente
            dps.update(cur)
        return dps

    def _exec(self, cmd):
        d, COLORS, PALIERS = self.dev, lamp_mod.COLORS, lamp_mod.PALIERS
        if isinstance(cmd, dict):                          # syntaxe v2 : patch d'etat WLED
            self._apply(cmd); return
        if cmd == "blackout":                              # noir + memorise l'etat d'avant
            self.saved = self.tracked_state()
            self._dps(switch=False); self.is_on = False; return
        if cmd == "restore":                               # rejoue l'etat d'avant blackout
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
        if cmd == "toggle":                                # bascule (idee reprise du plugin WLED)
            self._dps(switch=not self.is_on)
            self.is_on = not self.is_on; return
        if cmd.startswith("wled:"):                        # specifique WLED : passe son tour
            return
        if cmd.startswith("set:"):                         # mode AVANCE : couleur + intensite
            _, cname, pct = cmd.split(":")
            pct = max(1, min(100, int(pct)))
            self.bri = pct
            self.rgb = COLORS.get(cname, COLORS["bleu"])
            r, g, b = lamp_mod.scale(self.rgb, pct)
            self._colour(r, g, b); return
        if cmd.startswith("white:"):                       # blanc : intensite + temperature
            _, bri, temp = cmd.split(":")
            self.bri = max(1, min(100, int(bri)))
            self._dps(switch=True, mode="white",
                      brightness=max(10, self.bri * 10),
                      colourtemp=max(0, min(1000, int(temp) * 10)))
            self.is_on = True; return
        if cmd.startswith("scene:"):                       # scene NOMMEE capturee (config)
            name = cmd.split(":", 1)[1]
            data = (self.c.get("scenes") or {}).get(name)
            if data:
                self._dps(switch=True, mode="scene", scene=data)
                self.is_on = True
            else:
                log(self.name, "scene inconnue:", name, "- capture-la d'abord")
            return
        if cmd.startswith("countdown:"):                   # minuterie d'extinction (minutes)
            self._dps(timer=max(0, int(cmd.split(":")[1])) * 60)
            return
        if cmd.startswith("preset:"):                      # commun : Tuya rejoue SA scene
            self._dps(switch=True, mode="scene")           # (le N ne sert qu'a WLED)
            self.is_on = True; return
        if cmd.startswith("mode:"):                        # modes bruts de l'app Tuya (DP21)
            self._dps(switch=True, mode=cmd.split(":")[1])
            self.is_on = True; return
        if cmd in COLORS:                                  # couleur : garde l'intensite
            self.rgb = COLORS[cmd]
            tt = self._next_tt                             # fondu par bouton -> fade Tuya doux, une fois
            if tt:
                self._next_tt = None
                self._fade_to(COLORS[cmd], self.bri, tt * 100); return
            r, g, b = lamp_mod.scale(COLORS[cmd], self.bri)
            self._colour(r, g, b); return
        pct = PALIERS.get(cmd)
        if pct is None and cmd.startswith("bri:"):
            pct = max(1, min(100, int(cmd.split(":")[1])))
        if pct is not None:                                # intensite : garde la couleur
            self.bri = pct
            color = self.state.get("color", "bleu")
            self.rgb = COLORS.get(color, COLORS["bleu"])
            tt = self._next_tt
            if tt:
                self._next_tt = None
                self._fade_to(self.rgb, pct, tt * 100); return
            r, g, b = lamp_mod.scale(self.rgb, pct)
            self._colour(r, g, b); return
        log(self.name, "commande inconnue:", cmd)

class WledLamp(BaseLamp):
    """Lampes/rubans sous firmware WLED (ESP8266/ESP32), API HTTP JSON locale.

    L'EXACT OPPOSE de TuyaLamp (cf AUTOPSIE au-dessus de cette classe). Pourquoi WLED
    est simple et fiable la ou Tuya est un calvaire :
    - Pas de session : chaque commande est un POST HTTP autonome et sans etat. Rien a
      "maintenir", donc rien ne peut "pourrir" (vs pathologies Tuya #2, #4, #7).
    - Pas de creneau unique : WLED accepte plusieurs connexions simultanees (vs #1).
    - Pas de chiffrement/negociation : requete en clair, reponse immediate (vs #2).
    - Pas de faux ack : le POST /json/state repond l'etat reel avec {"v":true} — la
      lampe ne ment pas, donc pas besoin de verify-after-write acrobatique (vs #3).
    - Pas de cle cloud : rien a extraire, l'IP locale suffit (vs #8).
    - Debug trivial : on tape http://<host>/ dans un navigateur et on voit tout (vs
      la capture tcpdump a 22h).
    Une commande WLED tient en une ligne : POST {"seg":[{"col":[[255,0,0]]}]}.

    Config : {"name": "L1", "type": "wled", "host": "192.168.8.50"} — host = IP OU
    hostname mDNS (ex "wled-abc123.local"). Options : "segment": N (zone du ruban),
    "transition": N (fondu x100 ms par commande)."""

    def _url(self, path):
        return "http://%s%s" % (self.c["host"], path)

    def _post(self, payload, verify=False):
        # POST autonome, sans session. verify=True -> WLED renvoie l'etat reel
        # ({"v":true}) qu'on relit pour rafraichir le suivi. Un echec HTTP/reseau
        # leve -> la boucle de vie reconnecte (mais "reconnecter" = juste re-pinger,
        # il n'y a pas de session a rouvrir : cf _connect).
        body = dict(payload)
        if verify:
            body["v"] = True
        req = urllib.request.Request(
            self._url("/json/state"), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        last = None
        for attempt in (1, 2):                 # 1 retry : couvre un timeout Wi-Fi isole
            try:
                raw = urllib.request.urlopen(req, timeout=3).read()
                return json.loads(raw) if verify and raw else None
            except Exception as e:
                last = e
                time.sleep(0.15)
        raise RuntimeError("WLED POST echec: %s" % last)

    def _connect(self):
        # "Connexion" WLED = simple sanity-check HTTP (pas de session a ouvrir).
        try:
            info = json.loads(urllib.request.urlopen(
                self._url("/json/info"), timeout=3).read())
            self.ok = True
            log(self.name, "(WLED) OK @", self.c["host"],
                "ver", info.get("ver", "?"), "leds", (info.get("leds") or {}).get("count", "?"))
            # recupere l'etat reel a la connexion (pour les touches Status)
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
        # WLED dit toujours vrai : on aligne le suivi moteur sur son etat reel.
        if not isinstance(st, dict):
            return
        if "on" in st:
            self.is_on = bool(st["on"])
        if "bri" in st:
            self.bri = max(1, min(100, round(st["bri"] / 2.55)))
        segs = st.get("seg") or []
        if segs and segs[0].get("col"):
            self.rgb = tuple(segs[0]["col"][0][:3])

    def _seg(self, seg):
        # config "segment": N -> cette "lampe" du plugin ne pilote qu'une zone du ruban
        if "segment" in self.c:
            seg["id"] = self.c["segment"]
        return seg

    def _payload(self, p):
        # priorite tt : patch explicite > fondu par bouton (one-shot) > defaut config
        if self._next_tt is not None:
            p.setdefault("tt", self._next_tt); self._next_tt = None
        if "transition" in self.c:
            # config "transition": N -> fondu (x100 ms) applique a chaque commande
            p.setdefault("tt", self.c["transition"])
        return p

    def _apply(self, st):
        # syntaxe v2 = l'API WLED : quasi-passthrough
        p = {}; seg = {}
        for k in ("on", "bri", "tt", "ps", "nl"):
            if k in st: p[k] = st[k]
        for k in ("fx", "sx", "ix", "pal", "cct"):
            if k in st: seg[k] = st[k]
        if st.get("col"):
            seg["col"] = [list(st["col"][:3])]
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
        if isinstance(cmd, dict):                          # syntaxe v2 : patch d'etat WLED
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
            self._post(self._payload({"on": "t"}))        # "t" = toggle natif WLED
            self.is_on = not self.is_on; return
        if cmd.startswith("set:"):                         # avance : couleur + intensite
            _, cname, pct = cmd.split(":")
            r, g, b = COLORS.get(cname, COLORS["bleu"])
            self.bri = max(1, min(100, int(pct)))
            self._post(self._payload({"on": True, "bri": round(self.bri * 2.55),
                        "seg": [self._seg({"col": [[r, g, b]]})]}))
            self.is_on = True; return
        if cmd.startswith("preset:"):                      # commun : WLED a 250 presets
            self._post(self._payload({"ps": int(cmd.split(":")[1])}))
            self.is_on = True; return
        if cmd.startswith("wled:fx:"):                     # effet anime : fx[:sx[:ix[:pal]]]
            parts = cmd.split(":")[2:]
            fx = parts[0] if parts[0] in ("~", "~-", "r") else int(parts[0])
            seg = self._seg({"fx": fx})
            for k, v in zip(("sx", "ix", "pal"), parts[1:]):
                seg[k] = int(v)
            self._post(self._payload({"on": True, "seg": [seg]}))
            self.is_on = True; return
        if cmd.startswith("wled:psave:"):                  # sauvegarde l'etat en preset N
            self._post({"psave": int(cmd.split(":")[2])}); return
        if cmd.startswith("mode:"):
            log(self.name, "(WLED) modes Tuya non applicables — utiliser les presets WLED")
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

class LocalApi(threading.Thread):
    """Porte d'entree LOCALE du plugin (principe d'architecture, 2026-07-03).

    Pourquoi : une lampe Tuya n'accepte qu'UNE connexion locale — detenue par ce
    process tant que Stream Deck tourne. Tout autre frontal (CLI lamp.py, Bome
    "Execute file", raccourcis macOS, scripts…) doit donc passer PAR le plugin.
    Cette mini API HTTP (127.0.0.1 uniquement, jamais exposee au reseau) est ce
    point d'entree : le meme vocabulaire de commandes que les touches, la meme
    latence (connexions persistantes). Stream Deck n'est qu'UN frontal parmi
    d'autres — le moteur reste integrable ailleurs sans rien remettre en question.

      GET /cmd?c=<commande>[&lamps=L1,L2]  -> {"ok": bool, "targets": [...]}
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
                # endpoint compatible WLED (OpenLamp State) : un patch d'etat JSON
                # s'applique aux lampes ciblees (?lamps=..., vide = toutes). Les
                # outils qui savent parler a un WLED peuvent piloter les Tuya.
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
                self._reply({"success": bool(ok)})       # meme reponse que WLED

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/json/state":
                    # etat agrege facon WLED + detail par lampe en extension
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
                    # contrat public de la syntaxe v2 (cf SYNTAXE-MOTEUR.md)
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
                        # relecture PHYSIQUE via les connexions persistantes (FIFO,
                        # donc apres les commandes en attente) — debug + tests
                        holder = {}; evs = []
                        for l in plugin.lamps:
                            ev = threading.Event(); evs.append(ev)
                            l.q.put(("__status__", holder, ev))
                        for ev in evs:
                            ev.wait(8)
                        for n, dps in holder.items():
                            body.setdefault(n, {})["dps"] = dps
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
            log("API locale prete sur 127.0.0.1:%d" % API_PORT)
            srv.serve_forever()
        except OSError as e:
            log("API locale indisponible (port %d):" % API_PORT, e)


class Engine:
    """OpenLamp engine host: owns the lamps (persistent connections), the dispatcher,
    groups, snapshots, animations, sync and the local API. Frontend-agnostic — the
    only upward link is the on_change hook (see module docstring)."""
    def __init__(self):
        self.cfg = load_config()
        _maybe_enable_driver_debug(self.cfg)
        self.state = self.cfg.setdefault("state", {"color": "bleu"})
        self.lamps = []
        self.anims = {}          # lampe -> Event d'arret de l'animation en cours
        self._beat_proc = None   # subprocess beatsync (beat:on) — None si arrete
        self.on_change = None    # hook frontal, appele ~1,5 s apres un dispatch
        self._dirty = False
        self._start_lamps()
        LocalApi(self).start()   # porte d'entree CLI / MIDI / Bome / autres frontaux
        threading.Thread(target=self._saver, daemon=True).start()
        threading.Thread(target=self._wifi_keepalive, daemon=True).start()

    def _wifi_keepalive(self):
        """Battement reseau anti-aging (2026-07-04 18h) : le driver MTK du routeur
        expulse les clients Wi-Fi SILENCIEUX (~15 min) — et le Mac est silencieux
        sur en0 des que les lampes churnent (internet passe par le cable). Un ping
        du routeur toutes les 45 s garde l'association vivante. Cout : nul."""
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
            l.engine = self              # pour la synchronisation a la connexion
            l.start()

    def sync_patch(self, lamp):
        """Synchronisation multi-lampes (parametre global "sync" de la config,
        Benoit 2026-07-03) : a la connexion d'une lampe, on l'aligne — sur l'etat
        des lampes DEJA connectees si possible (retour en cours de set), sinon sur
        l'etat par defaut configure. Le groupe reste coherent, et l'affichage
        dynamique (base sur la 1re lampe ciblee) dit alors vrai pour toutes.
        Config : "sync": {"enabled": true, "state": {"on": true, "col": [0,100,200],
        "bri": 153}} — state = patch OLS."""
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

    # ----- animations moteur (cycle / flash / tempo) -----
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
        Rythmes plafonnes : le firmware des lampes lache la session au-dela de
        ~4 commandes acquittees/s (mesure 2026-07-03)."""
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
                for l in tgts:                             # retour a l'etat d'avant
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
                        l.q.put({"bri": 255})              # pulse sur le temps
                    if ev.wait(beat * 0.3):
                        return
                    for l in tgts:
                        l.q.put({"bri": 50})
                    if ev.wait(beat * 0.7):
                        return
            self._start_anim(tgts, worker)
            return bool(tgts) and all(l.ok for l in tgts)
        return False

    # ----- beat-sync (Ableton Link / MIDI clock via openlamp-midi beatsync) -----
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
        log("beat: beatsync introuvable — `pip install \"openlamp-midi[link]\"` "
            "ou renseigner cfg beat.cmd")
        return None

    def _beat_running(self):
        return self._beat_proc is not None and self._beat_proc.poll() is None

    def _start_beat(self, spec, settings):
        """spec = "toggle" | "on" | "<source>[:action[:colors[:sub]]]" (grammaire alignee
        sur OpenLamp ; source = link|midi ; defauts en cfg beat.*)."""
        base = self._beatsync_cmd()
        if not base:
            return False
        self._stop_beat()                          # repart propre si deja lance
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
        names = (settings or {}).get("lamps") or []   # ciblage engine -> --lamps
        if names:
            args += ["--lamps", ",".join(names)]
        try:
            self._beat_proc = subprocess.Popen(base + args)
        except Exception as e:
            log("beat: echec du lancement:", e)
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
        """Lampes visees. settings.lamps = noms de lampes OU DE GROUPES ; vide = toutes.
        Les groupes sont definis dans la config : "groups": {"front": ["L1"], ...}."""
        names = (settings or {}).get("lamps") or []
        if not names:
            return self.lamps
        groups = self.cfg.get("groups") or {}
        expanded = []
        for n in names:
            expanded.extend(groups.get(n, [n]))
        return [l for l in self.lamps if l.name in expanded]

    def dispatch(self, cmd, settings):
        # syntaxe v2 : un patch d'etat JSON compatible WLED passe tel quel
        if isinstance(cmd, str) and cmd.startswith("{"):
            try:
                cmd = json.loads(cmd)
            except Exception as e:
                log("patch JSON invalide:", e); return False
        # snapshots : photo/rappel de l'etat de toutes les lampes ciblees
        if isinstance(cmd, str) and cmd.startswith("snap:"):
            if cmd.startswith("snap:save:"):
                # capture VIA la file de chaque lampe : garantit de photographier
                # l'etat APRES les commandes en cours (un fondu qui tourne, etc.)
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
                    log("snapshot '%s' sauve (%d lampes)" % (name, len(holder)))
                threading.Thread(target=_collect, daemon=True).start()
                return True
            name = cmd.split(":", 1)[1]
            snap = (self.cfg.get("snapshots") or {}).get(name)
            if not snap:
                log("snapshot inconnu:", name); return False
            tgts = self.targets(settings)
            self._stop_anims(tgts)
            for l in tgts:
                s = snap.get(l.name)
                if not s:
                    continue
                if s.get("on", True):
                    l.q.put({"on": True, "col": s.get("rgb"),
                             "bri": round(s.get("bri", 60) * 2.55)})
                else:
                    l.q.put({"on": False})
            return bool(tgts) and all(l.ok for l in tgts)
        # beat-sync : suivi de tempo externe (Ableton Link / MIDI clock) via beatsync.
        # Grammaire alignee sur OpenLamp : beat:off|stop | beat:toggle |
        # beat:<source>[:action[:colors[:sub]]]  (beat / beat:on == beat:link:pulse)
        if isinstance(cmd, str) and (cmd == "beat" or cmd.startswith("beat:")):
            rest = cmd.split(":", 1)[1] if ":" in cmd else "toggle"
            if rest in ("off", "stop") or (rest == "toggle" and self._beat_running()):
                self._stop_beat()
                return True
            return self._start_beat(rest, settings)    # "toggle"/"on"/"<source>[:...]"
        # commandes "tout couper" : arretent aussi le beat en cours
        if isinstance(cmd, str) and cmd in ("off", "blackout", "animstop"):
            self._stop_beat()
        if isinstance(cmd, str) and \
           (cmd == "animstop" or cmd.startswith(("cycle:", "flash:", "tempo:"))):
            return self._anim_cmd(cmd, settings)
        tgts = self.targets(settings)
        self._stop_anims(tgts)     # toute commande normale coupe l'animation en cours
        if isinstance(cmd, str):
            if cmd in lamp_mod.COLORS:
                self.state["color"] = cmd
            elif cmd.startswith("set:"):                   # avance : memorise aussi la couleur
                self.state["color"] = cmd.split(":")[1]
        # fondu par bouton : settings["tt"] (ms) arme un fondu one-shot juste pour cette
        # pression, mis en file juste avant la commande pour rester ordonne (FIFO) avec elle.
        tt_ms = (settings or {}).get("tt")
        for l in tgts:
            if tt_ms not in (None, ""):
                l.q.put("tt1:%s" % tt_ms)
            l.q.put(cmd)
        # hook frontal (ex: rafraichir les touches Status) ~1,5 s apres, le temps
        # que les commandes en file s'executent. Seul lien moteur -> frontal.
        cb = self.on_change
        if cb:
            threading.Timer(1.5, cb).start()
        # PAS de save_config ici : ecrire dans Google Drive (File Provider) peut
        # bloquer plusieurs secondes -> hors du chemin critique de l'appui.
        # Le thread _saver persiste en arriere-plan (toutes les 2 s si modif).
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
                    log("sauvegarde config ratee:", e)
