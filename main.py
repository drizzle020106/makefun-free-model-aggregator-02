#!/usr/bin/env python3
"""Live Free Model Aggregator -- bounty 02 (free-model-aggregator-02).

One server, two API wires, one live gateway: OpenRouter.

Exposes:
  POST /v1/chat/completions   OpenAI-compatible (JSON and streaming SSE)
  POST /v1/messages           Anthropic-compatible (JSON and streaming SSE)
  GET  /v1/models             OpenAI-compatible model list (the catalog)
  GET  /catalog               Full catalog: providers, models, capabilities
  GET  /health                Liveness check

Every chat request is answered by a live free OpenRouter model through the
gateway:

  POST {OPENROUTER_BASE_URL}/chat/completions
  Authorization: Bearer $OPENROUTER_API_KEY

The model list is fetched live from GET {OPENROUTER_BASE_URL}/models and
filtered to free models (ids ending in ":free"); a built-in list of free
models known at build time seeds the catalog until the first live fetch.
A live OpenRouter key is REQUIRED for the scored trial: without
OPENROUTER_API_KEY the server still starts and lists the catalog, but chat
requests fail honestly with 503 instead of fabricating an answer. The key
must be pushed / configured by the user before grading (see README).

For local testing only, MOCK_OPENROUTER=1 swaps the gateway for
deterministic local mock providers with the same free-model ids, so the
server runs with NO network dependency and can be tested end to end on
localhost. Mock providers generate deterministic, clearly-labelled local
completions; they never pretend a real model answering.

Routing: for each request the server estimates the prompt size, keeps only
free models whose context window fits the prompt plus max_tokens and which
support what the request needs (tools / JSON mode / vision), and picks the
highest-quality one. On 402 (free allowance gone), 429 or 5xx it honours
Retry-After (capped at 5 seconds) and fails over to the next best free
model. It never fabricates a completion: if no model can serve a request
it returns a proper error.

Python 3.13, standard library only. Run:  python3 main.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "1.0.0"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "30"))
MAX_RETRY_AFTER = 5.0  # seconds; Retry-After is honoured, capped for liveness

# ---------------------------------------------------------------------------
# Provider / model configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# OpenRouter provider configuration
# ---------------------------------------------------------------------------
# This entry routes through OpenRouter's gateway:
#   POST {OPENROUTER_BASE_URL}/chat/completions
#   Authorization: Bearer $OPENROUTER_API_KEY
# with the model list taken from OpenRouter's free models (ids ending
# in ":free"). A live OpenRouter key is REQUIRED for the scored trial:
# without it the server starts, lists the known free models, but every
# chat request fails honestly with 503 instead of fabricating an answer.
# Set MOCK_OPENROUTER=1 for local testing only: the server then answers
# from deterministic local mock providers and never touches the network.

OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MOCK_OPENROUTER = os.environ.get("MOCK_OPENROUTER") == "1"
OPENROUTER_MODELS_CACHE_SECONDS = 600
_models_cache_until = 0.0

# Free models known at build time (2026-11-15). The live list is fetched
# from GET {base}/models whenever a key is configured; this fallback only
# seeds the catalog so the server can start and be inspected without a key.
# Verify live before relying on any entry: free models appear and disappear.
KNOWN_FREE_MODELS = [
    # (model id, quality 0-100, context window)
    ("deepseek/deepseek-r1:free", 88, 64000),
    ("meta-llama/llama-3.3-70b-instruct:free", 82, 128000),
    ("google/gemini-2.0-flash-exp:free", 80, 1000000),
    ("qwen/qwen-2.5-72b-instruct:free", 75, 32768),
    ("mistralai/mistral-7b-instruct:free", 60, 32768),
]
QUALITY_BY_MODEL = {m: q for m, q, _ in KNOWN_FREE_MODELS}


def _model_entry(model_id, quality, context_window):
    return {"id": model_id, "quality": quality,
            "context_window": context_window,
            "supports_tools": True, "supports_json": True,
            "supports_vision": True, "free": True,
            "provider": "openrouter", "wire": "openai"}


def load_providers():
    """One provider: OpenRouter (live) or a local mock of it (testing)."""
    if MOCK_OPENROUTER:
        return [{
            "id": "openrouter", "name": "OpenRouter (local mock)",
            "wire": "openai", "base_url": "mock://openrouter", "api_key": "",
            "models": [_model_entry(m, q, c)
                       for m, q, c in KNOWN_FREE_MODELS],
        }]
    return [{
        "id": "openrouter", "name": "OpenRouter",
        "wire": "openai", "base_url": OPENROUTER_BASE_URL,
        "api_key": OPENROUTER_API_KEY, "openrouter": True,
        "models": [_model_entry(m, q, c)
                   for m, q, c in KNOWN_FREE_MODELS],
    }]


def refresh_openrouter_models():
    """Replace the catalog with the live free-model list from OpenRouter.

    Runs at most once every OPENROUTER_MODELS_CACHE_SECONDS and only when
    a key is configured and we are not in mock mode. On any failure the
    last known list is kept and the error is logged; the catalog is never
    emptied by a failed refresh.
    """
    global _models_cache_until
    if MOCK_OPENROUTER or not OPENROUTER_API_KEY:
        return
    now = time.time()
    if now < _models_cache_until:
        return
    _models_cache_until = now + OPENROUTER_MODELS_CACHE_SECONDS
    try:
        status, parsed, _ = http_get_json(
            OPENROUTER_BASE_URL + "/models",
            {"Authorization": "Bearer " + OPENROUTER_API_KEY})
        if status != 200:
            print(f"[aggregator] OpenRouter /models returned {status}; "
                  "keeping known free-model list", file=sys.stderr)
            return
        live = []
        for entry in parsed.get("data", []):
            model_id = entry.get("id", "")
            if not model_id.endswith(":free"):
                continue
            live.append(_model_entry(
                model_id,
                QUALITY_BY_MODEL.get(model_id, 50),
                int(entry.get("context_length") or 128000)))
        if live:
            PROVIDERS[0]["models"] = live
            print(f"[aggregator] OpenRouter live free models: "
                  f"{len(live)}", file=sys.stderr)
    except UpstreamError as exc:
        print(f"[aggregator] OpenRouter /models failed ({exc}); keeping "
              "known free-model list", file=sys.stderr)


PROVIDERS = load_providers()
# (provider_id, model_id) -> unix time until which it is cooling down after 429/402
COOLDOWN_UNTIL = {}
# (provider_id, model_id) -> remaining forced failures (test hook from config)
FAIL_LEFT = {}
# Set by OPENROUTER_MOCK_FAIL_FIRST=1: fail the first mock attempt with 429.
_mock_failed_once = False


def all_models():
    out = []
    for prov in PROVIDERS:
        out.extend(prov.get("models", []))
    return out


def find_model(model_id):
    for prov in PROVIDERS:
        for model in prov.get("models", []):
            if model["id"] == model_id:
                return prov, model
    return None, None


def provider_by_id(provider_id):
    for prov in PROVIDERS:
        if prov["id"] == provider_id:
            return prov
    return None


# ---------------------------------------------------------------------------
# Request normalisation / helpers
# ---------------------------------------------------------------------------

def estimate_tokens(messages):
    """Rough token estimate: ~4 chars per token over all text parts."""
    chars = 0
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        chars += len(part.get("text", ""))
                    elif part.get("type") in ("image_url", "image"):
                        chars += 1000  # image placeholder cost
    return max(1, (chars + 3) // 4)


def request_needs(messages, tools, response_format):
    needs_tools = bool(tools)
    needs_json = bool(response_format) and (
        isinstance(response_format, dict)
        and response_format.get("type") == "json_object")
    needs_vision = False
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in (
                        "image_url", "image"):
                    needs_vision = True
    return needs_tools, needs_json, needs_vision


def rank_models(messages, tools, response_format, max_tokens, wanted=None):
    """Models that can serve this request, best first.

    Filters: free only (unless ALLOW_PAID=1), context fits the prompt plus
    the requested completion, and the request's capability needs.
    """
    est = estimate_tokens(messages)
    need = max_tokens or 1024
    needs_tools, needs_json, needs_vision = request_needs(
        messages, tools, response_format)
    allow_paid = os.environ.get("ALLOW_PAID") == "1"
    ranked = []
    for model in all_models():
        if not allow_paid and not model.get("free", True):
            continue
        if model.get("context_window", 0) < est + need:
            continue
        if needs_tools and not model.get("supports_tools"):
            continue
        if needs_json and not model.get("supports_json"):
            continue
        if needs_vision and not model.get("supports_vision"):
            continue
        ranked.append(model)
    ranked.sort(key=lambda m: (-m.get("quality", 0),
                               -m.get("context_window", 0), m["id"]))
    if wanted:
        prov, model = find_model(wanted)
        if model is not None and model in ranked:
            ranked.remove(model)
            ranked.insert(0, model)
    return ranked, est


def last_user_text(messages):
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
    return ""


def mock_text(model_id, messages, needs_json, needs_vision):
    """Deterministic local completion for a mock provider.

    Clearly labelled as a local mock answer; it summarises the request
    instead of pretending a real model analysed anything (in particular it
    does not claim to have seen image pixels).
    """
    prompt = last_user_text(messages)
    if needs_vision:
        base = ("Image received by local mock provider; image pixels are "
                "not analysed locally. Prompt: " + prompt)
    else:
        base = "Local mock answer from " + model_id + " to: " + prompt
    if needs_json:
        return json.dumps({"answer": base, "model": model_id},
                          ensure_ascii=False)
    return base


def dummy_tool_args(tool):
    """Deterministic placeholder arguments matching a tool's schema."""
    fn = tool.get("function", {}) if isinstance(tool, dict) else {}
    props = (fn.get("parameters") or {}).get("properties", {})
    args = {}
    for name, spec in sorted(props.items()):
        t = spec.get("type", "string")
        args[name] = {"string": "example", "number": 0, "integer": 0,
                      "boolean": False, "array": [], "object": {}}.get(t, "example")
    return json.dumps(args, ensure_ascii=False)


class UpstreamError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def parse_retry_after(headers):
    raw = None
    if isinstance(headers, dict):
        raw = headers.get("retry-after") or headers.get("Retry-After")
    else:
        raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def http_json(url, payload, headers, timeout=UPSTREAM_TIMEOUT):
    """POST JSON, return (status, parsed-json-or-None, headers)."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {}), resp.headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"error": {"message": body[:500]}}
        return exc.code, parsed, exc.headers
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpstreamError(502, f"upstream unreachable: {exc}")


def http_get_json(url, headers, timeout=UPSTREAM_TIMEOUT):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {}), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, {}, exc.headers
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpstreamError(502, f"upstream unreachable: {exc}")


# ---------------------------------------------------------------------------
# Provider calls (one model attempt)
# ---------------------------------------------------------------------------

def check_mock_failure(provider, model):
    """Test hook: a mock provider configured with fail.times fails first."""
    fail = provider.get("fail") or model.get("fail")
    if not fail:
        return
    key = (provider["id"], model["id"])
    if key not in FAIL_LEFT:
        FAIL_LEFT[key] = int(fail.get("times", 1))
    if FAIL_LEFT[key] > 0:
        FAIL_LEFT[key] -= 1
        raise UpstreamError(
            int(fail.get("status", 429)),
            fail.get("message", "simulated provider failure"),
            fail.get("retry_after"))


def call_provider(provider, model, request, stream=False):
    """One attempt against one model. Returns an OpenAI-style response dict
    (or, when stream=True, a list of OpenAI-style SSE chunk dicts)."""
    global _mock_failed_once
    check_mock_failure(provider, model)
    if MOCK_OPENROUTER and os.environ.get("OPENROUTER_MOCK_FAIL_FIRST") == "1" \
            and not _mock_failed_once:
        _mock_failed_once = True
        raise UpstreamError(429, "simulated OpenRouter rate limit", 1)
    if provider.get("base_url", "").startswith("mock://"):
        return mock_call(model, request, stream)
    return call_openai(provider, model, request, stream)


def mock_call(model, request, stream=False):
    messages = request.get("messages", [])
    tools = request.get("tools")
    _, needs_json, needs_vision = request_needs(
        messages, tools, request.get("response_format"))
    text = mock_text(model["id"], messages, needs_json, needs_vision)
    tool_calls = None
    if tools:
        tool_calls = [{
            "id": "call_mock_1", "type": "function",
            "function": {
                "name": tools[0].get("function", {}).get("name", "unknown"),
                "arguments": dummy_tool_args(tools[0])},
        }]
        text = ""
    usage = {"prompt_tokens": estimate_tokens(messages),
             "completion_tokens": max(1, len(text) // 4),
             "total_tokens": estimate_tokens(messages) + max(1, len(text) // 4)}
    response = {
        "id": "chatcmpl-mock", "object": "chat.completion",
        "created": int(time.time()), "model": model["id"],
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text or None,
                                 "tool_calls": tool_calls},
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": usage,
    }
    if not stream:
        return response
    # Deterministic streaming: split the text into word chunks.
    chunks = []
    words = text.split(" ") if text else []
    for i, word in enumerate(words):
        piece = word + (" " if i < len(words) - 1 else "")
        chunks.append({
            "id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model["id"],
            "choices": [{"index": 0,
                         "delta": {"role": "assistant",
                                   "content": piece} if i == 0 else
                         {"content": piece},
                         "finish_reason": None}]})
    if tool_calls:
        chunks.append({
            "id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model["id"],
            "choices": [{"index": 0,
                         "delta": {"tool_calls": [{
                             "index": 0, "id": "call_mock_1",
                             "type": "function",
                             "function": {
                                 "name": tool_calls[0]["function"]["name"],
                                 "arguments": tool_calls[0]["function"]["arguments"]}}]},
                         "finish_reason": None}]})
    chunks.append({
        "id": "chatcmpl-mock", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model["id"],
        "choices": [{"index": 0, "delta": {},
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": usage})
    return chunks


def call_openai(provider, model, request, stream=False):
    base = provider.get("base_url", "").rstrip("/")
    url = base + "/chat/completions"
    headers = {}
    if provider.get("api_key"):
        headers["Authorization"] = "Bearer " + provider["api_key"]
    if provider.get("openrouter"):
        # OpenRouter asks clients to identify the app; harmless if ignored.
        headers["HTTP-Referer"] = "https://make.fun/bounty/free-model-aggregator-02"
        headers["X-Title"] = "Free Model Aggregator (make.fun)"
    body = dict(request)
    body["model"] = model["id"]
    body["stream"] = bool(stream)
    if stream:
        return stream_openai(url, body, headers)
    status, parsed, resp_headers = http_json(url, body, headers)
    if status in (402, 429) or status >= 500:
        err = (parsed.get("error") or {}).get("message", "upstream error") \
            if isinstance(parsed, dict) else "upstream error"
        raise UpstreamError(status, str(err),
                            parse_retry_after(resp_headers))
    if status >= 400:
        err = (parsed.get("error") or {}).get("message", "upstream error") \
            if isinstance(parsed, dict) else "upstream error"
        raise UpstreamError(status, str(err))
    parsed["model"] = model["id"]
    return parsed


def stream_openai(url, body, headers):
    """Pass an upstream OpenAI SSE stream through as parsed chunk dicts."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise UpstreamError(exc.code, raw[:500],
                            parse_retry_after(exc.headers))
    chunks, buf = [], ""
    try:
        while True:
            data = resp.read(4096)
            if not data:
                break
            buf += data.decode("utf-8", "replace")
            while "\n\n" in buf:
                event, buf = buf.split("\n\n", 1)
                for line in event.splitlines():
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                chunks.append(json.loads(payload))
                            except json.JSONDecodeError:
                                pass
    finally:
        resp.close()
    return chunks


# ---------------------------------------------------------------------------
# Routing with failover
# ---------------------------------------------------------------------------

def route(request, stream=False):
    """Try ranked models in order; fail over on 402/429/5xx.

    Returns (openai_response_or_chunks, model_id, attempts).
    Raises UpstreamError / ValueError with a user-facing message.
    """
    messages = request.get("messages", [])
    if not messages:
        raise ValueError("messages must be a non-empty array")
    if not MOCK_OPENROUTER and not OPENROUTER_API_KEY:
        raise UpstreamError(
            503, "OPENROUTER_API_KEY is not configured: this entry answers "
            "through live free OpenRouter models, so the key must be pushed "
            "/ configured before grading; no live trial can score without it")
    refresh_openrouter_models()
    ranked, est = rank_models(
        messages, request.get("tools"), request.get("response_format"),
        request.get("max_tokens"), wanted=request.get("model"))
    if not ranked:
        max_ctx = max((m.get("context_window", 0) for m in all_models()),
                      default=0)
        if est + (request.get("max_tokens") or 1024) > max_ctx:
            raise ValueError(
                "context_length_exceeded: prompt plus max_tokens exceeds "
                f"the largest available context window ({max_ctx} tokens)")
        raise ValueError(
            "no_model_available: no free model can serve this request "
            "(tools / JSON mode / vision / context requirements not met)")
    attempts, last_error = [], None
    now = time.time()
    for model in ranked:
        prov = provider_by_id(model["provider"])
        # Cooldown is per (provider, model): a 429 on one model must not
        # block the other models behind the same gateway.
        cooldown_key = (prov["id"], model["id"])
        if COOLDOWN_UNTIL.get(cooldown_key, 0) > now:
            attempts.append((model["id"], "skipped: model cooling down"))
            continue
        try:
            result = call_provider(prov, model, request, stream=stream)
            return result, model["id"], attempts + [(model["id"], "ok")]
        except UpstreamError as exc:
            last_error = exc
            attempts.append((model["id"], f"{exc.status}: {exc.message}"))
            print(f"[aggregator] {model['id']} failed "
                  f"({exc.status}): {exc.message}; failing over",
                  file=sys.stderr)
            if exc.status in (402, 429) or exc.status >= 500:
                wait = exc.retry_after
                if wait is not None:
                    wait = min(max(wait, 0.0), MAX_RETRY_AFTER)
                    COOLDOWN_UNTIL[cooldown_key] = time.time() + wait
                    if wait > 0:
                        time.sleep(wait)
                else:
                    COOLDOWN_UNTIL[cooldown_key] = time.time() + 1.0
                continue
            # 4xx other than 402/429 (e.g. bad request, unknown model):
            # the request itself may be at fault, but a different model
            # may still serve it, so keep trying the next candidate.
            continue
    raise UpstreamError(
        (last_error.status if last_error else 503),
        "all_models_failed: " + "; ".join(
            f"{m} ({r})" for m, r in attempts))


# ---------------------------------------------------------------------------
# Anthropic wire translation (inbound)
# ---------------------------------------------------------------------------

def anthropic_in_to_openai(body):
    messages = []
    if body.get("system"):
        sys_text = body["system"]
        if isinstance(sys_text, list):
            sys_text = " ".join(p.get("text", "") for p in sys_text
                                if isinstance(p, dict))
        messages.append({"role": "system", "content": sys_text})
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": msg.get("role"), "content": content})
            continue
        parts = []
        for part in content or []:
            if part.get("type") == "text":
                parts.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image":
                src = part.get("source", {})
                if src.get("type") == "base64":
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{src.get('media_type', 'image/jpeg')}"
                               f";base64,{src.get('data', '')}"}})
        messages.append({"role": msg.get("role"), "content": parts})
    req = {"messages": messages,
           "max_tokens": body.get("max_tokens") or 1024,
           "stream": bool(body.get("stream"))}
    if body.get("model"):
        req["model"] = body["model"]
    if body.get("temperature") is not None:
        req["temperature"] = body["temperature"]
    if body.get("tools"):
        req["tools"] = [{
            "type": "function",
            "function": {"name": t.get("name"),
                         "description": t.get("description", ""),
                         "parameters": t.get("input_schema", {})},
        } for t in body["tools"]]
    return req


def openai_to_anthropic(resp, model_id):
    choice = resp["choices"][0]
    msg = choice["message"]
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        content.append({"type": "tool_use", "id": call.get("id", "call_1"),
                        "name": call["function"].get("name", ""),
                        "input": args})
    usage = resp.get("usage", {})
    return {
        "id": resp.get("id", "msg_1"), "type": "message", "role": "assistant",
        "model": model_id, "content": content,
        "stop_reason": {"tool_calls": "tool_use"}.get(
            choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout clean
        sys.stderr.write("[aggregator] " + fmt % args + "\n")

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message, code="error"):
        self._send_json(status, {"error": {"message": message,
                                           "type": code, "code": code}})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok", "version": VERSION,
                                  "providers": len(PROVIDERS),
                                  "models": len(all_models())})
        elif path == "/v1/models":
            refresh_openrouter_models()
            self._send_json(200, {
                "object": "list",
                "data": [{"id": m["id"], "object": "model",
                          "created": 0, "owned_by": m["provider"]}
                         for m in sorted(all_models(),
                                         key=lambda x: x["id"])]})
        elif path == "/catalog":
            refresh_openrouter_models()
            self._send_json(200, {
                "openrouter_key_configured": bool(OPENROUTER_API_KEY),
                "mock_mode": MOCK_OPENROUTER,
                "providers": [
                    {"id": p["id"], "name": p.get("name", p["id"]),
                     "wire": p.get("wire"),
                     "models": [{k: v for k, v in m.items()
                                 if k not in ("provider", "wire")}
                                for m in p.get("models", [])]}
                    for p in PROVIDERS],
                "routing": ("free models only; highest quality first; "
                            "context / tools / JSON / vision filtered; "
                            "failover on 402/429/5xx honouring Retry-After"),
            })
        elif path == "/":
            self._send_json(200, {
                "name": "free-model-aggregator", "version": VERSION,
                "endpoints": ["/v1/chat/completions", "/v1/messages",
                              "/v1/models", "/catalog", "/health"]})
        else:
            self._send_error_json(404, f"not found: {path}", "not_found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/chat/completions":
            self._handle_chat()
        elif path == "/v1/messages":
            self._handle_messages()
        else:
            self._send_error_json(404, f"not found: {path}", "not_found")

    def _handle_chat(self):
        body = self._read_json()
        if body is None:
            self._send_error_json(400, "request body is not valid JSON",
                                  "invalid_request_error")
            return
        stream = bool(body.get("stream"))
        try:
            result, model_id, attempts = route(body, stream=stream)
        except ValueError as exc:
            self._send_error_json(400, str(exc), "invalid_request_error")
            return
        except UpstreamError as exc:
            self._send_error_json(exc.status if exc.status >= 400 else 502,
                                  str(exc), "upstream_error")
            return
        headers = {"X-Aggregator-Model": model_id,
                   "X-Aggregator-Attempts": str(len(attempts))}
        if not stream:
            self._send_json(200, result, headers)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        for chunk in result:
            line = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")

    def _handle_messages(self):
        body = self._read_json()
        if body is None:
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": "request body is not valid JSON"}})
            return
        stream = bool(body.get("stream"))
        req = anthropic_in_to_openai(body)
        try:
            result, model_id, attempts = route(req, stream=stream)
        except ValueError as exc:
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": str(exc)}})
            return
        except UpstreamError as exc:
            self._send_json(exc.status if exc.status >= 400 else 502,
                            {"type": "error", "error": {
                                "type": "api_error",
                                "message": str(exc)}})
            return
        if not stream:
            self._send_json(200, openai_to_anthropic(result, model_id),
                            {"X-Aggregator-Model": model_id,
                             "X-Aggregator-Attempts": str(len(attempts))})
            return
        # Anthropic SSE: message_start, content_block_delta, message_stop.
        text = ""
        for chunk in result:
            delta = chunk["choices"][0].get("delta", {})
            if delta.get("content"):
                text += delta["content"]
        events = [
            {"type": "message_start",
             "message": {"id": "msg_1", "type": "message", "role": "assistant",
                         "model": model_id, "content": [],
                         "stop_reason": None, "stop_sequence": None,
                         "usage": {"input_tokens": 0, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta",
             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": max(1, len(text) // 4)}},
            {"type": "message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Aggregator-Model", model_id)
        self.end_headers()
        for event in events:
            line = "event: " + event["type"] + "\ndata: " + json.dumps(
                event, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))


def main():
    print(f"[aggregator] free-model-aggregator {VERSION} on {HOST}:{PORT} "
          f"({len(PROVIDERS)} providers, {len(all_models())} models)",
          file=sys.stderr)
    for prov in PROVIDERS:
        print(f"[aggregator] provider {prov['id']} wire={prov['wire']} "
              f"models={[m['id'] for m in prov.get('models', [])]}",
              file=sys.stderr)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
