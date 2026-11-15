#!/usr/bin/env bash
# Local validation for free-model-aggregator-02.
# Runs the server in MOCK_OPENROUTER=1 mode (no network, no real key):
# exercises both API wires in JSON + streaming, proves failover with a
# simulated 429, checks determinism, and proves that WITHOUT a live key
# the server refuses to fabricate an answer (503) instead of scoring.
# Usage: ./test.sh
# Live check (needs a real key, not run here):
#   OPENROUTER_API_KEY=sk-or-... PORT=8080 python3 main.py
set -u
cd "$(dirname "$0")"

export PORT=18083
export MOCK_OPENROUTER=1
BASE="http://127.0.0.1:$PORT"
PASS=0; FAIL=0
ok()   { echo "PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $1"; FAIL=$((FAIL+1)); }
check_json() { # $1 = file, $2 = python expr on data, $3 = label
  if python3 -c "import json,sys; d=json.load(open('$1')); assert $2, 'check failed'" 2>/dev/null; then ok "$3"; else bad "$3"; fi
}

python3 main.py & SERVER_PID=$!
# wait for health
for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "$BASE/health" 2>/dev/null && break
  sleep 0.2
done

echo "--- GET /health";      curl -sS "$BASE/health"; echo
echo "--- GET /v1/models";   curl -sS "$BASE/v1/models"; echo
echo "--- GET /catalog";     curl -sS "$BASE/catalog" | head -c 600; echo

echo "--- POST /v1/chat/completions (JSON)"
curl -sS "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hello in one sentence."}]}' \
  -o /tmp/agg02_chat.json -D /tmp/agg02_chat.hdr
cat /tmp/agg02_chat.json; echo
check_json /tmp/agg02_chat.json "d['choices'][0]['message']['content'] and d['model']=='deepseek/deepseek-r1:free'" \
  "OpenAI JSON routes to best free model (deepseek-r1:free, quality 88)"
grep -q "X-Aggregator-Model: deepseek/deepseek-r1:free" /tmp/agg02_chat.hdr && ok "X-Aggregator-Model header present" || bad "X-Aggregator-Model header present"

echo "--- POST /v1/chat/completions (streaming SSE)"
curl -sS -N "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Count to three."}],"stream":true}' \
  -o /tmp/agg02_stream.txt
cat /tmp/agg02_stream.txt
grep -q "data: \[DONE\]" /tmp/agg02_stream.txt && ok "streaming ends with [DONE]" || bad "streaming ends with [DONE]"

echo "--- POST /v1/chat/completions (tools + JSON mode)"
curl -sS "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"What is the weather?"}],"tools":[{"type":"function","function":{"name":"get_weather","description":"weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}],"response_format":{"type":"json_object"}}' \
  -o /tmp/agg02_tools.json
cat /tmp/agg02_tools.json; echo
check_json /tmp/agg02_tools.json "d['choices'][0]['message'].get('tool_calls')" \
  "tools request returns a tool_call (no fake completion)"

echo "--- POST /v1/messages (Anthropic wire)"
curl -sS "$BASE/v1/messages" -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"auto","max_tokens":256,"messages":[{"role":"user","content":"Say hello in one sentence."}]}' \
  -o /tmp/agg02_anthropic.json
cat /tmp/agg02_anthropic.json; echo
check_json /tmp/agg02_anthropic.json "d['type']=='message' and d['content'][0]['text']" \
  "Anthropic wire answers in Anthropic format"

echo "--- POST /v1/messages (Anthropic streaming)"
curl -sS -N "$BASE/v1/messages" -H 'Content-Type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"auto","max_tokens":256,"stream":true,"messages":[{"role":"user","content":"Say hi."}]}' \
  -o /tmp/agg02_anthropic_stream.txt
cat /tmp/agg02_anthropic_stream.txt
grep -q "message_stop" /tmp/agg02_anthropic_stream.txt && ok "Anthropic streaming ends with message_stop" || bad "Anthropic streaming ends with message_stop"

echo "--- error case: impossible context (no fake completion)"
curl -sS "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"x"}],"max_tokens":2000000}' \
  -o /tmp/agg02_err.json
cat /tmp/agg02_err.json; echo
check_json /tmp/agg02_err.json "'error' in d" "oversized request returns an error, not a completion"

echo "--- determinism: same request twice"
curl -sS "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hello in one sentence."}]}' \
  -o /tmp/agg02_run2.json
python3 - <<'PY'
import json
a=json.load(open('/tmp/agg02_chat.json')); b=json.load(open('/tmp/agg02_run2.json'))
same = (a['choices']==b['choices'] and a['model']==b['model'])
print("PASS: deterministic repeat (same model + content)" if same else "FAIL: deterministic repeat")
raise SystemExit(0 if same else 1)
PY
[ $? -eq 0 ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))

kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null

# --- failover: first mock attempt 429s once, then the next free model serves ---
echo "--- failover test (simulated 429, Retry-After 1s)"
PORT=18084 MOCK_OPENROUTER=1 OPENROUTER_MOCK_FAIL_FIRST=1 python3 main.py & SERVER_PID=$!
for _ in $(seq 1 30); do curl -fsS -o /dev/null "http://127.0.0.1:18084/health" 2>/dev/null && break; sleep 0.2; done
START=$(date +%s)
curl -sS "http://127.0.0.1:18084/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Failover check."}]}' \
  -o /tmp/agg02_failover.json -D /tmp/agg02_failover.hdr
ELAPSED=$(( $(date +%s) - START ))
cat /tmp/agg02_failover.json; echo
check_json /tmp/agg02_failover.json "d['model']=='meta-llama/llama-3.3-70b-instruct:free'" \
  "failover: 429 on best model -> served by next model (llama-3.3-70b:free)"
grep -q "X-Aggregator-Model: meta-llama/llama-3.3-70b-instruct:free" /tmp/agg02_failover.hdr && ok "failover header names serving model" || bad "failover header names serving model"
[ "$ELAPSED" -ge 1 ] && ok "Retry-After honoured (took ${ELAPSED}s >= 1s)" || bad "Retry-After honoured (took ${ELAPSED}s)"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null

# --- no-key behaviour: live mode without OPENROUTER_API_KEY must refuse ---
echo "--- no-key test (live mode, no OPENROUTER_API_KEY)"
env -u MOCK_OPENROUTER -u OPENROUTER_API_KEY PORT=18085 python3 main.py & SERVER_PID=$!
for _ in $(seq 1 30); do curl -fsS -o /dev/null "http://127.0.0.1:18085/health" 2>/dev/null && break; sleep 0.2; done
curl -sS -w "\nHTTP %{http_code}\n" "http://127.0.0.1:18085/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello."}]}' \
  -o /tmp/agg02_nokey.json
cat /tmp/agg02_nokey.json; echo
grep -q "OPENROUTER_API_KEY" /tmp/agg02_nokey.json && ok "no key -> honest 503 naming OPENROUTER_API_KEY" || bad "no key -> honest 503 naming OPENROUTER_API_KEY"
check_json /tmp/agg02_nokey.json "'error' in d" "no key -> error, not a fabricated completion"
kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null

echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
