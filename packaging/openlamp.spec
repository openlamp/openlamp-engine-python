# PyInstaller spec — bundle the OpenLamp engine + wled-midi bridge into one headless app.
#
# Build:  pyinstaller packaging/openlamp.spec   (run from the repo root)
# Output: dist/OpenLamp.app (macOS) / dist/OpenLamp/ (Windows).
# Sign + notarise afterwards — see packaging/SIGNING.md (needs your Apple/Windows cert).
#
# app.py loads engine.py / midi.py / lamp.py by file path at runtime, so they ship as DATA
# files next to the executable (PyInstaller unpacks them into sys._MEIPASS). rtmidi + tinytuya
# are the native deps; pystray + Pillow are optional (tray icon) and included if installed.

import os
block_cipher = None
ROOT = os.path.abspath(os.getcwd())

datas = [(os.path.join(ROOT, f), ".") for f in ("engine.py", "midi.py", "daemon.py", "lamp.py")]

a = Analysis(
    ["../app.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=["rtmidi", "tinytuya", "pystray", "PIL.Image", "PIL.ImageDraw"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="OpenLamp",
    console=False,            # windowed/headless (tray only, no terminal)
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="OpenLamp")

# macOS: wrap into a .app bundle. LSUIElement=1 => menubar/tray only, no Dock icon.
app = BUNDLE(
    coll,
    name="OpenLamp.app",
    bundle_identifier="org.openlamp.app",
    info_plist={
        "LSUIElement": True,
        "CFBundleName": "OpenLamp",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
    },
)
