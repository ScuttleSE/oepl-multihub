# Changelog

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
