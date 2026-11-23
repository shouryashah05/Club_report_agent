"""
Shared Gemini client.

The API key is read from the GEMINI_API_KEY environment variable, or from a
local `.env` file next to this script (KEY=value per line). If the primary
model is overloaded, one fallback model is tried; on a quota error (429)
Gemini is paused for a few minutes and the agents run in offline mode.
"""
import json
import os
import time

ROOT = os.path.dirname(__file__)


def _load_dotenv(path=os.path.join(ROOT, ".env")):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# Flash-Lite: the lightest model, with the highest free-tier request limits
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Tried only when the primary model is overloaded (503/504) — never on a quota error
FALLBACK_MODELS = [m for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite").split(",")
                   if m and m != MODEL]
last_model_used = MODEL
EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
QUOTA_COOLDOWN_SECONDS = 300  # after a 429, skip Gemini entirely for this long

_client = None
_quota_blocked_until = 0.0


class QuotaExhausted(RuntimeError):
    pass


def quota_blocked() -> bool:
    return time.time() < _quota_blocked_until


def available() -> bool:
    """True when a key is set and we are not in a quota cooldown (so no calls are wasted)."""
    return bool(os.environ.get("GEMINI_API_KEY")) and not quota_blocked()


def status() -> dict:
    return {"llm": available(), "model": MODEL if available() else None,
            "key_set": bool(os.environ.get("GEMINI_API_KEY")), "quota_blocked": quota_blocked(),
            "retry_in_seconds": max(0, int(_quota_blocked_until - time.time()))}


def client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                               http_options=types.HttpOptions(timeout=25_000))
    return _client


def _call(fn):
    """Run one Gemini request. A 429 (quota / rate limit) is final: no retry, no other
    model; Gemini is paused for QUOTA_COOLDOWN_SECONDS so callers fall back to rules."""
    global _quota_blocked_until
    from google.genai import errors
    try:
        return fn()
    except errors.ClientError as e:
        if getattr(e, "code", None) == 429:
            _quota_blocked_until = time.time() + QUOTA_COOLDOWN_SECONDS
            raise QuotaExhausted("Gemini quota exhausted (429); using offline mode for "
                                 f"{QUOTA_COOLDOWN_SECONDS // 60} minutes.") from e
        raise


def generate(contents, config):
    """One request on the primary model; only if it is overloaded (503/504) try the fallback once."""
    global last_model_used
    from google.genai import errors
    if quota_blocked():
        raise QuotaExhausted("Gemini quota exhausted; using offline mode.")
    models = [MODEL] + FALLBACK_MODELS
    for i, model in enumerate(models):
        try:
            resp = _call(lambda: client().models.generate_content(model=model, contents=contents, config=config))
            last_model_used = model
            return resp
        except errors.ServerError as e:
            if getattr(e, "code", None) not in (503, 504) or i == len(models) - 1:
                raise


def to_jsonable(obj):
    """Round-trip through JSON so numpy/pandas scalars become plain Python values."""
    def default(o):
        if hasattr(o, "item"):
            return o.item()
        return str(o)
    return json.loads(json.dumps(obj, default=default))


def generate_json(prompt: str, system: str = None, temperature: float = 0.2) -> dict:
    """One-shot call that must return a JSON object."""
    from google.genai import types
    resp = generate(prompt, types.GenerateContentConfig(system_instruction=system, temperature=temperature,
                                                       response_mime_type="application/json"))
    return json.loads(resp.text)


def generate_text(prompt: str, system: str = None, temperature: float = 0.4) -> str:
    from google.genai import types
    resp = generate(prompt, types.GenerateContentConfig(system_instruction=system, temperature=temperature))
    return (resp.text or "").strip()


def embed(text: str) -> list:
    if quota_blocked():
        raise QuotaExhausted("Gemini quota exhausted; using offline mode.")
    resp = _call(lambda: client().models.embed_content(model=EMBED_MODEL, contents=[text]))
    return list(resp.embeddings[0].values)
