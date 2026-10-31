#!/usr/bin/env bash
# End-to-end verification: fake upstream + gateway on SQLite, no real API spend.
# Asserts that a proxied request is recorded with correct tokens and cost,
# that a bad key is rejected, and that /metrics + /usage expose the numbers.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -d .venv ] || { echo "run: uv venv && uv pip install -r requirements.txt"; exit 1; }
source .venv/bin/activate

rm -f verify.db
export UPSTREAM_BASE_URL=http://127.0.0.1:9911/v1
export GATEWAY_API_KEYS=testkey
export MODEL_PRICES='{"fake-model":{"in":0.5,"out":1.5}}'
export DATABASE_URL=sqlite:///./verify.db

uvicorn tests.fake_upstream:app --host 127.0.0.1 --port 9911 --log-level warning &
UP=$!
uvicorn app.main:app --host 127.0.0.1 --port 9912 --log-level warning &
GW=$!
cleanup() { kill $UP $GW 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:9912/healthz >/dev/null && break
  sleep 0.25
done

echo "--- 1. authorized request ---"
curl -s -o /tmp/gw_body.json -w "status=%{http_code}\n" \
  -X POST http://127.0.0.1:9912/v1/chat/completions \
  -H 'Authorization: Bearer testkey' -H 'Content-Type: application/json' \
  -d '{"model":"fake-model","messages":[{"role":"user","content":"ping"}]}'

echo "--- 2. unauthorized request ---"
curl -s -o /dev/null -w "status=%{http_code}\n" \
  -X POST http://127.0.0.1:9912/v1/chat/completions \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' \
  -d '{"model":"fake-model","messages":[]}'

echo "--- 3. /metrics ---"
curl -s http://127.0.0.1:9912/metrics | grep -E 'gateway_(requests_total|prompt_tokens_total|completion_tokens_total|cost_usd_total)'

echo "--- 4. /usage ---"
curl -s http://127.0.0.1:9912/usage

echo "--- 5. streaming relay ---"
curl -s -N -X POST http://127.0.0.1:9912/v1/chat/completions \
  -H 'Authorization: Bearer testkey' -H 'Content-Type: application/json' \
  -d '{"model":"fake-model","stream":true,"messages":[{"role":"user","content":"ping"}]}' \
  | head -4

python3 - <<'PY'
import json, sqlite3, sys, urllib.request

usage = json.load(urllib.request.urlopen("http://127.0.0.1:9912/usage"))
model = usage["by_model"][0]
expected_cost = (120 * 0.5 + 80 * 1.5) / 1_000_000  # 0.00018

checks = {
    "both requests metered for the model": model["requests"] == 2,
    "prompt tokens recorded": model["prompt_tokens"] == 120,
    "completion tokens recorded": model["completion_tokens"] == 80,
    "cost computed from MODEL_PRICES": abs(model["cost_usd"] - expected_cost) < 1e-12,
    "rejected key produced no usage row": len(usage["by_model"]) == 1,
    "raw key never stored": "testkey" not in json.dumps(usage["recent"]),
}
rows = sqlite3.connect("verify.db").execute(
    "select model, status, streamed, prompt_tokens, completion_tokens, cost_usd, client_key"
    " from usage_events order by id"
).fetchall()
checks["two rows written (1 non-stream + 1 stream)"] = len(rows) == 2
checks["streamed request flagged and unmetered tokens left null"] = len(rows) == 2 and rows[1][2] == 1 and rows[1][3] is None
checks["401 not written to db"] = all(r[1] == 200 for r in rows)
print("\nrows in sqlite:")
for r in rows:
    print(" ", r)
failed = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("PASS  " if ok else "FAIL  ") + k)
sys.exit(1 if failed else 0)
PY
echo "ALL CHECKS PASSED"
