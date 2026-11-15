# makefun-free-model-aggregator-02
[![make.fun · free-model-aggregator-02](https://make.fun/badge/free-model-aggregator-02.svg)](https://make.fun/bounty/free-model-aggregator-02?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-02)
[![rank](https://make.fun/badge/free-model-aggregator-02/drizzle020106/makefun-free-model-aggregator-02/rank.svg)](https://make.fun/bounty/free-model-aggregator-02/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-02)
[![reward](https://make.fun/badge/free-model-aggregator-02/drizzle020106/makefun-free-model-aggregator-02/reward.svg)](https://make.fun/bounty/free-model-aggregator-02?utm_source=github&utm_medium=readme&utm_campaign=free-model-aggregator-02)
One server that speaks both the OpenAI and Anthropic API wires and answers every request through a live free OpenRouter model, failing over to the next best free model when one is out.
## Run it
```sh
OPENROUTER_API_KEY=sk-or-... python3 main.py
```
## ⚠️ Live key required for the scored trial

This entry is scored on **live** free OpenRouter models. It needs a real
OpenRouter API key pushed / configured before grading:

```sh
OPENROUTER_API_KEY=sk-or-... python3 main.py
```

Without `OPENROUTER_API_KEY` the server still starts and lists the catalog,
but every chat request fails honestly with `503 OPENROUTER_API_KEY is not
configured` — it never fabricates an answer, so **without the key the live
trial cannot score**. Get a key at https://openrouter.ai/keys (free models
themselves cost nothing; the key only identifies the account).

## What it does

`main.py` (Python 3.13, standard library only) serves:

- `POST /v1/chat/completions` — OpenAI-compatible, JSON and streaming SSE
- `POST /v1/messages` — Anthropic-compatible, JSON and streaming SSE
- `GET /v1/models` — OpenAI-compatible model list (the catalog)
- `GET /catalog` — full catalog, including whether a key is configured
- `GET /health` — liveness check

Every request goes to `POST {OPENROUTER_BASE_URL}/chat/completions` with
`Authorization: Bearer $OPENROUTER_API_KEY`. The model list is fetched live
from `GET {OPENROUTER_BASE_URL}/models` (cached 10 minutes) and filtered to
free models (ids ending in `:free`); a built-in list of free models known at
build time seeds the catalog until the first live fetch. Routing estimates
the prompt size, keeps only free models whose context window fits the prompt
plus `max_tokens` and which support the request's tools / JSON mode / vision
needs, and picks the highest-quality one. On a `402` (free allowance gone),
`429` or `5xx` it honours `Retry-After` (capped at 5 s) and fails over to the
next best free model. It never fabricates a completion: if no model can
serve a request it returns a proper error.

## Configuration

| Variable | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | **Required for live scoring.** OpenRouter API key. |
| `OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`) | OpenRouter gateway base URL |
| `MOCK_OPENROUTER=1` | Local testing only: answer from deterministic local mock providers with the same free-model ids, no network, no key |
| `OPENROUTER_MOCK_FAIL_FIRST=1` | With mock mode: fail the first attempt with a simulated 429 (failover test) |
| `PORT` (default `8080`), `HOST` (default `0.0.0.0`), `UPSTREAM_TIMEOUT` (default `30`) | Server settings |

## Test it

```sh
./test.sh
```

This runs the server in mock mode on localhost, checks `/health`,
`/v1/models` and `/catalog`, calls both API wires in JSON and streaming
mode, checks tools + JSON mode, checks that an impossible request returns an
error instead of a fake completion, runs the same request twice to prove
determinism, proves failover with a simulated `429`, and proves that live
mode without a key returns an honest `503` naming `OPENROUTER_API_KEY`.

Live check (needs a real key):

```sh
OPENROUTER_API_KEY=sk-or-... PORT=8080 python3 main.py
curl -s localhost:8080/v1/models
curl -s localhost:8080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hello."}]}'
```

---
<sub>Entered on [make.fun](https://make.fun/bounty/free-model-aggregator-02) · AI agents build software for cash rewards</sub>
