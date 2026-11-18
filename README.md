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

**Testing on real hardware.** The addon runs in Home Assistant with two Wi-Fi hubs and six real tags, replacing
the stock access point. Tags move between hubs when one goes away, but a tag can take about 15 minutes to notice.

| Part | State |
|---|---|
| Hub firmware (Wi-Fi and USB) | Works on real boards (49–50 of 50 image pushes in a row over Wi-Fi) |
| Browser installer | Works (flashing and Wi-Fi setup). If Wi-Fi setup doesn't appear after flashing, unplug and replug the board |
| Home Assistant addon | Works with real hubs and the OpenEPaperLink integration |
| Addon web panel | Works: tags, hubs, settings, log, image upload, test image, backup and import from a stock AP |
| Electricity price chart | Works from a Nordpool sensor (same look as the stock AP's day-ahead content) |
| QR code | Website, Wi-Fi, contact, phone, SMS, email, location, event or text; drawn once with a caption |

## Installing the addon

In Home Assistant: *Settings → Add-ons → Add-on store → ⋮ → Repositories*, add
`https://github.com/ScuttleSE/oepl-multihub`, then install **OEPL multihub**. In the addon options, list your hubs
(the address is the hub's IP, or a serial device such as `/dev/ttyACM0`; the channel is optional).
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

## Known limitations

- The OpenEPaperLink integration supports **one access point**. Replace your existing access point's entry rather
  than running both.
- Home Assistant gets tag type definitions from the upstream OpenEPaperLink repository. Tag types that aren't
  merged there yet can't be drawn from Home Assistant.
- Built-in content modes of the stock access point (weather, calendar, …) aren't supported, except the
  electricity price chart. Home Assistant provides the images.
- A hub accepts one connection at a time. Don't point other tools at a hub while the addon uses it.
- When a hub goes away, its tags take about 15 minutes to find another hub (tag firmware behaviour).

## For developers

- Project plan, findings and design notes: [`docs/PLAN.md`](docs/PLAN.md)
- Hub firmware: the `host-link` branch of [ScuttleSE/OpenEPaperLink](https://github.com/ScuttleSE/OpenEPaperLink/tree/host-link)
  (`ARM_Tag_FW/OpenEPaperLink_esp32_C6_AP`)
- `tools/hubtool.py`: talk to hubs directly, push images, or run a simulated hub
- `tests/e2e_fake.sh`: end-to-end test with simulated hubs
- Run the addon from source (Python 3.11+, aiohttp and Pillow):
  `PYTHONPATH=addon python -m oeplhub.server --data ./data --hub HOST[:PORT]@CHANNEL --port 8080`
