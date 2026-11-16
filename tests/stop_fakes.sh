#!/usr/bin/env bash
# Stop simulated hubs and test servers started from this repo
for p in $(pgrep -f "python3 (tools/hubtool.py fakehub|-m oeplhub.server)"); do
    kill "$p" 2>/dev/null
done
exit 0
