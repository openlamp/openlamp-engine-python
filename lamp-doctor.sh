#!/bin/sh
# lamp-doctor.sh — diagnose "lamps unreachable" by testing the 3 causes IN ORDER
# (Benoit's triad, 2026-07-04):
#   1. Mac not on the stage Wi-Fi (YOUR-WIFI)   -> join it
#   2. Mango crashed / radio down                -> power-cycle the router
#   3. lamps powered off or radio-napping        -> deauth kick, else replug lamps
# NEVER trust the GL.iNet web panel: it renders from service-worker CACHE even
# with the router dead. Ground truth = air scan + SSH + TCP probes (this script).
CFG="$(cd "$(dirname "$0")" && pwd)/tuya-lamps.json"
PY=/usr/bin/python3
SSID=$($PY -c "import json;print(json.load(open('$CFG')).get('router',{}).get('ssid','YOUR-WIFI'))")
HOST=$($PY -c "import json;print(json.load(open('$CFG')).get('router',{}).get('host','192.168.8.1'))")
KEY=$(eval echo $($PY -c "import json;print(json.load(open('$CFG')).get('router',{}).get('ssh_key','~/.ssh/id_ed25519_mango'))"))
MACS=$($PY -c "import json;print(' '.join(l['mac'] for l in json.load(open('$CFG'))['lamps'] if l.get('mac')))")

echo "— cause 2 d'abord (la plus structurante) : le Mango émet-il ?"
if ping -c 1 -t 2 "$HOST" >/dev/null 2>&1; then
  echo "  ✅ routeur joignable ($HOST)"
else
  # PIEGE macOS (appris 2026-07-04) : system_profiler CAVIARDE les noms de SSID
  # sans permission Localisation -> "absent du scan" ne prouve RIEN. On tente
  # directement la re-jointure (marche meme si le scan est aveugle ; l'erreur
  # -3900 est cosmetique, l'association suit souvent quand meme).
  networksetup -setairportnetwork en0 "$SSID" >/dev/null 2>&1
  sleep 8
  if ping -c 2 -t 3 "$HOST" >/dev/null 2>&1; then
    echo "  🟡 CAUSE 1 (resolue) : le Mac n'etait pas sur $SSID -> re-jointure OK."
  else
    echo "  🔴 CAUSE 2 probable : $HOST muet meme apres re-jointure."
    echo "     MAIS verifier d'abord depuis un AUTRE appareil (iPhone -> http://$HOST)."
    echo "     Si l'iPhone repond : probleme cote Mac (bail fantome, radio) -> couper/rallumer le Wi-Fi du Mac."
    echo "     Si l'iPhone ne repond pas non plus : debrancher/rebrancher le routeur (JAMAIS de reboot SSH)."
    exit 2
  fi
fi

echo "— cause 3 : les lampes"
FAIL=0
for MAC in $MACS; do
  ASSOC=$(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 "root@$HOST" \
    "iwinfo ra0 assoclist 2>/dev/null" 2>/dev/null | grep -ic "$MAC")
  IP=$($PY -c "
import json
for l in json.load(open('$CFG'))['lamps']:
    if l.get('mac')=='$MAC':
        print((l.get('ips') or {}).get(l.get('last','') , ''))")
  TCP=ko; [ -n "$IP" ] && $PY -c "
import socket,sys
s=socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(('$IP',6668))==0 else 1)" 2>/dev/null && TCP=OK
  if [ "$ASSOC" -ge 1 ] && [ "$TCP" = "OK" ]; then
    echo "  ✅ $MAC : associée + port Tuya ouvert ($IP)"
  elif [ "$ASSOC" -ge 1 ]; then
    echo "  🟡 $MAC : associée mais TCP fermé (sieste radio / créneau occupé)"
    echo "     -> déauth : ssh root@$HOST 'iwpriv ra0 set DisConnectSta=$MAC'"
    FAIL=1
  else
    echo "  🔴 CAUSE 3 : $MAC absente du Wi-Fi -> lampe éteinte ou en boot."
    echo "     -> vérifier l'alimentation / débrancher-rebrancher la lampe."
    FAIL=1
  fi
done
exit $FAIL
