#!/usr/bin/env bash
# Builds both hub variants in the C6 AP project directory (the current directory) and merges each into
# one image for offset 0. Run inside an ESP-IDF v5.5 environment. Extra defaults (like sdkconfig.ci)
# are used when present.
set -euo pipefail
extra=""
[ -f sdkconfig.ci ] && extra=";sdkconfig.ci"
idf.py -B build-wifi -D SDKCONFIG=sdkconfig.wifi -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.host-wifi$extra" build
idf.py -B build-usb -D SDKCONFIG=sdkconfig.usb -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.host-usb" build
for v in wifi usb; do
    # merge_bin needs a real flash size, not "detect"
    sed 's/--flash_size detect/--flash_size 4MB/' "build-$v/flash_args" > "build-$v/merge_args"
    (cd "build-$v" && python -m esptool --chip esp32c6 merge_bin -o "../hub-$v.bin" @merge_args)
done
ls -l hub-wifi.bin hub-usb.bin
