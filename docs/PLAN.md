# OEPL hub: ESP32-C6 Wi-Fi radios + Home Assistant addon

Project writeup, plan and working notes, for agents and developers. Started 16 Nov 2026. The user-facing intro is
`README.md` in the repo root.

## Current state and how to continue (16 Nov 2026)

**Waiting for hardware:** 2× ESP32-C6 Super Mini, due about 19 Nov. Everything below is built and tested without
hardware (against the simulated hub in `oeplhub/fakehub.py`). Nothing has run on a real C6 or a real tag yet.

**What exists:**
- **Hub firmware:** branch `host-link` of `ScuttleSE/OpenEPaperLink`, project `ARM_Tag_FW/OpenEPaperLink_esp32_C6_AP`.
  Kconfig choice "Host link": UART (stock, default), USB (`main/host_usb.c`), Wi-Fi (`main/host_wifi.c` with mDNS and
  TCP 6639, `main/improv.c` for Improv Serial, own partition table `partitions-wifi.csv`). All three compile.
- **Installer:** https://scuttlese.github.io/oepl-multihub/ (ESP Web Tools). It's built by
  `.github/workflows/firmware.yml` from the fork commit in `firmware/upstream-ref`, using `firmware/build.sh`.
  **After pushing to the fork, update `firmware/upstream-ref`** or the installer keeps the old build.
- **Python package `oeplhub/`:** `v2proto` (framing), `transport` (serial and TCP), `link` (`HubLink`), `fakehub`,
  `imageenc` (stock AP image format, checked against a real AP file), `tagdb`, `tagtypes`, `core` (AP logic, several
  hubs, roaming), `server` (stock-compatible HTTP and websocket API, aiohttp).
- **Tools:** `tools/hubtool.py` (`monitor`, `push --image/--file`, `fakehub --hw --tag`), `tests/e2e_fake.sh`, and
  `tests/stop_fakes.sh`.
- **Tested with the real HA integration** (step 7): drawing on a fake tag worked end to end.

**Next steps, in order:**
1. **When the boards arrive:** use the installer's **USB hub** on one board, then `python3 tools/hubtool.py monitor
   /dev/ttyACM0 --channel 26` and watch for tag 1 (`8C73DAFFFE5D531B`; it's on the stock AP on channel 11 now, so
   it needs a rescan or the stock AP switched off). Then `push … --image x.png --tagtype tagtypes/94.json --tag-fw
   0x2B`. Check that opening the port doesn't reset the chip (DTR/RTS) and look at the `lateMs` timing.
2. Reflash as a **Wi-Fi hub** with the installer, which tests Improv. Repeat step 1 against `oepl-hub-xxxx.local`.
   Then do the coexistence test (plan step 5) and the roaming test with two hubs (step 6).
3. **Addon packaging** for HA (`config.yaml`, Dockerfile, hub list in the options, port 80 or ingress), then keep
   pending items across restarts, then the HA-style web UI.
4. **Ask upstream** to merge PR #638 (`94.json`). Until then HA can't draw on 0x94 tags.

**Environment notes:**
- **ESP-IDF:** `export IDF_TOOLS_PATH=$PWD/toolchain/.espressif; . toolchain/esp-idf/export.sh`, then
  `bash ../../../firmware/build.sh` in `upstream/ARM_Tag_FW/OpenEPaperLink_esp32_C6_AP`. `toolchain/` and
  `upstream/` (the fork clone, remote `upstream` = the official repo) are git-ignored.
- **Network:** the dev PC is `192.168.0.3`. HA reaches it, and HA's requests come from `192.168.0.80`. The stock AP
  is `192.168.50.205` (read-only use was allowed).
- **HA:** the user's real HA has the stock AP's OEPL entry. To test the addon, the user must disable that entry
  (the integration supports one AP), then re-enable it afterwards. Use fake MACs (`00000000FA4E000x`) and a tag type
  that is upstream (`--hw 0x41`).
- **Gotcha:** `pkill -f`/`pgrep -f` with a pattern that also appears in your own shell command kills that shell.
  Use `tests/stop_fakes.sh` (anchored pattern).
- **Python:** Pillow and aiohttp are installed on the dev PC (Python 3.14). `gh` is logged in as ScuttleSE.

## Goal

Replace the OpenEPaperLink (OEPL) access point with:

1. One or more **hubs**. Each hub is a single **ESP32-C6** running the stock OEPL C6 radio firmware. The only change
   is that its host link goes over TCP on Wi-Fi instead of UART.
2. A **Home Assistant addon** that does everything the AP's ESP32-S3 "brain" does today: tag database,
   image encoding, data and block serving, web UI and API.

Constraints:
- **Tags stay on stock OEPL firmware.** Over the air, a hub must look exactly like a normal OEPL AP radio.
- **Several hubs for coverage.** One addon owns all hubs and keeps one tag database.
- The hub firmware change should be clean enough to offer upstream as a build option.

## Background: how a stock OEPL AP works

Upstream: https://github.com/OpenEPaperLink/OpenEPaperLink (last checked at `8395ac8`, master).

```
tags ──802.15.4── radio chip (C6/H2) ──UART── ESP32-S3 (the "AP": logic, web UI, ~18k lines C++)
```

- **Radio firmware:** `ARM_Tag_FW/OpenEPaperLink_esp32_C6_AP/` (ESP-IDF, about 3,300 lines in `main/`).
  Prebuilt in `binaries/ESP32-C6/` (bootloader @0x0, partitions @0x8000, app @0x10000; 4 MB flash).
- **S3 firmware:** `ESP32_AP-Flasher/`. The parts our addon must re-implement:
  - `src/serialap.cpp`, `src/v2transport.cpp`: the radio link
  - `src/newproto.cpp`, `src/pendingupload.cpp`: pending data, block serving, check-in handling
  - `src/tag_db.cpp`: tag database
  - `src/makeimage.cpp`, `src/g5/`: image encoding (and G5 compression)
  - `src/web.cpp`: the HTTP and websocket API that the HA integration and web UI use
  - `src/contentmanager.cpp`: built-in content (weather, calendar, …). Low priority, since HA provides images.
- **Tag types:** `resources/tagtypes/*.json` (resolution, colours, bpp, layout per hwid).
- **Repo helpers worth reading:** `miscellaneous/ap-tool/aptool.py` drives an AP over its web API,
  `miscellaneous/virtual_tags/`.

### The radio link (protocol v2): the important part

Defined in **`oepl-radio-v2.h`** (shared header, about 360 lines, well commented).
Radio side: `main/v2link.c`, `main/main.c`. S3 side: `src/v2transport.cpp`.

- **Startup:**
  1. The link starts with ASCII at 115200: host sends `RDY?` and `NFO?`, and the radio answers `ACK>`, info lines,
     and `CAP>xxxx` with its capability bits.
  2. Host sends `FRM1`, the radio sends `ACK>`, and both wait 20 ms (`V2_SWITCH_DELAY_MS`).
  3. Both switch to 2 Mbaud binary frames (`uart_switch_speed(V2_BAUD)` in `main.c` `processAscii()`). The first frame is `HELLO`.
- The C6 firmware is **v2-only** after startup. The old ASCII protocol (`legacytransport.cpp`) is not supported, so ignore it.
- **Frames:** `COBS(header{type,seq,flags} | payload | crc16) 0x00`, little-endian, packed.
  - Every non-response frame gets exactly one response (`ACK`, `NAK`, `HELLO_REPLY` or `STATUS`).
  - **ACK timeout: 30 ms**, with **3 retries** (same seq, flagged as a retransmit). The receiver drops duplicate seqs.
  - **Heartbeat: 1000 ms.**
- **Host → radio:** `HELLO`, `HEARTBEAT`, `SET_CONFIG` (802.15.4 channel, tx power, lock mode, LED),
  `SET_PENDING`, `CANCEL_PENDING`, `DATA_CHUNK` (≤240 B, window of 4), `DATA_DROP`, `GET_STATS`,
  `ALLOW_CLEAR`, `ALLOW_ADD`, `ALLOW_REMOVE`.
- **Radio → host:** `CHECKIN`, `DATA_NEEDED`, `BLOCK_SERVED`, `XFER_COMPLETE`, `XFER_TIMEOUT`, `TAG_RETURN_DATA`, `LOG`.
- **Block cache (`V2_CAP_BLOCK_CACHE`):**
  1. With `V2_PENDING_DATA_FOLLOWS`, the host pushes the whole image to the radio as `DATA_CHUNK`s up front.
  2. The radio then serves the tag's 4 KB block requests **locally**, without asking the host.
  
  This is why Wi-Fi latency should hardly matter: the tag never waits on a network round trip.
- **Allow-list and lock (`V2_CAP_ALLOW_LIST`):** the radio can be told to answer only listed tags.
  This could help with multiple hubs (see below).

### Over TCP instead of UART

- On the C6, all host I/O goes through `main/second_uart.c`:
  - `init_second_uart()`
  - `uartTx()`, `uartWrite()`, `uartRead()`
  - `uartFlushInput()`
  - `uart_switch_speed()`
  
  There is also one direct `uart_wait_tx_done(1, …)` call in `main.c` `processAscii()`.
- Plan: add `main/wifi_link.c` with the same functions backed by a **single-client TCP server**, chosen with a Kconfig
  option (`OEPL_HOST_LINK_UART` / `OEPL_HOST_LINK_WIFI`):
  - **Baud switch:** `uart_switch_speed()` does nothing, and the host simply continues with frames after `FRM1`/`ACK>`.
  - **`uart_wait_tx_done` call:** needs a guard or an equivalent.
  - **Wi-Fi:** STA mode with `WIFI_PS_NONE`. mDNS name `oepl-hub-N.local`, TCP port **6639**.
  - **Debug output:** stays on the USB console.
  - **Client disconnect:** the radio should behave as after a host restart. Check what v2link does on a new `HELLO`.
- Concerns to verify:
  - **30 ms ACK timeout:** Wi-Fi jitter will cause retransmits. Dedup by seq makes them harmless, but the host's
    retransmit logic must be tolerant. If needed, raise the timeout for the TCP build only.
  - **Missed heartbeats:** what does the radio do when they stop? Check `v2link.c`.
  - **Coexistence:** Wi-Fi and 802.15.4 share the C6's single 2.4 GHz radio (time-sliced, ESP-IDF coex).
    The risk is missed tag frames. Test it.

## Multiple hubs

Stock OEPL already supports multiple APs, and **tags roam on their own**:
- The radio firmware offers channels `{11, 15, 20, 25, 26, 27}` (`main.c` `channelList`).
  The host sets a hub's channel with `SET_CONFIG`.
- The tag firmware scans all channels at boot and joins one AP. After several missed check-ins it rescans
  (`enableScanForAPAfterTimeout`, on by default). Tags can't be pinned to a hub without changing tag firmware, which we
  avoid.
- How the tag ranks APs when it hears more than one isn't verified yet (RSSI/LQI?). It's in the tag firmware's radio code,
  https://github.com/OpenEPaperLink/Tag_FW_EFR32xG22 (`oepl_radio.c`, `start_scan`).

Addon behaviour:
- One TCP connection per hub (hubs listed in the addon config). Give each hub a distinct channel at connect.
- Track `tag → hub` from `CHECKIN` events.
- Send `SET_PENDING` and the data only to the tag's current hub. If a tag hasn't been seen yet, send it to all hubs.
- **Roaming:** when a tag shows up on another hub, send `CANCEL_PENDING` on the old hub and re-send to the new one.
- **Hub reconnect:** re-send all pending data for that hub.
- Unlike stock multi-AP setups (`udp.cpp` syncs tag databases between APs), there's one database, so no sync is needed.

## Radio environment (the user's home, read 16 Nov 2026)

| What | Channel | Frequency |
|---|---|---|
| Wi-Fi UniFi U6 Lite | 6, 20 MHz | 2427–2447 MHz |
| Wi-Fi UniFi U7 Pro XG | 11, 20 MHz | 2452–2472 MHz |
| Zigbee (Zigbee2MQTT) | 802.15.4 ch 11 | 2405 MHz |
| Thread (`ha-thread-7d11`, SLZB-06P7) | 802.15.4 ch 15 | 2425 MHz |

OEPL channel plan:
- 11 and 15 are taken by Zigbee and Thread. 20 (2450 MHz) sits between Wi-Fi 6 and 11, so avoid it.
- **Hub 1: ch 26 (2480 MHz).** **Hub 2: ch 25 (2475 MHz)**, about 3 MHz above Wi-Fi 11, so watch it.
- Ideally the hubs associate with the U6 Lite (Wi-Fi ch 6), farther from 25 and 26.
- The SLZB-06P7 (Thread RCP) already uses TCP port 6638, so the hubs use **6639**.

## Hardware

- **Hubs:** 2× Heemol "ESP32-C6 Mini" (Super Mini style, ESP32-C6FH4 with 4 MB flash in the package, small ceramic
  antenna, USB-C on the native USB, BOOT and RST buttons, TX/RX labelled, probably GPIO16/17). Arriving about 19 Nov 2026.
  The ceramic antenna means short range, a room or two per hub.
  - Chip spec: 160 MHz RISC-V, 512 KB HP-SRAM, 16 KB LP-SRAM, 4 MB in-package flash. The Wi-Fi build uses
    205 KB of the 452 KB DIRAM statically, which leaves about 247 KB for heap (Wi-Fi needs some of that at runtime).
    The firmware's pins (USB 12/13, LEDs 22/23, default UART 2/3, CC1101 probe 0/1/4–7) aren't among the pins used
    by the in-package flash.
- **Fallback if coexistence is bad:** a plain ESP32 (several spares available) as a UART⇄TCP bridge (for example
  ESPHome `stream_server`), wired to a C6 running the stock firmware.
  - Wiring: TX/RX crossed, GND, plus optionally C6 EN for reset and GPIO9 for boot.
  - The addon doesn't change. It's the same byte stream, but the bridge must handle the 2 Mbaud switch.
- **Simplest test setup:** one C6 on USB to the dev PC or the HA box, stock firmware, host link over USB serial.

## Tags

- **Main test tags:** SoluM EL058F5W4C (M3 5.85" BWRY, 792x272, two cascaded JD controllers), on OEPL tag firmware.
  - Support for them is upstream-pending:
    - Tag firmware: https://github.com/OpenEPaperLink/Tag_FW_EFR32xG22/pull/49
    - Tag type `94.json` (hwid 0x94, `usetemplate` 65): https://github.com/OpenEPaperLink/OpenEPaperLink/pull/638
  - Image data: 2 bpp, 4 colours (0 black, 1 white, 2 yellow, 3 red). The panel needs no special pixel reordering
    from the host on OEPL. The tag firmware handles that.
- **Tag 1, OEPL MAC `8C73DAFFFE5D531B`** (label on the tag: `EF70A4D777DF`): debug-unlocked, on OEPL. Use it for tests.
- More OEPL tags work as long as their tagtype JSON exists.

## Toolchain

- **C6 firmware:** ESP-IDF **v5.5.5**, as in OEPL CI (`.github/workflows/release.yml`, `espressif/esp-idf-ci-action@v1`).
  Not installed yet. Choose one:
  - (a) local install inside this repo's `toolchain/` (git-ignored), or
  - (b) build with GitHub Actions using the same CI action.
- **Flashing:** `esptool.py` is installed on the dev PC.
- **Host side:** Python, standard library only where possible. Note that the addon ships as a Docker image, so it can
  have dependencies, but keep them minimal.
- **OEPL source:** clone upstream rather than copying it in. Work on a fork branch of
  https://github.com/OpenEPaperLink/OpenEPaperLink (the user's fork: https://github.com/ScuttleSE/OpenEPaperLink).

## Plan

1. [ ] **Toolchain:** choose (a) or (b), then build the stock C6 radio firmware unchanged and flash a C6.
   (Only the flashing is left.)
   - Chose (a). ESP-IDF v5.5.5 is in `toolchain/` (`IDF_TOOLS_PATH=toolchain/.espressif`, Python 3.14).
     The fork is cloned at `upstream/` (git-ignored). The stock C6 build works. Flashing waits for the boards (~19 Nov).
   - Fork branch **`host-link`** (`ScuttleSE/OpenEPaperLink`): Kconfig choice `OEPL_HOST_LINK_UART` (default, as
     stock) / `OEPL_HOST_LINK_USB` (native USB Serial/JTAG, new `main/host_usb.c`). Uses `uartWaitTxDone()` instead of
     the direct `uart_wait_tx_done` call. In the USB build the debug console is on UART0 only. Build in
     `ARM_Tag_FW/OpenEPaperLink_esp32_C6_AP`:
     `idf.py -B build-usb -D SDKCONFIG=sdkconfig.usb -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.host-usb" build`.
     Both builds compile. The Wi-Fi option (`OEPL_HOST_LINK_WIFI`) goes into the same choice later.
   - Watch out: on the USB build the host must not reset the chip through DTR/RTS when it opens the port.
2. [ ] **`hubtool.py` over USB serial** (stock firmware, no Wi-Fi yet):
   - ASCII startup, `FRM1`, 2 Mbaud, v2 frames (COBS, CRC16, seq, ACK and retries, heartbeat)
   - `SET_CONFIG` the channel
   - log `CHECKIN`s from tag 1
   
   Design it for several hubs from the start (one link object per hub).
   - **Written and tested against a simulated hub** (16 Nov). Not yet tested against a real C6.
     - `oeplhub/` (stdlib only, reused by the addon later): `v2proto.py` (framing, structs), `transport.py`
       (serial: native USB or `uart:` adapter, and TCP), `link.py` (`HubLink`: startup, requests with retries,
       chunk window, events with ACK and dedup, heartbeat, 3 missed means link lost), `fakehub.py` (simulated radio
       with fake tags, `--loss` and `--jitter`).
     - `tools/hubtool.py monitor HUB... --channel 26 25`, `push HUB --mac --file`, `fakehub`.
     - On connect it tries `HELLO` first, so it can pick up a radio that is still in v2 (host restart within 5 s).
       Otherwise it does the ASCII startup.
     - Works against the simulated hub with 5% frame loss each way and 15 ms jitter, in both cache-preload and
       `DATA_NEEDED` modes.
3. [ ] **Push one image** to tag 1: `SET_PENDING` with `DATA_FOLLOWS`, then `DATA_CHUNK`s, and wait for `XFER_COMPLETE`.
   Use `94.json` for the format. Check against `newproto.cpp` and `v2transport.cpp` (the dataType, the image header, and
   whether G5 or raw is used).
   - **Encoder written** (`oeplhub/imageenc.py`, a port of `colorquant.h` and `makeimage.cpp`; needs Pillow).
     `hubtool.py push HUB --mac … --image x.png --tagtype 94.json --tag-fw 0x…` works against the simulated hub.
   - Format for 0x94 (from the stock AP):
     - **Palette:** index = order in `colortable`: 0 white, 1 black, 2 red, 3 yellow. Pixels are quantised from
       RGB565.
     - **Bit planes:** plane 0 = index 1 or 3, plane 1 = index 2 or 3 (so yellow is set in both). Row-major, MSB
       first, no row padding, `ceil(w*h/8)` bytes each.
     - **dataType:** `0x30` ZLIB if tag fw ≥ `zlib_compression` (0x27). Otherwise `0x21` RAW_2BPP (plane 0 then
       plane 1), or `0x20` if nothing is red or yellow. 94.json has no `g5_compression`, so no G5 (not implemented).
     - **ZLIB file:** `u32 uncompressed size` + a zlib stream with a **4 KB window** (`wbits=12`, header `48 C7`, the
       same bytes as the AP) of `[6, w u16, h u16, bpp]` + plane 0 + plane 1.
     - **dataVer:** first 8 bytes of the file's MD5, little-endian.
     - **dataTypeArgument:** `lut:2 | preload:1 | specialType:5`, LSB first.
     - The integration's default dither is ordered (2). The AP decodes JPEG with TJpgDec, so pixels may differ
       slightly from Pillow. Byte-identical files aren't expected.
   - Open: read tag 1's firmware version from a real check-in (that decides zlib).
   - **Checked against the running stock AP** (16 Nov): tag 1 reports fw `43` (0x2B), `imgtype 48` (0x30 ZLIB).
     Its current file (`docs/ap-samples/tag1-8C73DAFFFE5D531B.raw`) has the same structure as our encoder's output:
     size 53862, zlib header `48 C7`, inner header `06 1803 1001 02`, and `hash` = MD5[:8]. There was no pixel
     comparison, because the source JPEG is gone from the AP.
4. [ ] **C6 firmware Wi-Fi transport** (`wifi_link.c`, Kconfig switch, mDNS, power-save off). Point `hubtool.py` at
   `oepl-hub-1.local:6639` and repeat steps 2–3.
   - **Written and compiles** (16 Nov, branch `host-link`): `main/host_wifi.c`, Kconfig `OEPL_HOST_LINK_WIFI`.
     Not tested on hardware.
     - Build: `idf.py -B build-wifi -D SDKCONFIG=sdkconfig.wifi -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.host-wifi" build`.
       Set SSID and password with `menuconfig` (`OEPL config`) or in a local defaults file. Never commit them.
       If they're empty, the hub uses the credentials stored in NVS (for Improv later).
     - STA mode, `WIFI_PS_NONE`, auto-reconnect. mDNS hostname `oepl-hub-xxxx` (last 2 MAC bytes; can be set in
       Kconfig) plus service `_oepl-hub._tcp`. TCP server on port 6639, one client, a new client replaces the old one.
       TCP_NODELAY, keepalive 5+2×3 s.
     - On client disconnect the radio keeps its v2 state and falls back to ASCII after 5 s, as on UART.
       `HubLink.connect()` handles both cases (it tries `HELLO` first).
     - Own partition table `partitions-wifi.csv`: nvs, otadata, 2 × 1.8 MB app slots (ready for OTA), no littlefs (the
       radio doesn't use it). Built with `-Os`: 0xDB290 bytes, 47% of a slot. With the stock 1 MB partition and
       `-Og` it was 95%.
     - Since the build uses `-Os` instead of stock `-Og`, check the radio timing (`lateMs` in `BLOCK_SERVED`).
5. [ ] **Coexistence test:** 50+ image pushes over Wi-Fi. Count `XFER_TIMEOUT`s, retransmits and missed check-ins.
   If it's bad, use the ESP32-bridge fallback.
6. [ ] **Roaming test:** two hubs on channels 25 and 26. Power one off and confirm that the tag rescans, appears on the
   other hub, and gets its pending image.
7. [ ] **Addon:**
   - **Base written and tested against simulated hubs** (16 Nov), not yet against HA:
     - `PYTHONPATH=addon python -m oeplhub.server --data DIR --hub HOST[:PORT]@CH ... --tagtypes tagtypes --port 8080`. Needs
       `requirements.txt` (aiohttp, Pillow).
     - `oeplhub/core.py`: the AP logic, after `newproto.cpp`. Per-tag queue (a new plain image replaces queued
       images), announce the head to the tag's hub (or to all hubs if unknown or its hub is down), preload into the
       radio cache, `DATA_NEEDED`, `XFER_COMPLETE` (hash = MD5[:8], `/current/<MAC>.raw`), `XFER_TIMEOUT`, commands
       (`0xAF`: reboot, scan, reset, deepsleep, LED), lock modes, and roaming (`CANCEL_PENDING` on the old hub,
       announce on the new one).
     - `oeplhub/server.py`: `/`, `/ws`, `/get_db` (paged, `continu`), `/sysinfo`, `/get_ap_config`, `/save_apcfg`,
       `/save_cfg`, `/tag_cmd`, `/led_flash`, `/imgupload`, `/reboot`, `/current/*`, `/tagtypes/*`, plus `/api/hubs`
       (ours). `sys` is sent every 5 s.
     - `oeplhub/tagdb.py` (tag records, the same fields as `fillNode()`, `data/tagDB.json`),
       `oeplhub/tagtypes.py` (local `tagtypes/` first, then upstream GitHub, cached).
     - `tagtypes/94.json` is kept here until upstream PR #638 is merged.
     - `tests/e2e_fake.sh`: two simulated hubs, a command plus an upload, then roaming and a second upload.
       Everything passes.
     - **Tested with the real HA integration** (16 Nov), with the addon on the dev PC and a simulated hub holding two
       fake tags. HA's config flow, `get_db`, the websocket and `drawcustom` → `/imgupload` → encode → preload →
       `xfer complete` all worked, and the decoded image matched. The stock AP's entry had to be disabled (one AP
       only, see below).
     - **Type 0x94 can't be drawn from HA yet:** the integration only gets tag types from upstream GitHub `master`
       (`tag_types.py`), and `94.json` waits for PR #638. Until then HA reports "Unexpected error getting tag type
       … 148". The test used `--hw 0x41`.
     - **Pending items survive a restart** (16 Nov): `data/pending.json` plus `data/pending/<ver>.bin`, saved on
       every queue change, restored at start, re-announced when each hub comes up. Tested with a restart between
       upload and check-in.
     - Not yet: content modes other than uploads,
       `/jsonupload`, firmware OTA.
   - tag DB, kept in `/data`
   - encoding from the official tagtype JSONs
   - HTTP and websocket API compatible with the stock AP, so the official OEPL HA integration works against it
   - **Own web UI, styled like Home Assistant (not a copy of the stock `wwwroot/`):**
     - an ingress panel, so the hub shows up as an item in HA's left sidebar
     - the stock UI's sections (tags, AP/hub settings, logs, …) become HA-style tabs at the top of the panel
     - follows the HA theme (light/dark, colours) where the ingress iframe allows it
     - shows the hubs too: link state, channel, stats
     - **First version written** (16 Nov, addon 0.2.0), checked locally in headless Chromium (light and dark), not
       yet seen inside HA. `addon/www/index.html`, plain HTML/JS, no build step, relative URLs (ingress). Tabs: Tags
       (cards with preview, hub, last seen, next check-in, battery, signal, transfer progress; rename, refresh, LED,
       clear, reboot, scan, delete), Hubs (`/api/hubs`, every 5 s), Settings (part of `save_apcfg`), Log
       (websocket). Theme: copies HA's CSS variables from the parent page (same origin in ingress), else
       `prefers-color-scheme`. Previews: `/api/preview/<MAC>` renders `current/<MAC>.raw` to PNG with
       `imageenc.decode()` (round trip tested for all buffer rotations). Tag records now carry `hub` in
       `to_json()` too (extra key for the stock API).
     - The hub installer stays in `web/` (GitHub Pages); it was briefly moved into `addon/` by mistake.
   - **HA addon packaging done** (16 Nov). The repo is an addon repository (`repository.yaml`; HACS can't install
     addons). The addon lives in `addon/` (code moved there, since HA builds from the addon folder): `config.yaml`
     (slug `oepl_multihub`, options `hubs: [{spec, channel}]` and `verbose`, ingress on 8080, port 80 on the host for
     the integration, `uart`/`usb` for USB hubs), `build.yaml` (HA Alpine base 3.20), `Dockerfile` (aiohttp and
     Pillow from Alpine packages), `run.sh` (`--config /data/options.json`, data in `/data`). Run locally with
     `PYTHONPATH=addon python -m oeplhub.server …`.
   - **Works end to end from the installed addon** (16 Nov): builds and installs on the user's HA, ingress panel shows
     the placeholder page. With a simulated hub on the dev PC (`192.168.0.3:16650`, two fake 0x41 tags) in the
     options and the integration added with the HA host as AP address: `drawcustom` → pending → transfer complete.
8. [ ] **Easy install for other users** (after step 4):
   - Publish two hub variants: **Wi-Fi** (the normal case) and **USB** (a hub plugged into the HA machine; no Wi-Fi,
     so no coexistence issues). The UART variant is the stock build for S3-based APs and stays only as the upstream
     default.
   - GitHub Actions builds the hub firmware and attaches a merged `.bin` (flashed at 0x0) to each release.
   - A GitHub Pages site with **ESP Web Tools**: an "Install" button that flashes over USB from Chrome or Edge.
   - **Improv Serial** in the hub firmware, so the install page can set the Wi-Fi details right after flashing,
     with no Wi-Fi details compiled in.
   - **Done and live** (16 Nov), not yet tested with a board: **https://scuttlese.github.io/oepl-multihub/**
     - `.github/workflows/firmware.yml` builds the fork at the commit in `firmware/upstream-ref` with
       `firmware/build.sh` (`espressif/esp-idf-ci-action`, IDF v5.5.5), merges `hub-wifi.bin` and `hub-usb.bin`
       (flash size fixed to 4 MB), writes the ESP Web Tools manifests, and deploys `web/` to Pages. It runs on
       pushes to `firmware/`, `web/` or the workflow, or by hand. **After changing the fork, update
       `firmware/upstream-ref`.**
     - The manifests set `new_install_prompt_erase`. Erasing is recommended, because the Wi-Fi build has its own
       partition table.
     - Improv Serial (`main/improv.c` in the fork, `OEPL_IMPROV_SERIAL`, on by default in the Wi-Fi build) on the
       native USB port: state, device info, Wi-Fi scan, and Wi-Fi settings (stored in NVS by the driver, 20 s to
       get an IP). In the Wi-Fi build the debug log is on UART0 only (TX/RX pins).
9. [ ] **Later:** tag OTA (same block mechanism), C6 radio OTA over Wi-Fi, content modes, an upstream PR for the Wi-Fi
   transport.

### Findings from the source (16 Nov 2026)

- **Host link pins:** in stock firmware the host link is UART1 on GPIO, not the native USB. The default profile uses
  TX GPIO3 and RX GPIO2 (`second_uart.h`). The native USB is only the debug console. So "C6 on USB with stock firmware"
  needs a USB-UART adapter that can do 2 Mbaud, wired to GPIO2 and GPIO3.
- **Host loss:** with no frames from the host for 5000 ms (`V2_LINK_LOST_MS` in `main.c`), the radio calls `v2Stop()`
  and goes back to ASCII at 115200. Queued events are dropped, because the host re-sends everything after it negotiates
  again. Pending data and the block cache stay. A new host must do `RDY?`/`NFO?`/`FRM1` again.
- **Second `HELLO`** (while still in v2): it is answered with `HELLO_REPLY` and is never treated as a duplicate.
  It does not reset the radio's seq or its event queue. A clean reconnect is: go silent for more than 5 s, then do ASCII
  startup again.
- **Events:** the radio sends one event at a time (stop-and-wait) and waits 30 ms for the ACK, with 3 retries. After
  that it marks the link down and sends again when the next host frame arrives. Nothing is lost, so a slow ACK over Wi-Fi
  only costs time. The host must ACK every event except `LOG`, and must drop a repeated seq after ACKing it.
- **The S3** treats 3 missed heartbeats as a lost link.

## Home Assistant integration: the API it uses

Checked 16 Nov 2026 against https://github.com/OpenEPaperLink/Home_Assistant_Integration (master), Wi-Fi AP mode only.
The BLE parts don't touch the AP. The addon must serve these on port 80, with the same paths and formats as the
stock AP:

| Endpoint | Used for |
|---|---|
| `GET /` | config flow: checks that something answers |
| `GET /sysinfo` | AP info (reads `env`, among others) |
| `GET /get_ap_config` | AP settings |
| `GET /get_db?pos=N` | tag list, paged with a continuation value |
| `GET /current/<MAC>.raw` | the tag's current image (the integration decodes raw and G5 itself) |
| `POST /imgupload` | multipart: `mac`, `contentmode=25`, `dither`, `ttl` (min), `lut`, `image` (**JPEG**) |
| `POST /tag_cmd` | `mac`, `cmd` = `clear` / `refresh` / `reboot` / `scan` |
| `GET /led_flash?mac=&pattern=` | tag LED |
| `POST /save_cfg` | tag alias (`mac`, `alias`) |
| `POST /save_apcfg` | AP settings |
| `POST /reboot` | AP reboot |
| `ws://host/ws` | JSON messages `sys`, `tags` (one tag record), `logMsg`, `errMsg`, `apitem` |

Notes:
- The image arrives as a JPEG, and the AP does the dithering and encoding. So the addon needs JPEG decoding and
  dithering that match the stock AP (`makeimage.cpp`). Pillow is the likely dependency.
- `logMsg` text is parsed: the integration looks for `block request` and `reports xfer complete`. Use the same wording.
- `sys` fields read: `apstate`, `runstate`, `currtime`, `uptime`, `heap`, `recordcount`, `dbsize`, `littlefsfree`,
  `psfree`, `rssi`, `wifissid`, `wifistatus`, `temp`, `lowbattcount`, `timeoutcount`, `mac`.
- Tag fields read: `mac`, `alias`, `hwType`, `ver`, `RSSI`, `LQI`, `ch`, `batteryMv`, `temperature`, `lastseen`,
  `nextupdate`, `nextcheckin`, `pending`, `hash`, `contentMode`, `modecfgjson`, `rotate`, `lut`, `wakeupReason`,
  `capabilities`, `isexternal`, `updatecount`, `boot_count`, `checkin_count`, `block_requests`, `runtime`.
- All hubs appear to HA as **one AP**, so HA doesn't need to know there are several hubs.
- **The integration supports only one AP:** every action goes to the first non-BLE config entry
  (`get_hub_from_hass` in `util.py`). So the addon must replace the stock AP's entry rather than run beside it. To
  test next to a stock AP, disable the stock AP's entry first.
- **Real samples** from the running stock AP (`192.168.50.205`, S3 + C6, fw 3.0, 6 tags) are in `docs/ap-samples/`:
  `sysinfo`, `get_ap_config`, `get_db?pos=0` and 70 s of websocket traffic (SSID redacted).
  - `sys` is sent every ~5 s. `lowbattcount` and `timeoutcount` are only in some of them.
  - `tags` is sent on every check-in with the full tag record (the same fields as `get_db`, plus `apip`,
    `invert`, `updatelast`, `imgtype`).
  - `get_db` returned only `{"tags": [...]}` for 6 tags, so there is no continuation key with a small DB.
    Check `web.cpp` for paging.
- The current AP runs on **802.15.4 channel 11**, the same as Zigbee. Expect the new hubs on 25 and 26 to do better.

## Open questions

- Wi-Fi build: should a TCP disconnect reset the radio to ASCII at once, instead of after 5 s? Not needed so far.
- Is the 30 ms ACK timeout OK over Wi-Fi, or does the TCP build need a longer one?
- How do tags rank multiple APs during a scan?
- Exact field formats in the `sys`, `tags` and `get_db` JSON: copy them from `web.cpp` and `tag_db.cpp`.
