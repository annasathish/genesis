"""analyzer.py and planner.py call Gemini through insights/llm.py; every failure must fall back to heuristics."""
import json

import pytest

import analyzer
import planner
from insights import llm

ARGS = dict(fault_type="container_crash", target_service="catalogue", fault_params="{}", p95_baseline=33.0,
            p95_during=40.0, error_rate=0.06, recovery_time_sec=15.3, kill_switch_fired=True, score_out_of_10=7.3)


def _fake_generate(text, seen=None):
    def gen(system, contents, key=None, max_tokens=700, temperature=0.2, json_mode=False, model=None):
        if seen is not None:
            seen.append(contents[0]["parts"][0]["text"])
        return {"text": text, "model": "gemini-3.8-flash"}
    return gen


class TestAnalyzer:
    def test_llm_answer_is_used_and_masked(self, monkeypatch):
        seen = []
        monkeypatch.setattr(llm, "generate", _fake_generate(
            '```json\n{"explanation": "catalogue down, mail ops@shop.io", "recommended_fix": "Increase Replicas"}\n```', seen))
        r = analyzer.analyze_experiment(**ARGS, use_llm=True)
        assert r["recommended_fix"] == "Increase Replicas"
        assert "catalogue down" in r["explanation"] and "ops@shop.io" not in r["explanation"]
        assert "catalogue" in seen[0]

    def test_fix_outside_vocabulary_is_replaced(self, monkeypatch):
        monkeypatch.setattr(llm, "generate", _fake_generate('{"explanation": "x", "recommended_fix": "Rewrite in Rust"}'))
        assert analyzer.analyze_experiment(**ARGS, use_llm=True)["recommended_fix"] in analyzer.FIXED_VOCABULARY

    @pytest.mark.parametrize("failure", [llm.LLMError("no GEMINI_API_KEY"), ValueError("bad json")])
    def test_any_failure_falls_back_to_heuristics(self, monkeypatch, failure):
        def boom(*a, **k):
            raise failure
        monkeypatch.setattr(llm, "generate", boom)
        r = analyzer.analyze_experiment(**ARGS, use_llm=True)
        assert r == analyzer.analyze_experiment(**ARGS, use_llm=False)

    def test_non_json_answer_falls_back(self, monkeypatch):
        monkeypatch.setattr(llm, "generate", _fake_generate("Sorry, I cannot help with that."))
        assert analyzer.analyze_experiment(**ARGS, use_llm=True)["recommended_fix"] in analyzer.FIXED_VOCABULARY

    def test_no_llm_never_calls_the_model(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not be called")
        monkeypatch.setattr(llm, "generate", boom)
        analyzer.analyze_experiment(**ARGS, use_llm=False)

    def test_no_old_sdk_dependency(self):
        assert not hasattr(analyzer, "genai") and not hasattr(planner, "genai")


GRAPH = {"nodes": [{"id": "cart", "kind": "worker"}, {"id": "redis", "kind": "cache"}, {"id": "web", "kind": "frontend"}]}


class TestPlanner:
    def test_use_llm_off_returns_heuristics_untouched(self, monkeypatch):
        monkeypatch.delenv("USE_LLM", raising=False)
        cands = planner.generate_heuristic_candidates(GRAPH)
        assert planner.llm_rerank(list(cands)) == cands

    def test_rerank_merges_llm_order(self, monkeypatch):
        monkeypatch.setenv("USE_LLM", "true")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")  # llm_rerank needs a key set
        cands = planner.generate_heuristic_candidates(GRAPH)
        ids = [c["candidate_id"] for c in cands[:3]]
        answer = [{"candidate_id": ids[2], "rank": 1, "reason": "r1", "hypothesis": "h1"},
                  {"candidate_id": ids[0], "rank": 2, "reason": "r2", "hypothesis": "h2"}]
        monkeypatch.setattr(llm, "generate", _fake_generate(json.dumps(answer)))
        out = planner.llm_rerank([dict(c) for c in cands])
        assert [c["candidate_id"] for c in out][:2] == [ids[2], ids[0]]       # AI order first
        assert sorted(c["candidate_id"] for c in out) == sorted(c["candidate_id"] for c in cands)  # none dropped
        assert out[0]["llm_rank"] == 1 and out[0]["hypothesis"] == "h1" and out[0]["reasoning"] == "r1"

    @pytest.mark.parametrize("failure", [llm.LLMError("Gemini model not found (HTTP 404)"), None])
    def test_failure_or_garbage_falls_back(self, monkeypatch, failure):
        monkeypatch.setenv("USE_LLM", "true")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")  # llm_rerank needs a key set
        cands = planner.generate_heuristic_candidates(GRAPH)

        def gen(*a, **k):
            if failure:
                raise failure
            return {"text": "not json at all", "model": "m"}
        monkeypatch.setattr(llm, "generate", gen)
        assert planner.llm_rerank(cands) == cands


def test_ai_client_routes_through_insights_llm(monkeypatch):
    """ai_client.generate_json keeps its interface but uses the REST client (timeouts, model fallback)."""
    import ai_client
    seen = {}

    def gen(system, contents, key=None, max_tokens=700, temperature=0.2, json_mode=False, model=None):
        seen.update(json_mode=json_mode, model=model, prompt=contents[0]["parts"][0]["text"])
        return {"text": '{"ok": true}', "model": "gemini-test"}
    monkeypatch.setattr(llm, "generate", gen)
    assert ai_client.generate_json("say ok") == '{"ok": true}'
    assert seen == {"json_mode": True, "model": ai_client.DEFAULT_MODEL, "prompt": "say ok"}

    def boom(*a, **k):
        raise llm.LLMError("Gemini rate limit / quota exceeded (HTTP 429)")
    monkeypatch.setattr(llm, "generate", boom)
    with pytest.raises(RuntimeError, match="429"):     # same exception type callers already handle
        ai_client.generate_json("x")


def test_api_key_lookup_order(tmp_path, monkeypatch):
    root, ctl = tmp_path / "root.env", tmp_path / "ctl.env"
    monkeypatch.setattr(llm, "ROOT_ENV_FILE", root)
    monkeypatch.setattr(llm, "ENV_FILE", ctl)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm.api_key() == ""
    ctl.write_text("GEMINI_API_KEY=from-controller")
    assert llm.api_key() == "from-controller"
    root.write_text("GEMINI_API_KEY=replace-with-your-gemini-api-key")   # .env.example placeholder ignored
    assert llm.api_key() == "from-controller"
    root.write_text("GEMINI_API_KEY=from-root")
    assert llm.api_key() == "from-root"
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert llm.api_key() == "from-env"


def test_explicit_model_does_not_override_a_proven_one(monkeypatch):
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    monkeypatch.setattr(llm, "_working_model", None)
    assert llm.configured_model("gemini-3.8-flash") == "gemini-3.8-flash"
    monkeypatch.setattr(llm, "_working_model", "gemini-3.6-flash")
    assert llm.configured_model("gemini-3.8-flash") == "gemini-3.6-flash"
