"""Test-wide guard: no test may read the real .env files or call Gemini with a real key.

Every test starts with GEMINI_API_KEY unset and the key lookup pointed at files that do not exist; tests that need a
key set a fake one themselves (monkeypatch.setenv / explicit key="...").
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        from insights import llm
    except ImportError:  # pragma: no cover
        return
    monkeypatch.setattr(llm, "ENV_FILE", tmp_path / "no-controller.env")
    monkeypatch.setattr(llm, "ROOT_ENV_FILE", tmp_path / "no-root.env")
    monkeypatch.setattr(llm, "_working_model", None)
    monkeypatch.setattr(llm, "STATE_DIR", tmp_path / "llm-state")  # fresh cache / budget per test, never runs/logs
