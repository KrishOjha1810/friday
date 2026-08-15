#!/usr/bin/env bash
# End to end: a real Friday server with a fake world, driven by a real browser.
#
# Starts the server, waits for it to say READY, runs the browser suite against
# it, and always tears it down, including when the suite fails.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
log="$(mktemp -t friday-e2e)"
cd "$root"

if [ ! -d node_modules/playwright ]; then
  echo "  playwright is not installed. From $root:"
  echo "     npm install playwright && npx playwright install chromium"
  exit 2
fi

PYTHONPATH="$root" python3 "$here/e2e_server.py" > "$log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null; rm -f "$log"' EXIT

url=""
for _ in $(seq 1 40); do
  url="$(grep -m1 READY "$log" 2>/dev/null | awk '{print $2}')"
  [ -n "$url" ] && break
  kill -0 $server 2>/dev/null || { echo "  server died before it was ready:"; cat "$log"; exit 1; }
  sleep 0.5
done

if [ -z "$url" ]; then
  echo "  server never became ready:"; cat "$log"; exit 1
fi

node "$here/e2e_browser.mjs" "$url"
