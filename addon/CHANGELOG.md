# Changelog

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
