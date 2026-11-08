"""Pip's AI, standard library only. Copied from relay/client.py (2026-11-07, phase E) so the layer ships alone:
keep PROVIDERS, build_request, parse_reply and post the same in both files. Only ask() and check_key() are new.

Where a question goes (the family's choice on the "Pip's thinking" page, family.household.ai_mode):
  fambox  the FamBox AI relay: FAMBOX_AI_URL (default https://ai.fam.box) + FAMBOX_AI_TOKEN from the box's settings
  own     the family's own key (household.ai_provider, ai_key), straight to the provider, no relay
  rules   no AI
Nothing here ever logs a key, a token or what was written.
"""
import base64
import json
import os
import urllib.error
import urllib.request

# One place for every provider: where it lives, how it signs in, its wire format and its default models.
# chat = a small fast model; photo = a vision-capable one (letters, receipts).
PROVIDERS = {
    "anthropic": {"url": "https://api.anthropic.com/v1/messages", "wire": "anthropic",
                  "chat": "claude-haiku-5-5", "photo": "claude-sonnet-5-5"},
    "openai": {"url": "https://api.openai.com/v1/chat/completions", "wire": "openai",
               "chat": "gpt-5-mini", "photo": "gpt-5-mini", "max_field": "max_completion_tokens"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "wire": "openai",
                   "chat": "google/gemini-2.5-flash", "photo": "google/gemini-2.5-flash"},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "wire": "openai",
               "chat": "gemini-2.5-flash", "photo": "gemini-2.5-flash"},
    "mistral": {"url": "https://api.mistral.ai/v1/chat/completions", "wire": "openai",
                "chat": "mistral-small-latest", "photo": "mistral-small-latest", "image_as_string": True},
}
MAX_TOKENS = {"chat": 2000, "photo": 4000}  # includes the model's own thinking on Claude 5.x


class AIError(Exception):
    """reason: a short code (cap_reached, rate_limited, no_key, upstream_error, ...); status: HTTP status if any."""
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason, self.status = reason, status


def image_part(image):
    """bytes -> {"media_type", "data" (base64)}; a dict in that shape passes through."""
    if isinstance(image, dict):
        return image
    kind = ("image/png" if image[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp" if image[8:12] == b"WEBP"
            else "image/gif" if image[:4] == b"GIF8" else "image/jpeg")
    return {"media_type": kind, "data": base64.b64encode(image).decode()}


def build_request(provider, key, model, messages, system="", image=None, tools=None, max_tokens=None):
    """-> (url, headers, body) for one provider. image: {"media_type", "data"}, added to the last user message."""
    p = PROVIDERS[provider]
    kind = "photo" if image else "chat"
    model = model or p[kind]
    max_tokens = min(int(max_tokens or MAX_TOKENS[kind]), MAX_TOKENS["photo"])
    msgs = [{"role": m["role"], "content": str(m["content"])} for m in messages]
    if p["wire"] == "anthropic":
        if image:
            msgs[-1]["content"] = [{"type": "image", "source": {"type": "base64", **image}},
                                   {"type": "text", "text": msgs[-1]["content"]}]
        body = {"model": model, "max_tokens": max_tokens, "messages": msgs,
                "output_config": {"effort": "low" if kind == "chat" else "medium"}}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]
        headers = {"x-api-key": key, "anthropic-version": "2026-10-21"}
    else:
        if image:
            url = f"data:{image['media_type']};base64,{image['data']}"
            msgs[-1]["content"] = [{"type": "text", "text": msgs[-1]["content"]},
                                   {"type": "image_url", "image_url": url if p.get("image_as_string") else {"url": url}}]
        body = {"model": model, p.get("max_field", "max_tokens"): max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else []) + msgs}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t.get("parameters") or {"type": "object", "properties": {}}}} for t in tools]
        headers = {"Authorization": "Bearer " + key}
    return p["url"], headers, body


def parse_reply(provider, j):
    """Provider answer -> {"text", "tool_calls": [{"id", "name", "arguments"}], "model", "stop", "usage": {"input_tokens", "output_tokens"}}."""
    if PROVIDERS[provider]["wire"] == "anthropic":
        blocks = j.get("content") or []
        u = j.get("usage") or {}
        return {"text": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                "tool_calls": [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}}
                               for b in blocks if b.get("type") == "tool_use"],
                "model": j.get("model"), "stop": j.get("stop_reason"),
                "usage": {"input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0)}}
    choice = (j.get("choices") or [{}])[0]
    m = choice.get("message") or {}
    u = j.get("usage") or {}
    calls = []
    for c in m.get("tool_calls") or []:
        f = c.get("function") or {}
        try:
            args = json.loads(f.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append({"id": c.get("id"), "name": f.get("name"), "arguments": args})
    text = m.get("content") or ""
    if isinstance(text, list):  # some providers answer in parts
        text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
    return {"text": text, "tool_calls": calls, "model": j.get("model"), "stop": choice.get("finish_reason"),
            "usage": {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}}


def post(url, headers, body, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            reason = json.load(e).get("error")
        except Exception:
            reason = None
        raise AIError(reason if isinstance(reason, str) else "upstream_error", e.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise AIError("unreachable") from e


def call_provider(provider, key, messages, system="", image=None, tools=None, model=None, max_tokens=None, url=None):
    """One call straight to a provider with its key. url overrides the provider's address (tests)."""
    if provider not in PROVIDERS:
        raise AIError("unknown_provider")
    if not key:
        raise AIError("no_key")
    u, headers, body = build_request(provider, key, model, messages, system, image, tools, max_tokens)
    return parse_reply(provider, post(url or u, headers, body))


def setting(c):
    """The family's AI, or None: ("own", provider, key) or ("fambox", url, token)."""
    h = c.execute("select ai_mode, ai_provider, ai_key from family.household").fetchone()
    if not h:
        return None
    if h["ai_mode"] == "own" and h["ai_provider"] in PROVIDERS and h["ai_key"]:
        return "own", h["ai_provider"], h["ai_key"]
    token = os.environ.get("FAMBOX_AI_TOKEN", "").strip()
    if h["ai_mode"] == "fambox" and token:
        return "fambox", os.environ.get("FAMBOX_AI_URL", "").strip() or "https://ai.fam.box", token
    return None


def ask(c, messages, system="", image=None, tools=None, max_tokens=None):
    """One question to the family's AI. Raises AIError: no_ai, cap_reached, ai_not_set_up, rate_limited, unreachable, ..."""
    s = setting(c)
    if not s:
        raise AIError("no_ai")
    img = image_part(image) if image else None
    if s[0] == "own":
        return call_provider(s[1], s[2], messages, system, img, tools, os.environ.get("FAMBOX_AI_MODEL") or None, max_tokens,
                             url=os.environ.get("FAMBOX_AI_OWN_URL") or None)  # a stand-in provider, tests only
    body = {"messages": messages, "system": system, "image": img, "tools": tools, "max_tokens": max_tokens}
    return post(s[1].rstrip("/") + "/v1/chat", {"Authorization": "Bearer " + s[2]}, {k: v for k, v in body.items() if v},
                timeout=90 if image else 45)


def check_key(provider, key):
    """One tiny call with a key the family pasted. None when it works, else "refused" or "offline"."""
    try:
        call_provider(provider, key, [{"role": "user", "content": "Say OK."}], max_tokens=200,
                      url=os.environ.get("FAMBOX_AI_OWN_URL") or None)
    except AIError as ex:
        return "refused" if ex.status in (400, 401, 403, 404) or ex.reason in ("no_key", "unknown_provider") else "offline"
    return None
