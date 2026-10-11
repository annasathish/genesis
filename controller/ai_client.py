"""Small lazy wrapper around Gemini, shared by the planner, the M5 analyzer and dashboard_api.

`generate_json(prompt)` returns JSON text or raises. It goes through `insights/llm.py` (Gemini REST, stdlib only)
first: that client has a timeout, falls back from a hanging, retired or out-of-quota model to the newest available
Flash model, and reads GEMINI_API_KEY from the environment or the ignored .env file. If that module is not
available, Google's supported `google-genai` SDK is used directly.
"""

from __future__ import annotations

import os


DEFAULT_MODEL = "gemini-3.8-flash"


def generate_json(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Generate JSON text with the Gemini API without logging credentials."""
    try:
        from insights import llm
    except ImportError:
        llm = None

    if llm is not None:
        try:
            out = llm.generate("", [{"role": "user", "parts": [{"text": prompt}]}],
                               max_tokens=2000, json_mode=True, model=model)
        except llm.LLMError as exc:
            raise RuntimeError(str(exc)) from exc
        return out["text"]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError("Install google-genai to enable Gemini features") from exc

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response")
    return response.text
