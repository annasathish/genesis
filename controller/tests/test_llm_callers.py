"""analyzer.py and planner.py call Gemini through insights/llm.py; every failure must fall back to heuristics."""
import json

import pytest

import analyzer
import planner
from insights import llm

ARGS = dict(fault_type="container_crash", target_service="catalogue", fault_params="{}", p95_baseline=33.0,
            p95_during=40.0, error_rate=0.06, recovery_time_sec=15.3, kill_switch_fired=True, score_out_of_10=7.3)


def _fake_generate(text, seen=None):
    def gen(system, contents, key=None, max_tokens=700, temperature=0.2):
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
        cands = planner.generate_heuristic_candidates(GRAPH)
        ids = [c["candidate_id"] for c in cands[:3]]
        answer = [{"candidate_id": ids[2], "rank": 1, "reason": "r1", "hypothesis": "h1"},
                  {"candidate_id": ids[0], "rank": 2, "reason": "r2", "hypothesis": "h2"}]
        monkeypatch.setattr(llm, "generate", _fake_generate(json.dumps(answer)))
        out = planner.llm_rerank([dict(c) for c in cands])
        assert [c["candidate_id"] for c in out] == [ids[2], ids[0]]
        assert out[0]["llm_rank"] == 1 and out[0]["hypothesis"] == "h1" and out[0]["reasoning"] == "r1"

    @pytest.mark.parametrize("failure", [llm.LLMError("Gemini model not found (HTTP 404)"), None])
    def test_failure_or_garbage_falls_back(self, monkeypatch, failure):
        monkeypatch.setenv("USE_LLM", "true")
        cands = planner.generate_heuristic_candidates(GRAPH)

        def gen(*a, **k):
            if failure:
                raise failure
            return {"text": "not json at all", "model": "m"}
        monkeypatch.setattr(llm, "generate", gen)
        assert planner.llm_rerank(cands) == cands
