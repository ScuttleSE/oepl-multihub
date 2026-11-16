#!/bin/sh
set -e
cd /app
args=""
if [ "$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("verbose", False))')" = "True" ]; then
    args="-v"
fi
exec python3 -m oeplhub.server --data /data --config /data/options.json --tagtypes /app/tagtypes --port 8080 $args
