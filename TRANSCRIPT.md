# Transcript — makefun-free-model-aggregator-02

Date: 2026-11-15. Built by an AI agent (Muse) in a local session for Amy
(GitHub: drizzle020106) as an entry for make.fun bounty
`free-model-aggregator-02` — "The best live free-model aggregator", $500 in
USDC (same payout as 01; ranked prizes, entries outside the paid places earn
nothing). Entry fee $0.00. Entries close 2026-11-17 11:30 CST. Only 378
entries so far at build time.

## What was built

Built by adapting the bounty-01 server core (same routing, failover and
two-wire translation code), reworked for this bounty's live OpenRouter trial:

- `main.py` — a real working server, Python 3.13 (standard library only).
  It exposes OpenAI-compatible `POST /v1/chat/completions` (JSON +
  streaming SSE) and Anthropic-compatible `POST /v1/messages` (JSON +
  streaming SSE), plus `GET /v1/models`, `GET /catalog` and `GET /health`.
  Every chat request goes to `POST https://openrouter.ai/api/v1/chat/completions`
  with `Authorization: Bearer $OPENROUTER_API_KEY`. The model list is fetched
  live from `GET {base}/models` (cached 10 minutes) and filtered to free
  models (ids ending in `:free`); a built-in list of five free models known
  at build time seeds the catalog until the first live fetch. Routing picks
  the highest-quality free model that fits the prompt context and supports
  the request's tools / JSON mode / vision needs, and fails over on 402 /
  429 / 5xx honouring `Retry-After` (capped at 5 s). Cooldowns are tracked
  per (provider, model), so a 429 on one model does not block the other free
  models behind the same gateway. It never fabricates a completion.
- `test.sh` — local validation script (both API wires, JSON + streaming,
  tools + JSON mode, error case, determinism, failover with a simulated 429,
  and the no-key refusal check).
- `README.md` — copied from the official make.fun template, filled in with
  this entry's slug, badges and bounty link (plain link, no referral code),
  with a prominent warning that a live OpenRouter key is required.
- `LICENSE` — MIT.

## Live-key requirement (blocker for scoring, not for building)

**A live OpenRouter API key must be pushed / configured by the user before
grading** (`OPENROUTER_API_KEY`). Without it the server starts and lists the
catalog, but every chat request fails honestly with `503 OPENROUTER_API_KEY
is not configured` — verified in the test below — so the live trial cannot
score without the key. No key was available or used in this session; the
live path (real OpenRouter call, live `/models` fetch) is implemented but
**not yet tested against the real gateway**. `MOCK_OPENROUTER=1` exists only
for local testing and must NOT be set during grading.

## Validation run (2026-11-15, local machine, Python 3.12.3)

Note: only Python 3.12.3 was installed on this machine; the code uses only
standard-library features available in Python 3.13 as well, and the bounty
harness specifies Python 3.13. `python3 -m py_compile main.py` passed.

Command: `./test.sh` (run twice, in `MOCK_OPENROUTER=1` mode). Result both
times: **13 passed, 0 failed**.

- `GET /health` → `{"status": "ok", "version": "1.0.0", "providers": 1, "models": 5}`
- `GET /v1/models` → 5 free models (`deepseek/deepseek-r1:free`,
  `meta-llama/llama-3.3-70b-instruct:free`, `google/gemini-2.0-flash-exp:free`,
  `qwen/qwen-2.5-72b-instruct:free`, `mistralai/mistral-7b-instruct:free`)
- `GET /catalog` → `openrouter_key_configured: false, mock_mode: true`
- `POST /v1/chat/completions` (JSON) → served by `deepseek/deepseek-r1:free`
  (highest quality, 88), `X-Aggregator-Model` header present
- Streaming SSE → word chunks ending with `data: [DONE]`
- Tools + JSON mode → `tool_calls` for `get_weather`, no fake completion
- `POST /v1/messages` (Anthropic) → Anthropic-format message; streaming ends
  with `message_stop`
- Impossible request (`max_tokens` 2,000,000) → `context_length_exceeded`
  error, not a completion
- Same request twice → identical model and content (deterministic)
- Failover: simulated `429` once with `Retry-After: 1` on the best model →
  request served by `meta-llama/llama-3.3-70b-instruct:free` after ~1 s
  (Retry-After honoured)
- No-key check: live mode with no `OPENROUTER_API_KEY` → HTTP 503 naming
  `OPENROUTER_API_KEY`, an error body, no fabricated completion

Two bugs were found and fixed during validation: the mock provider id did
not match the provider id recorded on its models (chat requests crashed),
and the 429 cooldown was per-provider, which blocked every free model
behind the one OpenRouter gateway after a single model's 429 (now
per-model; the same fix was applied to the 01 server and its suite
re-run green).

No GitHub repo was created, nothing was submitted to make.fun, and no wallet
or seed phrase was used in this session. The next steps (repo creation with
the About settings in the submission report, pushing an OpenRouter key for
grading, then signing/submission by Amy in her own browser) are still to be
done. Do NOT resubmit or touch agency-poem-01.
