"""Small lazy wrapper around Google's supported Gemini SDK."""

from __future__ import annotations

import os


DEFAULT_MODEL = "gemini-3.8-flash"


def generate_json(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Generate JSON text with the Gemini API without logging credentials."""
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
