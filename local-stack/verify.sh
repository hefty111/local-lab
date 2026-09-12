#!/usr/bin/env bash
# End-to-end verification of the local-setup compose stack.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.yml")
BASE_URL="${BASE_URL:-http://localhost:4000}"
MASTER_KEY="${LITELLM_MASTER_KEY:-sk-1234}"
FAILS=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILS=$((FAILS+1)); }
json_get() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval("d"+sys.argv[1]))' "$1"; }

echo "== 1. Migration =="
MIGRATE_ID=$("${COMPOSE[@]}" ps -a -q litellm-migrate)
EXIT_CODE=$(docker inspect -f '{{.State.ExitCode}}' "$MIGRATE_ID")
[ "$EXIT_CODE" = "0" ] && pass "litellm-migrate exited 0" || fail "litellm-migrate exit code $EXIT_CODE"

TABLES=$("${COMPOSE[@]}" exec -T postgres psql -U llmproxy -d litellm -tAc \
  "select count(*) from information_schema.tables where table_name='LiteLLM_VerificationToken'")
[ "$TABLES" = "1" ] && pass "LiteLLM_VerificationToken table exists" || fail "LiteLLM_VerificationToken table missing"

echo "== 2. Health =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/health/liveliness")
[ "$CODE" = "200" ] && pass "/health/liveliness 200" || fail "/health/liveliness $CODE"

READY=$(curl -s -w '\n%{http_code}' "$BASE_URL/health/readiness")
READY_CODE=$(echo "$READY" | tail -n1); READY_BODY=$(echo "$READY" | head -n -1)
if [ "$READY_CODE" = "200" ] && echo "$READY_BODY" | grep -q '"db": *"connected"'; then
  pass "/health/readiness 200 with db connected"
else
  fail "/health/readiness code=$READY_CODE body=$READY_BODY"
fi

echo "== 3. DB round-trip (key generate + use) =="
KEY_RESP=$(curl -s -X POST "$BASE_URL/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"models":["fake-gpt-4"],"key_alias":"verify-'"$(date +%s)"'"}')
VKEY=$(echo "$KEY_RESP" | json_get '["key"]' 2>/dev/null || true)
[[ "$VKEY" == sk-* ]] && pass "/key/generate returned key" || fail "/key/generate: $KEY_RESP"

MODELS=$(curl -s -w '\n%{http_code}' "$BASE_URL/models" -H "Authorization: Bearer $VKEY")
MODELS_CODE=$(echo "$MODELS" | tail -n1); MODELS_BODY=$(echo "$MODELS" | head -n -1)
if [ "$MODELS_CODE" = "200" ] && echo "$MODELS_BODY" | grep -q '"fake-gpt-4"'; then
  pass "/models with virtual key lists fake-gpt-4"
else
  fail "/models code=$MODELS_CODE body=$MODELS_BODY"
fi

echo "== 4. Chat completions via mock upstream =="
CHAT=$(curl -s -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $VKEY" -H 'Content-Type: application/json' \
  -d '{"model":"fake-gpt-4","messages":[{"role":"user","content":"ping"}]}')
CONTENT=$(echo "$CHAT" | json_get '["choices"][0]["message"]["content"]' 2>/dev/null || true)
[ -n "$CONTENT" ] && pass "non-streaming completion: $CONTENT" || fail "completion: $CHAT"

STREAM=$(curl -s -N -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $VKEY" -H 'Content-Type: application/json' \
  -d '{"model":"fake-gpt-4","stream":true,"messages":[{"role":"user","content":"ping"}]}')
echo "$STREAM" | grep -q '\[DONE\]' && pass "streaming completion ends with [DONE]" || fail "streaming: $STREAM"

echo "== 5. Traffic goes through pgbouncer =="
POOLS=$("${COMPOSE[@]}" exec -T -e PGPASSWORD=dbpassword9090 postgres \
  psql -h pgbouncer -p 5432 -U llmproxy -d pgbouncer -tAc "SHOW POOLS" 2>&1)
echo "$POOLS"
# columns: database|user|cl_active|cl_waiting|... ; sum cl_active for db 'litellm'
CL_ACTIVE=$(echo "$POOLS" | awk -F'|' '$1=="litellm"{s+=$3} END{print s+0}')
[ "${CL_ACTIVE:-0}" -gt 0 ] && pass "pgbouncer SHOW POOLS: $CL_ACTIVE active client conns to litellm" \
  || fail "pgbouncer SHOW POOLS shows no active client connections"

PGB_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$("${COMPOSE[@]}" ps -q pgbouncer)")
DIRECT=$("${COMPOSE[@]}" exec -T postgres psql -U llmproxy -d litellm -tAc \
  "select count(*) from pg_stat_activity where datname='litellm' and usename='llmproxy' and client_addr is not null and client_addr <> '$PGB_IP'::inet and application_name <> 'psql'")
[ "$DIRECT" = "0" ] && pass "all postgres clients originate from pgbouncer ($PGB_IP)" \
  || fail "$DIRECT postgres client(s) bypass pgbouncer"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
