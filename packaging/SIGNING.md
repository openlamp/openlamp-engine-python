# Signing & notarising OpenLamp

> ## ⏸ Status: DEFERRED — do this only when we ship a public download
>
> **Not needed yet, on purpose.** Signing/notarising exists solely to remove the Gatekeeper /
> SmartScreen warning **when third parties download the app**. For personal / source use the
> unsigned bundle runs fine for free (`python3 app.py`, or the built `.app` via right-click →
> Open / `xattr -d com.apple.quarantine OpenLamp.app`).
>
> **Why deferred:** the only cost in the whole project is the **Apple Developer Program — 99 $/yr**
> (the gate to a Developer ID cert). We're not paying it until there's a concrete trigger:
> **a public download link** (a Releases asset, a website, a store). Decision recorded 2026-07-16.
>
> **When that day comes:** follow the steps below (they're complete and ready — nothing else to
> figure out). macOS needs the Apple Developer Program + a *Developer ID Application* cert;
> Windows needs an Authenticode `.pfx`.

The bundle from `build.sh` runs, but macOS Gatekeeper / Windows SmartScreen will warn users
until it's **signed** (and, on macOS, **notarised**). These steps need **your** developer
certificate — they can't be automated in CI without your credentials, and are the one part of
shipping this app that stays a manual, you-only step.

## macOS — Developer ID sign + notarise

Prereqs: an Apple Developer account, a **Developer ID Application** certificate in your login
keychain, and an app-specific password (or an API key) for `notarytool`.

```sh
APP="dist/OpenLamp.app"
DEV_ID="Developer ID Application: Your Name (TEAMID)"

# 1. Sign (deep, hardened runtime — required for notarisation).
codesign --deep --force --options runtime --timestamp --sign "$DEV_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"     # should say "valid on disk"

# 2. Notarise (zip, submit, wait).
ditto -c -k --keepParent "$APP" OpenLamp.zip
xcrun notarytool submit OpenLamp.zip \
  --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-pw" --wait

# 3. Staple the ticket so it validates offline.
xcrun stapler staple "$APP"
spctl --assess --type execute --verbose "$APP"           # should say "accepted / Notarized Developer ID"
```

Notes:
- `python-rtmidi` and `tinytuya` bundle native code → **hardened runtime + timestamp are mandatory**
  or notarisation fails.
- If notarytool reports unsigned nested binaries, sign the offending `.so`/`.dylib` inside
  `Contents/Frameworks` first, then re-sign the app.

## Windows — Authenticode

Prereqs: a code-signing certificate (`.pfx`).

```bat
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  /f your-cert.pfx /p CERT_PASSWORD dist\OpenLamp\OpenLamp.exe
```

## Status

**Untested end-to-end here.** The launcher (`app.py`) and the spec are written and the source
launcher is import-checked, but the actual PyInstaller build + sign + notarise must run on your
machine with your certs. Treat this as a scaffold to run, not a shipped binary.
