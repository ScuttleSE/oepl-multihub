#!/usr/bin/env bash
# End-to-end test against simulated hubs: two hubs, an upload from "HA", a command, then tag 1 roams
# from hub1 to hub2 and gets a second image there. Prints the server's core log and the hubs' logs.
set -u
cd "$(dirname "$0")/.."
T=${TMPDIR:-/tmp}/oepl-e2e
rm -rf "$T"; mkdir -p "$T"
PORT=18080
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

python3 tools/hubtool.py fakehub --port 16670 --interval 3 > "$T/hub1.log" 2>&1 & H1=$!; pids+=($H1)
python3 tools/hubtool.py fakehub --port 16671 --interval 3 --tag 0000021EEA023B12 > "$T/hub2.log" 2>&1 & H2=$!; pids+=($H2)
sleep 0.5
PYTHONPATH=addon python3 -m oeplhub.server --data "$T/data" --hub 127.0.0.1:16670@26 --hub 127.0.0.1:16671@25 \
    --tagtypes addon/tagtypes --port $PORT > "$T/server.log" 2>&1 & pids+=($!)
sleep 5

python3 -c "from PIL import Image, ImageDraw
im = Image.new('RGB', (792, 272), 'white'); d = ImageDraw.Draw(im)
d.rectangle([20, 20, 300, 250], fill='black'); d.rectangle([320, 20, 540, 250], fill='red')
d.rectangle([560, 20, 770, 250], fill='yellow'); im.save('$T/up.jpg', quality=90)"

echo "--- reboot command and upload on hub1"
curl -s -X POST localhost:$PORT/tag_cmd -d mac=8C73DAFFFE5D531B -d cmd=reboot; echo
curl -s -F mac=8C73DAFFFE5D531B -F contentmode=25 -F dither=2 -F ttl=1 -F image=@"$T/up.jpg" localhost:$PORT/imgupload; echo
sleep 9

echo "--- hub1 goes away, tag 1 shows up on hub2"
kill $H1 $H2; wait $H1 $H2 2>/dev/null
python3 tools/hubtool.py fakehub --port 16671 --interval 3 --tag 0000021EEA023B12 --tag 8C73DAFFFE5D531B \
    > "$T/hub2b.log" 2>&1 & pids+=($!)
sleep 8
curl -s -F mac=8C73DAFFFE5D531B -F contentmode=25 -F dither=1 -F ttl=1 -F image=@"$T/up.jpg" localhost:$PORT/imgupload; echo
sleep 8

echo "--- server"
grep " core " "$T/server.log" | grep -v "connect failed" | cut -c12-
echo "--- hubs"
grep -hE "pending for|complete|command" "$T/hub1.log" "$T/hub2b.log"
echo "--- tag 1"
curl -s "localhost:$PORT/get_db?mac=8C73DAFFFE5D531B" | python3 -c "import json,sys
t = json.load(sys.stdin)['tags'][0]; print({k: t[k] for k in ('hash', 'pending', 'updatecount', 'imgtype', 'ch')})"
