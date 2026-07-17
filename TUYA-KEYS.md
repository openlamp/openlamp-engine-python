# Getting your Tuya lamps' local keys (one-time setup)

OpenLamp drives your lamps **100% locally** — but the Tuya protocol encrypts local
traffic with a per-device `local_key`. You fetch each key **once**, through Tuya's
own official cloud API, then the cloud is never needed again.

Time needed: ~15 minutes. Difficulty: easy if you follow the steps in order.

## What you need

- Your lamps already set up in the **Smart Life** (or Tuya Smart) mobile app
- A free account on [iot.tuya.com](https://iot.tuya.com) (Tuya's developer portal)
- Python 3 on your computer (only for this step): `pip install tinytuya`

## Step 1 — Create a (free) Tuya cloud project

1. Sign up / log in at **https://iot.tuya.com**.
2. Go to **Cloud → Development → Create Cloud Project**.
   - Industry: *Smart Home* · Development Method: *Smart Home*
   - **Data Center: pick the one matching your Smart Life app region**
     (Europe → *Central Europe Data Center*). A wrong data center is the #1
     cause of "empty device list" later.
3. In the project's **Service API** step, make sure *IoT Core* and
   *Authorization Token Management* are enabled.

> 💡 The IoT Core trial is free and renewable — it's enough for key retrieval.

## Step 2 — Link your Smart Life account

1. Open your project → **Devices** tab → **Link App Account** → *Add App Account*.
2. A QR code appears. In the **Smart Life app**: `Me → ⛭ (top right) →
   Scan QR code` (or `Me → Third-party services`), and scan it.
3. Your lamps now appear in the project's device list. ✅

## Step 3 — Grab your API credentials

In the project **Overview** tab, copy:
- **Access ID / Client ID**
- **Access Secret / Client Secret**

## Step 4 — Run the tinytuya wizard

```bash
pip install tinytuya
python3 -m tinytuya wizard
```

Answer the prompts:

```
Enter API Key   → your Access ID
Enter API Secret→ your Access Secret
Enter Device ID → just press Enter (or "scan")
Region          → eu   (match your data center!)
Download DP mapping? → Y
Poll local devices?  → Y
```

The wizard writes **`devices.json`** in the current folder — it contains, for each
lamp: `id` (device_id), `key` (**the local_key you're after**), name and more.

## Step 5 — Put the keys into OpenLamp

Open any OpenLamp key in the Stream Deck app → **⚙️ Lamp configuration** → edit the
JSON — one entry per lamp:

```json
{
  "name": "L1",
  "mac": "d8:c8:0c:xx:xx:xx",
  "device_id": "bf0553cce7e28685bdrunt",
  "local_key": "PASTE_THE_KEY_HERE",
  "ips": { "192.168.1": "192.168.1.27" }
}
```

- `mac`: shown by `python3 -m tinytuya scan`, or in your router's client list —
  it lets OpenLamp **re-find the lamp automatically** when its IP changes.
- `ips`: one entry per network you use (home, travel/stage router…).

Click **Save + reload connections** — your lamps should blink their welcome. 🎉

## Troubleshooting

| Symptom | Fix |
|---|---|
| Wizard finds 0 devices | Wrong **data center/region** — recreate the project with the right one |
| `local_key` changes | Re-pairing a lamp in the app regenerates its key — re-run the wizard |
| Lamp unreachable | It only accepts **one local connection**: close other tinytuya scripts; power-cycle if stuck |
| "Permission denied" API errors | In the project, re-enable *IoT Core* (trial may need renewal) |

*This guide uses Tuya's official, documented developer flow — you're fetching keys
for your own devices through your own account.*
