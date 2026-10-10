"""Minimal Gemini REST client (stdlib only) shared by the Copilot chat and the per-run AI brief.

Key: GEMINI_API_KEY from the environment or controller/.env (the same file docker compose already reads).
It is re-read on every call, so adding the key needs no restart. Sent as the x-goog-api-key header, never
in the URL.

Model: CHAT_MODEL if set, else "gemini-flash-latest" (Google's alias for the current Flash model, so a model
retirement does not break the dashboard). If a model is gone (HTTP 404, e.g. gemini-2.5-flash is closed to new
keys) or hangs (no answer within MODEL_TIMEOUT_S, seen with gemini-flash-latest while 3.5-flash answered in 1 s),
the next newest "gemini-<version>-flash" the key can use is discovered via ListModels, tried, and remembered.
At most MAX_MODELS models are tried per call.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
_FLASH = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")
_working_model: Optional[str] = None  # last model that answered, tried first next time
MODEL_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "20"))
MAX_MODELS = 4


def configured_model() -> str:
    return _working_model or os.environ.get("CHAT_MODEL", "").strip() or DEFAULT_MODEL


def discover_flash_models(key: str) -> List[str]:
    """Newest-first list of plain gemini-<version>-flash models this key can call."""
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                 headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            models = json.loads(r.read().decode()).get("models", [])
    except Exception:  # noqa: BLE001 - discovery is best effort
        return []
    found = []
    for m in models:
        name = m.get("name", "").removeprefix("models/")
        hit = _FLASH.match(name)
        if hit and "generateContent" in m.get("supportedGenerationMethods", []):
            found.append((float(hit.group(1)), name))
    return [n for _, n in sorted(found, reverse=True)]


class LLMError(Exception):
    """Human-readable reason the model could not answer (shown in the UI)."""


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key or not ENV_FILE.is_file():
        return key
    # dotenv semantics: the last non-empty assignment wins (a copied template often leaves an empty one above)
    for line in ENV_FILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        k, _, v = line.partition("=")
        if k.strip().removeprefix("export ").strip() == "GEMINI_API_KEY" and v.strip().strip("\"'"):
            key = v.strip().strip("\"'")
    return key


def _post(model: str, key: str, body: Dict) -> Dict:
    req = urllib.request.Request(URL.format(model=model), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def _reason(exc: urllib.error.HTTPError) -> str:
    try:
        msg = json.loads(exc.read().decode()).get("error", {}).get("message", "")
    except Exception:  # noqa: BLE001
        msg = ""
    hint = {400: "API key rejected or bad request", 401: "API key rejected", 403: "API key not allowed for this API",
            404: "model not found", 429: "rate limit / quota exceeded", 503: "model overloaded"}.get(exc.code, "error")
    return f"Gemini {hint} (HTTP {exc.code}){': ' + msg[:160] if msg else ''}"


def generate(system: str, contents: List[Dict], key: Optional[str] = None, max_tokens: int = 700,
             temperature: float = 0.2) -> Dict[str, str]:
    global _working_model
    key = key if key is not None else api_key()
    if not key:
        raise LLMError("no GEMINI_API_KEY configured (put it in controller/.env)")
    # thinkingBudget 0: these are short grounded answers; with thinking on, newer Flash models can spend the whole
    # token budget thinking and return nothing (finishReason MAX_TOKENS)
    gen = {"temperature": temperature, "maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 0}}
    body = {"contents": contents, "generationConfig": gen}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}

    queue = [configured_model()]
    discovered = False
    tried = 0
    last = "unknown error"

    def next_models(model: str) -> None:
        nonlocal discovered
        global _working_model
        if _working_model == model:
            _working_model = None
        if not discovered:
            discovered = True
            queue.extend(m for m in discover_flash_models(key) if m != model and m not in queue)

    while queue and tried < MAX_MODELS:
        model = queue.pop(0)
        tried += 1
        for attempt in range(3):
            try:
                data = _post(model, key, body)
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
                if not text:
                    raise LLMError(f"empty answer from {model} (finish reason {cand.get('finishReason', '?')})")
                _working_model = model
                return {"text": text, "model": data.get("modelVersion") or model}
            except urllib.error.HTTPError as exc:
                last = _reason(exc)
                if exc.code == 400 and "thinking" in last.lower() and "thinkingConfig" in gen:
                    gen.pop("thinkingConfig")  # model without a thinking budget setting: retry without it
                    continue
                if exc.code == 429 and "quota" in last.lower():
                    next_models(model)  # free-tier quotas are per model: another Flash model may still have some
                    break
                if exc.code in (429, 500, 503) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if exc.code == 404 or (exc.code in (500, 503) and attempt == 2):
                    next_models(model)
                    break  # next model
                raise LLMError(last) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason):
                    last = f"{model} did not answer within {MODEL_TIMEOUT_S:.0f}s"
                    next_models(model)
                    break  # model hanging: try the next one instead of failing
                raise LLMError(f"cannot reach Gemini ({reason})") from exc
            except (ValueError, KeyError) as exc:
                raise LLMError(f"unexpected Gemini response ({type(exc).__name__})") from exc
    raise LLMError(last)
