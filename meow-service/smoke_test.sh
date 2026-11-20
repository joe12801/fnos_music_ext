#!/usr/bin/env bash
# 对运行中的 meow-service 做真实冒烟：healthz -> search -> info -> stream(头几个字节)
set -euo pipefail
BASE="${1:-http://127.0.0.1:8778}"
KEYWORD="${2:-晴天}"

echo "== healthz =="
curl -fsS "$BASE/healthz"; echo

echo "== sources =="
curl -fsS "$BASE/sources"; echo

echo "== search: $KEYWORD =="
SEARCH_JSON=$(curl -fsS --get "$BASE/search" --data-urlencode "keyword=$KEYWORD" --data "limit=3")
echo "$SEARCH_JSON"

ID=$(python3 -c "import sys,json; d=json.load(sys.stdin); print(d['items'][0]['id'] if d.get('items') else '')" <<<"$SEARCH_JSON")
if [ -z "$ID" ]; then
  echo "no search result; skip info/stream"
  exit 0
fi

echo "== info: $ID =="
curl -fsS --get "$BASE/info" --data-urlencode "id=$ID"; echo

echo "== stream (first 1KB, expect audio bytes) =="
curl -fsS --get "$BASE/stream" --data-urlencode "id=$ID" --data "proxy=true" -r 0-1023 -o /tmp/meow_smoke.bin -w "http=%{http_code} bytes=%{size_download} type=%{content_type}\n"
file /tmp/meow_smoke.bin 2>/dev/null || true
echo "SMOKE_OK"
