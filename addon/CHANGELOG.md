# Changelog

## 0.10.1

- The tag settings dialog keeps its width: a long price sensor name no longer makes it wider. The QR code
  fields line up with the other fields.

## 0.10.0

- New content type "QR code": choose a type (website, Wi-Fi login, contact card, phone call, SMS, email,
  location, calendar event or plain text) and fill in its fields. The code is drawn once and sent to the tag,
  with an optional caption. On wide tags, the text goes beside the code.

## 0.9.0

- The electricity price chart now uses the stock access point's "Dayahead prices" layout: time and price at the
  top, bars coloured by price level (cheapest 40 % black, then grey, pink, and the top 10 % red), a "now" arrow,
  gridlines with labels, hour labels, and the cheapest 3-hour block per day shaded under the time axis. Same
  fonts and per-tag-type layout as the stock access point. Prices are shown in öre (or cents) per kWh.

## 0.8.2

- Fixed "Home Assistant not reachable": the addon now finds its Home Assistant access token.

## 0.8.1

- The price sensor is now a dropdown with the Nordpool sensors found in Home Assistant. Saving without a sensor
  is no longer possible.
- A price tag without a sensor uses the Nordpool sensor automatically if there is exactly one.
- The addon version shows correctly in the integration (it said "0.1" or "dev").

## 0.8.0

- **Electricity prices** as tag content: in a tag's Settings, set Content to "Electricity prices", enter your
  Nordpool sensor (for example `sensor.nordpool_kwh_se4_sek_3_10_0`) and how often to redraw (default 15 min).
  The tag shows the current price, a bar chart of the next 24 hours (expensive periods in red or yellow, today and
  tomorrow once published) and the cheapest coming hour. An unchanged chart is not sent again.
- The addon can now read entity states from Home Assistant (`homeassistant_api`). It only reads.
- Text on generated images uses DejaVu Sans, so å, ä and ö work.

## 0.7.2

- Click a tag's preview to see it at full size, pixel for pixel as on the tag.

## 0.7.1

- The test image shows the tag's name, or its address if it has no name.

## 0.7.0

- New **Test image** button on each tag: draws a test pattern made for that tag (its size, rotation and
  colours), with corner labels, one bar per colour and fine line patterns. Also available as
  `POST /api/test_image` with `mac`.
- An image that is the same as the one already waiting for the tag is no longer queued and sent again. (An image
  the same as the one on the tag was already skipped.)
- The addon reports its own version to the integration (`/sysinfo`).

## 0.6.0

- Works with real ESP32-C6 hubs over Wi-Fi (tested with two hubs and real tags). Use hub firmware
  2026.11.18 or newer from the installer.
- Wi-Fi hubs get longer timeouts (1 s instead of 30 ms), so slow Wi-Fi no longer causes reconnects. USB hubs keep
  the short timeouts.
- Fixed a crash when a hub's connection closed at the wrong moment.
- The hub status no longer counts a timeout on every connect.

## 0.5.1

- The options screen now has proper names and explanations: Hubs, Address, Channel, Debug logging.
- A hub's channel is optional. Without one, the addon picks a free channel (26, 25, 20, 15, 11 in that order), so
  no two hubs share one.

## 0.5.0

- Upload image now opens a picker for Home Assistant's media folder (Media › My media), with thumbnails and folders.
  Images are scaled and cropped to the tag on the addon. Choose the dithering in the picker. "From this
  computer…" still uploads a local file.
- The addon now has read-only access to `/media`.

## 0.4.0

- Backup tab: download and restore the tag database. Backups from a stock access point (`tagDB.json`) work too.
- Import from a running stock access point: tags, names, settings and current images.
- Variables for JSON templates (`/set_var`, `/set_vars`). Tags that use a variable are redrawn when it changes.
- Upload an image to a tag from the browser. It is scaled and cropped to the tag's size.
- Alerts above the tag list: low battery, overdue tags and rejected firmware updates.

## 0.3.0

- Tag settings dialog in the panel: name, rotation, refresh mode (LUT) and invert colours.
- Invert colours now applies to images.
- Tag firmware updates: install the latest firmware for the tag type from the OpenEPaperLink repository, or upload
  a `.bin` file.
- `/jsonupload`: draw a tag from a JSON list of elements (text, boxes, lines, circles, …), like the stock access
  point.
- Quiet hours and time zone: tags sleep through the quiet hours and update afterwards.
- Tags with nothing to do are told to sleep up to "maximum sleep" minutes between check-ins, like the stock access
  point.

## 0.2.0

- Web UI in the sidebar panel, styled like Home Assistant: tags with image previews, hubs, settings and log.
- The sidebar panel is now called "OEPL Multihub".

## 0.1.1

- Queued images and commands are kept across addon restarts.

## 0.1.0

- First version of the addon.
