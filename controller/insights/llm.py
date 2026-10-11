"""Minimal Gemini REST client (stdlib only) shared by the Copilot chat and the per-run AI brief.

Key: GEMINI_API_KEY from the environment, else the repo-root .env (the Makefile passes it to docker compose with
--env-file), else controller/.env.
It is re-read on every call, so adding the key needs no restart. Sent as the x-goog-api-key header, never
in the URL.

Model: CHAT_MODEL if set, else "gemini-flash-latest" (Google's alias for the current Flash model, so a model
retirement does not break the dashboard). If a model is gone (HTTP 404, e.g. gemini-2.5-flash is closed to new
keys) or hangs (no answer within MODEL_TIMEOUT_S, seen with gemini-flash-latest while 3.5-flash answered in 1 s),
the next newest "gemini-<version>-flash" the key can use is discovered via ListModels, tried, and remembered.
At most MAX_MODELS models are tried per call.

Quota protection (free-tier keys run out fast): every successful answer is cached on disk, so the same question
over the same data never costs a second call; at most LLM_DAILY_BUDGET HTTP requests go to Gemini per day (default
200, settable in the repo-root .env); after a quota error (HTTP 429) Gemini is not called again for QUOTA_PAUSE_S.
Callers then fall back to their rule-based answers. State lives in runs/logs/ (git-ignored).
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"          # controller/.env
ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"     # repo-root .env (Makefile: --env-file .env)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"
_FLASH = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")
_working_model: Optional[str] = None  # last model that answered, tried first next time
MODEL_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "20"))
MAX_MODELS = 3
STATE_DIR = Path(os.environ.get("LLM_STATE_DIR") or Path(__file__).resolve().parents[2] / "runs" / "logs")
QUOTA_PAUSE_S = 15 * 60
CACHE_MAX = 300
DEFAULT_BUDGET = 200


def _setting(name: str) -> str:
    """A plain setting from the repo-root .env (so the budget can be changed without a restart)."""
    val = ""
    if ROOT_ENV_FILE.is_file():
        for line in ROOT_ENV_FILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                val = v.strip().strip("\"'")
    return val


def daily_budget() -> int:
    try:
        return max(0, int(os.environ.get("LLM_DAILY_BUDGET", "") or _setting("LLM_DAILY_BUDGET") or DEFAULT_BUDGET))
    except ValueError:
        return DEFAULT_BUDGET


def _load(name: str, default):
    try:
        return json.loads((STATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _store(name: str, data) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / name).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def usage() -> Dict:
    """Today's Gemini usage for the UI: calls made, budget, answers served from cache, pause after a quota error."""
    u = _load("llm_usage.json", {})
    today = time.strftime("%Y-%m-%d")
    same = u.get("day") == today
    paused = u.get("paused_until") or 0
    return {"day": today, "calls": u.get("calls", 0) if same else 0, "budget": daily_budget(),
            "cache_hits": u.get("cache_hits", 0) if same else 0, "paused_until": paused if paused > time.time() else None}


def _count(field: str = "calls") -> None:
    u = _load("llm_usage.json", {})
    today = time.strftime("%Y-%m-%d")
    if u.get("day") != today:
        u = {"day": today, "calls": 0, "cache_hits": 0, "paused_until": u.get("paused_until", 0), "model": u.get("model")}
    u[field] = u.get(field, 0) + 1
    _store("llm_usage.json", u)


def _remember_model(model: str) -> None:
    """The last model that answered is tried first after a restart too (no calls wasted on a slow alias)."""
    u = _load("llm_usage.json", {})
    if u.get("model") != model:
        u["model"] = model
        _store("llm_usage.json", u)


def _pause() -> None:
    u = _load("llm_usage.json", {})
    u["paused_until"] = time.time() + QUOTA_PAUSE_S
    _store("llm_usage.json", u)


def _guard() -> None:
    """Raise instead of calling Gemini when today's budget is used up or a quota error happened recently."""
    st = usage()
    if st["paused_until"]:
        raise LLMError("Gemini paused after a quota error until "
                       + time.strftime("%H:%M", time.localtime(st["paused_until"])) + " (protecting the key)")
    if st["calls"] >= st["budget"]:
        raise LLMError(f"daily AI budget used ({st['calls']}/{st['budget']} Gemini calls today; "
                       "LLM_DAILY_BUDGET in .env changes it)")


def configured_model(preferred: Optional[str] = None) -> str:
    """A model proven to work in this process wins, then CHAT_MODEL, then the caller's choice, then the default."""
    return (_working_model or os.environ.get("CHAT_MODEL", "").strip() or preferred
            or _load("llm_usage.json", {}).get("model") or DEFAULT_MODEL)


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


PLACEHOLDERS = {"replace-with-your-gemini-api-key", "your-key-here", "changeme"}


def _key_from(path: Path) -> str:
    """dotenv semantics: the last non-empty assignment wins (a copied template often leaves an empty one above)."""
    key = ""
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            k, _, v = line.partition("=")
            v = v.strip().strip("\"'")
            if k.strip().removeprefix("export ").strip() == "GEMINI_API_KEY" and v and v not in PLACEHOLDERS:
                key = v
    return key


def api_key() -> str:
    """Process environment first, then the repo-root .env (the Makefile's --env-file), then controller/.env."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key and key not in PLACEHOLDERS:
        return key
    return _key_from(ROOT_ENV_FILE) or _key_from(ENV_FILE)


def _post(model: str, key: str, body: Dict, timeout: Optional[float] = None) -> Dict:
    req = urllib.request.Request(URL.format(model=model), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout or MODEL_TIMEOUT_S) as r:
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
             temperature: float = 0.2, json_mode: bool = False, model: Optional[str] = None,
             timeout: Optional[float] = None) -> Dict[str, str]:
    global _working_model
    key = key if key is not None else api_key()
    if not key:
        raise LLMError("no GEMINI_API_KEY configured (put it in the repo-root .env, see .env.example)")
    # thinkingBudget 0: these are short grounded answers; with thinking on, newer Flash models can spend the whole
    # token budget thinking and return nothing (finishReason MAX_TOKENS)
    gen = {"temperature": temperature, "maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 0}}
    if json_mode:
        gen["responseMimeType"] = "application/json"  # model must return bare JSON (no prose, no fences)
    body = {"contents": contents, "generationConfig": gen}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    ck = hashlib.sha256(json.dumps([body, model], sort_keys=True, default=str).encode()).hexdigest()
    hit = _load("llm_cache.json", {}).get(ck)
    if hit:  # same question over the same data: answer again without a call
        _count("cache_hits")
        return dict(hit)
    _guard()

    def remember(res: Dict[str, str]) -> Dict[str, str]:
        c = _load("llm_cache.json", {})
        c[ck] = res
        if len(c) > CACHE_MAX:
            c = dict(list(c.items())[-CACHE_MAX:])
        _store("llm_cache.json", c)
        return res

    queue = [configured_model(model)]
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
        for attempt in range(2):
            try:
                _guard()
                _count()
                data = _post(model, key, body, timeout)
                cand = (data.get("candidates") or [{}])[0]
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()
                if not text:
                    raise LLMError(f"empty answer from {model} (finish reason {cand.get('finishReason', '?')})")
                _working_model = model
                _remember_model(model)
                return remember({"text": text, "model": data.get("modelVersion") or model})
            except urllib.error.HTTPError as exc:
                last = _reason(exc)
                if exc.code == 400 and "thinking" in last.lower() and "thinkingConfig" in gen:
                    gen.pop("thinkingConfig")  # model without a thinking budget setting: retry without it
                    continue
                if exc.code == 429 and "quota" in last.lower():
                    if tried >= 2:  # two models out of quota: stop spending calls and pause Gemini for a while
                        _pause()
                        raise LLMError(last) from exc
                    next_models(model)  # free-tier quotas are per model: another Flash model may still have some
                    break
                if exc.code in (429, 500, 503) and attempt < 1:
                    time.sleep(2.0)
                    continue
                if exc.code == 404 or (exc.code in (500, 503) and attempt == 1):
                    next_models(model)
                    break  # next model
                raise LLMError(last) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason):
                    last = f"{model} did not answer within {timeout or MODEL_TIMEOUT_S:.0f}s"
                    next_models(model)
                    break  # model hanging: try the next one instead of failing
                raise LLMError(f"cannot reach Gemini ({reason})") from exc
            except (ValueError, KeyError) as exc:
                raise LLMError(f"unexpected Gemini response ({type(exc).__name__})") from exc
    if "HTTP 429" in last:
        _pause()
    raise LLMError(last)
