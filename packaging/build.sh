#!/bin/sh
# Build the OpenLamp headless desktop app. Run from the repo root:  packaging/build.sh
# Produces dist/OpenLamp.app (macOS) or dist/OpenLamp/ (Windows). Sign afterwards — see SIGNING.md.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Deps: pyinstaller + the runtime libs. pystray+Pillow are optional (tray icon).
python3 -m pip install --quiet --upgrade pyinstaller python-rtmidi tinytuya pystray Pillow

echo "Building OpenLamp (headless bundle)…"
python3 -m PyInstaller --noconfirm --clean packaging/openlamp.spec

echo
echo "Done. Output in dist/."
echo "macOS: dist/OpenLamp.app  —  now sign + notarise (packaging/SIGNING.md)."
echo "Windows: dist/OpenLamp/OpenLamp.exe  —  now sign with signtool (packaging/SIGNING.md)."
