#!/usr/bin/env bash
# mp3api-service 契约冒烟：/healthz -> /sources -> /search -> /info -> /stream (Range)
# 用法: ./smoke_test.sh [BASE_URL] [KEYWORD]
#   BASE_URL 默认 http://127.0.0.1:8776
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://127.0.0.1:8776}}"
KEYWORD="${2:-Chopin Nocturne}"
PY="${PYTHON:-python3}"

echo "== 1) /healthz =="
curl -sS --fail "${BASE_URL}/healthz"; echo

echo "== 2) /sources =="
curl -sS --fail "${BASE_URL}/sources"; echo

echo "== 3) /search =="
SEARCH_JSON="$(curl -sS --fail -G --data-urlencode "keyword=${KEYWORD}" --data-urlencode "limit=3" "${BASE_URL}/search")"
echo "${SEARCH_JSON}" | head -c 600; echo
FIRST_ID="$(printf '%s' "${SEARCH_JSON}" | "${PY}" -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"], d; assert d["items"], "empty search result"; print(d["items"][0]["id"])')"
echo "first id = ${FIRST_ID}"

echo "== 4) /info =="
curl -sS --fail -G --data-urlencode "id=${FIRST_ID}" "${BASE_URL}/info" | head -c 500; echo

echo "== 5) /stream (proxy=true + Range) =="
curl -sS --fail -D /tmp/mp3api_stream_headers.txt -o /tmp/mp3api_stream.bin \
  -H "Range: bytes=0-1023" -G \
  --data-urlencode "id=${FIRST_ID}" --data-urlencode "proxy=true" \
  "${BASE_URL}/stream"
head -n 12 /tmp/mp3api_stream_headers.txt
echo "downloaded bytes = $(wc -c < /tmp/mp3api_stream.bin)"
grep -qi "206\|200" /tmp/mp3api_stream_headers.txt || { echo "stream status missing"; exit 1; }
grep -qi "Content-Range: bytes" /tmp/mp3api_stream_headers.txt || { echo "Range not honoured"; exit 1; }
rm -f /tmp/mp3api_stream_headers.txt /tmp/mp3api_stream.bin

echo "== OK: mp3api-service 满足 musicdl 音源契约 =="
