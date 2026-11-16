# OEPL multihub

Run your [OpenEPaperLink](https://github.com/OpenEPaperLink/OpenEPaperLink) tags from Home Assistant, with
small ESP32-C6 boards as radio hubs instead of a dedicated access point.

```
tags ──802.15.4──  C6 hub ──Wi-Fi──┐
tags ──802.15.4──  C6 hub ──Wi-Fi──┼──  Home Assistant addon  ──  OpenEPaperLink integration
tags ──802.15.4──  C6 hub ──USB────┘
```

- **Hubs** are cheap ESP32-C6 boards running the stock OEPL radio firmware, with the link to the host over Wi-Fi
  (or USB) instead of a serial cable to an ESP32-S3.
- **The addon** does what the access point's main processor did: tag database, image conversion, delivery to the
  tags, and the same web API. The official OpenEPaperLink Home Assistant integration talks to it as if it were a
  normal access point.
- **Several hubs, one system.** Put a hub in each part of the house. Tags pick the hub they hear best, and the
  addon follows them, so all tags still appear as one access point in Home Assistant.
- **Tags are untouched.** They keep the stock OpenEPaperLink firmware.

## Status

**Early, not ready for use yet.** The parts are written and tested against simulated hardware, and the addon has
worked with the real Home Assistant integration. Testing on real boards and tags starts in late November 2026.

| Part | State |
|---|---|
| Hub firmware (Wi-Fi and USB) | Builds; not yet tested on a board |
| Browser installer | Online; not yet tested on a board |
| Addon core and API | Works with the HA integration and simulated hubs |
| Home Assistant addon packaging | Written; not yet installed on HA |
| Addon web UI | Not started |

## Installing the addon

In Home Assistant: *Settings → Add-ons → Add-on store → ⋮ → Repositories*, add
`https://github.com/ScuttleSE/oepl-multihub`, then install **OEPL multihub**. In the addon options, list your hubs
(`spec` is `HOST[:PORT]` or a serial device such as `/dev/ttyACM0`, and `channel` is 11–26, or 0 for automatic).
Then add the OpenEPaperLink integration with your Home Assistant host's IP as the AP address.

## What you need

- One or more **ESP32-C6** boards with 4 MB flash (for example an "ESP32-C6 Super Mini"). A small board covers a
  room or two; use more hubs for a larger home.
- OpenEPaperLink tags with stock OEPL firmware.
- Home Assistant with the [OpenEPaperLink integration](https://github.com/OpenEPaperLink/Home_Assistant_Integration).

## Installing a hub

Open **[the installer](https://scuttlese.github.io/oepl-multihub/)** in Chrome or Edge on a computer, connect the
C6 with a USB cable, and click **Install**:

- **Wi-Fi hub:** the normal choice. After flashing, the installer asks for your Wi-Fi name and password. The hub
  then appears on your network as `oepl-hub-xxxx.local`.
- **USB hub:** for a hub plugged straight into the Home Assistant machine.

If the browser can't find the board, hold the **BOOT** button while plugging it in.

Pick a free 802.15.4 channel for each hub, away from Zigbee and Thread (channels 25 and 26 are often quiet).

## Running the addon

The Home Assistant addon package doesn't exist yet. For testing, the addon runs from source on any machine with
Python 3.11 or newer:

```sh
pip install -r requirements.txt
python -m oeplhub.server --data ./data --hub oepl-hub-1a2b.local@26 --hub oepl-hub-3c4d.local@25 --port 8080
```

`--hub` takes `HOST[:PORT]@CHANNEL` for a Wi-Fi hub or `/dev/ttyACM0@CHANNEL` for a USB hub. Then add the
OpenEPaperLink integration in Home Assistant with the host `<this machine>:8080`.

## Known limitations

- The OpenEPaperLink integration supports **one access point**. Replace your existing access point's entry rather
  than running both.
- Home Assistant gets tag type definitions from the upstream OpenEPaperLink repository. Tag types that aren't
  merged there yet can't be drawn from Home Assistant.
- Built-in content modes of the stock access point (weather, calendar, …) aren't supported. Home Assistant
  provides the images.

## For developers

- Project plan, findings and design notes: [`docs/PLAN.md`](docs/PLAN.md)
- Hub firmware: the `host-link` branch of [ScuttleSE/OpenEPaperLink](https://github.com/ScuttleSE/OpenEPaperLink/tree/host-link)
  (`ARM_Tag_FW/OpenEPaperLink_esp32_C6_AP`)
- `tools/hubtool.py`: talk to hubs directly, push images, or run a simulated hub
- `tests/e2e_fake.sh`: end-to-end test with simulated hubs
