# Metered AI Gateway

OpenAI-compatible proxy that **meters every request** — tokens, latency, and cost per caller key — into Postgres, and exposes the numbers as Prometheus metrics plus a JSON usage API.

Built for people who share or resell AI access and need per-request proof of spend, not just a monthly platform total.

```
client ──▶ gateway (:PORT) ──▶ upstream (OpenAI / Groq / OpenRouter / vLLM / Ollama …)
                 │
                 ├─▶ Postgres        usage_events (tokens, cost, latency, key hash)
                 ├─▶ /metrics        Prometheus text exposition
                 └─▶ /usage          JSON rollup by model + recent requests
```

## What it meters

| Field | Source |
|---|---|
| `prompt_tokens` / `completion_tokens` | the upstream response's `usage` block |
| `cost_usd` | your price table × tokens (`MODEL_PRICES`, USD per 1M tokens) |
| `latency_ms` | measured at the gateway, around the upstream call |
| `client_key` | SHA-256 prefix of the caller's bearer token — the raw key is never stored |
| `streamed` | flagged when `stream: true`; token counts stay `NULL` because SSE carries no usage block |

Failed upstream calls are recorded with status `502` and the error string. Requests rejected at the gateway (bad key) are **not** written, so unauthenticated traffic cannot inflate your table.

## Endpoints

- `POST /v1/chat/completions` (and any other `/v1/*` path) — proxied
- `GET /healthz` — `ok`, used by the Railway healthcheck
- `GET /metrics` — Prometheus: `gateway_requests_total`, `gateway_prompt_tokens_total`, `gateway_completion_tokens_total`, `gateway_cost_usd_total`, `gateway_latency_ms_sum`
- `GET /usage` — `{"by_model": [...], "recent": [...]}`

## Deploy

1. New project on Railway → **Deploy from repo** (this repo) — the `Dockerfile` is used, `railway.json` sets the healthcheck and restart policy.
2. Add **Postgres** to the project.
3. Service → Variables:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `UPSTREAM_BASE_URL` = e.g. `https://api.openai.com/v1`
   - `UPSTREAM_API_KEY` = your provider key (or leave empty for bring-your-own-key)
   - `GATEWAY_API_KEYS` = comma-separated keys your callers must present (leave empty to only meter, not gate)
   - `MODEL_PRICES` = `{"gpt-4o-mini":{"in":0.15,"out":0.6}}`
4. Generate a domain, then:
   ```bash
   curl -X POST https://<your-domain>/v1/chat/completions \
     -H "Authorization: Bearer $GATEWAY_KEY" -H 'Content-Type: application/json' \
     -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
   curl https://<your-domain>/usage
   ```

Point it at a local model by setting `UPSTREAM_BASE_URL=http://ollama.railway.internal:11434/v1` — the gateway does not care what is behind it as long as it speaks the OpenAI wire format.

## Verify locally (no API spend)

```bash
uv venv && uv pip install -r requirements.txt
./scripts/verify_local.sh
```

The script boots a fake upstream plus the gateway on SQLite and asserts that tokens, cost, latency, key hashing, the 401 path, and the streaming path all behave. Expected tail: `ALL CHECKS PASSED`.

## Notes and limits

- **Non-streaming requests** get exact token accounting. Streaming is relayed but not token-metered — if you bill on streams, use a provider that returns usage in the final SSE chunk and extend `proxy()`.
- Prices are whatever you put in `MODEL_PRICES`; nothing is fetched from providers. Keep it current or your cost column drifts.
- The gateway adds one network hop. In-region that is single-digit milliseconds (≈40–70 ms measured locally in the verify run).
- `usage_events` grows with traffic — add a retention job or partition once you pass a few million rows.

## Publishing as a Railway template (kickback program)

Eligibility (per [docs.railway.com/templates/kickbacks](https://docs.railway.com/templates/kickbacks)):

- publish to the **marketplace** — private/unpublished templates earn nothing
- comply with the Fair Use Policy and ToS
- kickback is **15%** of usage cost incurred by users who deploy your template, plus a **10% support bonus (25% total)** when you actively support users through the Template Queue

Checklist before publishing:

- [ ] repo is public and the README's deploy path has been followed once, from scratch
- [ ] the template includes Postgres so it deploys green with **zero** manual env edits except `UPSTREAM_BASE_URL` + `UPSTREAM_API_KEY`
- [ ] demo project attached, so the listing shows a working live demo
- [ ] `railway.json` healthcheck verified (`/healthz` returns 200)
- [ ] description states plainly that it proxies to *your* provider key — do not imply free inference
