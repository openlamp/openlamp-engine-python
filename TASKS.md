# TASKS — openlamp-engine

- 🔄 **Packager le moteur en appli desktop no-install** (`.app` macOS + `.exe` Windows ; tray ; **signée + notarisée** ; **headless**) → enlève la seule vraie barrière du chemin universel (« lancer un process Python »). **Tier 1 accessibilité** (principe « accessible à tous d'abord », cf. [openlamp/live DESIGN.md](https://github.com/openlamp/live/blob/main/docs/DESIGN.md)). Scope **headless-only** (statut + config des lampes uniquement, pas de surface de contrôle riche).
  - ✅ **Scaffold en place** : `app.py` (launcher combiné = `engine.Engine()` + pont MIDI `start_bridge()` dans UN process + tray pystray optionnel) ; `midi.py` refactoré (`start_bridge()` non-bloquant, réutilisable, testé) ; `packaging/openlamp.spec` (PyInstaller, `.app` LSUIElement/tray-only) + `packaging/build.sh` + `packaging/SIGNING.md`.
  - ☐ **Build (gratuit)** : `packaging/build.sh` produit le `.app`/`.exe` **non signé** — suffisant pour usage perso (clic-droit → Ouvrir, ou `xattr -d com.apple.quarantine`). Aucun coût.
  - ⏸ **Signature + notarisation = DIFFÉRÉE par choix (2026-07-16)** — exige le **Apple Developer Program (99 $/an)**, seule dépense du projet. Ne sert QU'À la **distribution publique** (éviter l'alerte Gatekeeper au téléchargement par des tiers). **À revisiter uniquement quand on voudra un lien de téléchargement public** ; d'ici là, l'usage perso/source ne paie rien. Le how-to reste prêt dans `SIGNING.md`.

- ✅ **Port MIDI de retour `OpenLamp Feedback`** (moteur → DAW) : `feedback: true` → port MIDI OUT échoant l'état en langage wled-midi (looks exclusifs, util on/off, CC 1-8). **+ état RÉEL** : `feedback_wled: {host, channel}` s'abonne au WebSocket WLED (`ws://<host>/ws`) et reflète l'état ACTUEL du device (on/off→util, bri→CC1, couleur→CC3/4, fx→CC5), donc aussi les **changements externes** (app WLED, toggle physique). Mapping état→MIDI self-testé (émet seulement sur changement). Besoin : `pip install websocket-client`. Le rendu riche (nom d'effet) reste Tier 2 (M4L).

## See also

- Les tâches runtime de `midi.py` (test end-to-end sur vraies lampes, rate-limit du beat pulse via UDP realtime, test MPE, Link refinement, MIDI 2.0) sont suivies dans [openlamp/wled-midi TASKS.md](https://github.com/openlamp/wled-midi/blob/main/TASKS.md) — le moteur est l'implémentation de référence de cette convention.
