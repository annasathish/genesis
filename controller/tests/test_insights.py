"""Tests for controller/insights: PII masking, run comparison, business impact, chat, dashboard API."""
import json
import math

import pytest

from insights import business, chat, compare, live, llm, metrics, pii, runs, vulnerability


# ---------------------------------------------------------------------------------------------- helpers
def make_run(run_id, err_pct, n_per_window=100, windows=6, recover_windows=2, label="", fault="container_crash",
             target="catalogue", ttd=15.0, ttr=10.0, checkouts=None, base_checkouts=None):
    """A synthetic v1/v2 run: `windows` fault windows at err_pct, then healthy recovery windows."""
    ws = []
    for i in range(windows):
        w = {"t": 5.0 * (i + 1), "phase": "fault", "req": n_per_window, "err": err_pct / 100, "p95": 40,
             "failing": ["cart:add"] if err_pct else [], "down": []}
        if checkouts is not None:
            w["ep"] = {"payment:pay": [checkouts[i], 0], "cart:add": [10, 5]}
        ws.append(w)
    for j in range(recover_windows):
        w = {"t": 5.0 * (windows + j + 1), "phase": "recovery", "req": n_per_window, "err": 0.0, "p95": 35,
             "failing": [], "down": []}
        if checkouts is not None:
            w["ep"] = {"payment:pay": [base_checkouts or 0, 0]}
        ws.append(w)
    raw = {"target": target, "fault": fault, "label": label, "windows": ws,
           "baseline": {"requests": 600, "error_rate": 0.0, "p95_ms": 35, "threshold": 0.05},
           "summary": {"endpoints_failing": ["cart:add"], "services_down": [f"robotshop-{target}-1"],
                       "time_to_detect_s": ttd, "time_to_recover_s": ttr, "recovered": True}}
    if base_checkouts is not None:
        raw["baseline"].update({"duration_s": 30.0, "ep": {"payment:pay": [base_checkouts * 6, 0]}})
        raw["window_s"] = 5.0
    return runs.normalise(raw, run_id, f"2026-10-10T10:00:{len(run_id):02d}")


# ---------------------------------------------------------------------------------------------- PII
class TestPII:
    def setup_method(self):
        self.m = pii.Masker(salt="test")

    @pytest.mark.parametrize("text,leak", [
        ("contact jane.doe@example.com now", "jane.doe@example.com"),
        ("call +91 98765 43210", "98765 43210"),
        ("call 9876543210", "9876543210"),
        ("call (555) 123-4567", "123-4567"),
        ("card 4111 1111 1111 1111", "4111 1111 1111"),
        ("from 10.0.4.17", "10.0.4.17"),
        ("cart a1b2c3d4-e5f6-4711-8899-aabbccddeeff", "a1b2c3d4-e5f6"),
        ("password=hunter2", "hunter2"),
        ('{"api_key": "abc123secret"}', "abc123secret"),
        ("key AIzaSyA1234567890abcdefghijklmnopqrstu", "AIzaSyA1234567890"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123", "abcdefghijklmnopqrstuvwxyz123"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "eyJhbGciOiJIUzI1NiJ9"),
        ("aadhaar 2345 6789 0123", "6789 0123"),
        ("PAN ABCDE1234F", "ABCDE1234F"),
        ("ssn 123-45-6789", "123-45-6789"),
    ])
    def test_detects_and_hides(self, text, leak):
        out = self.m.mask_text(text)
        assert leak not in out, out

    def test_card_keeps_last4_and_requires_luhn(self):
        assert "[CARD-1111]" in self.m.mask_text("4111 1111 1111 1111")
        # 16 digits failing Luhn is not a card (e.g. a counter): left alone by the card rule
        assert "1234567812345678" in self.m.mask_text("id 1234567812345678")

    def test_pseudonyms_are_stable_and_salted(self):
        a = self.m.mask_text("x@y.com")
        assert a == self.m.mask_text("x@y.com")
        assert a != pii.Masker(salt="other").mask_text("x@y.com")
        assert a.startswith("email_") and a.endswith("@masked")

    def test_sensitive_keys_redacted_but_name_kept(self):
        out = self.m.mask_obj({"password": "p", "email": "e@x.io", "name": "cart:add",
                               "nested": [{"token": "t", "target": "cart"}]})
        assert out["password"] == pii.REDACTED and out["email"] == pii.REDACTED
        assert out["name"] == "cart:add"
        assert out["nested"][0] == {"token": pii.REDACTED, "target": "cart"}

    def test_no_false_positives_on_run_data(self):
        raw = {"target": "catalogue", "fault": "container_crash", "started_at": "2026-10-10T15:36:55",
               "windows": [{"t": 56.0, "req": 638, "err": 0.0216, "p95": 36, "failing": ["catalogue:product"],
                            "down": ["robotshop-catalogue-1"]}],
               "rollback_command": "docker start robotshop-catalogue-1", "intent_id": 12}
        assert self.m.mask_obj(raw) == raw
        assert self.m.report()["total_masked"] == 0

    def test_stats_count_categories(self):
        self.m.mask_text("a@b.com and 10.1.1.1")
        assert self.m.report() == {"total_masked": 2, "by_category": {"email": 1, "ip": 1}}


# ---------------------------------------------------------------------------------------------- compare
class TestCompare:
    def test_evaluator_example_50_30_20_10(self):
        rs = [make_run(f"r{i}", e, n_per_window=400) for i, e in enumerate([50, 30, 20, 10])]
        c = compare.compare_runs(rs)
        (g,) = c["groups"]
        rels = [s["primary_relative_change"] for s in g["steps"]]
        assert rels == pytest.approx([0.40, 1 / 3, 0.50], abs=1e-3)
        assert [s["verdict"] for s in g["steps"]] == ["improved"] * 3
        assert g["cumulative"]["primary_relative_change"] == pytest.approx(0.80)
        assert g["cumulative"]["verdict"] == "improved"

    def test_small_samples_are_noise(self):
        rs = [make_run("a", 12, n_per_window=5, windows=2), make_run("b", 9, n_per_window=5, windows=2)]
        (step,) = compare.compare_runs(rs)["groups"][0]["steps"]
        assert step["primary_relative_change"] > compare.MIN_EFFECT
        assert step["significance"]["p_value"] > compare.ALPHA
        assert step["verdict"] == "no significant change"

    def test_regression(self):
        rs = [make_run("a", 10, n_per_window=400), make_run("b", 40, n_per_window=400)]
        assert compare.compare_runs(rs)["groups"][0]["steps"][0]["verdict"] == "regressed"

    def test_mixed_when_shorter_but_worse(self):
        # shorter outage (fewer windows) but a higher error rate while it lasted
        rs = [make_run("a", 20, n_per_window=400, windows=10), make_run("b", 40, n_per_window=400, windows=3)]
        step = compare.compare_runs(rs)["groups"][0]["steps"][0]
        assert step["primary_relative_change"] > 0
        assert step["verdict"] == "mixed"

    def test_only_like_for_like(self):
        rs = [make_run("a", 50), make_run("b", 10, target="cart")]
        groups = compare.compare_runs(rs)["groups"]
        assert len(groups) == 2 and all(not g["steps"] for g in groups)

    def test_time_kpi_resolution(self):
        d = compare.kpi_delta("time_to_recover_s", 15.3, 15.4)
        assert d["better"] is None and "resolution" in d["note"]
        assert compare.kpi_delta("time_to_detect_s", 56.0, 15.4)["better"] is True

    def test_z_test_known_value(self):
        r = compare.two_proportion_z(50, 100, 30, 100)
        p = (80) / 200
        z = (0.5 - 0.3) / math.sqrt(p * (1 - p) * (2 / 100))
        assert r["z"] == pytest.approx(z, abs=1e-3)
        assert r["p_value"] == pytest.approx(math.erfc(z / math.sqrt(2)), abs=1e-5)
        assert compare.two_proportion_z(0, 0, 1, 10) == {"z": None, "p_value": None}


# ---------------------------------------------------------------------------------------------- metrics / business
class TestBusiness:
    def test_error_minutes_and_estimated_checkout(self):
        m = metrics.run_metrics(make_run("a", 50, windows=6))  # 6 windows x 5 s x 50 % = 15 s = 0.25 min
        assert m["error_minutes"] == pytest.approx(0.25)
        assert m["checkout_method"] == "estimated_from_aggregate_errors"
        assert m["checkout_loss_min"] == pytest.approx(0.25)
        assert m["fault_error_rate"] == pytest.approx(0.5)

    def test_measured_checkout_loss_counts_checkouts_never_attempted(self):
        # baseline 2 checkouts / window; during the fault payment:pay is never even called (0 requests)
        m = metrics.run_metrics(make_run("a", 50, windows=4, checkouts=[0, 0, 1, 2], base_checkouts=2))
        assert m["checkout_method"] == "measured"
        assert m["sandbox_checkouts_lost"] == pytest.approx(5.0)        # 2 + 2 + 1 + 0
        assert m["checkout_loss_min"] == pytest.approx(12.5 / 60, abs=1e-3)  # 5 + 5 + 2.5 + 0 seconds

    def test_formulas_by_hand(self):
        m = metrics.run_metrics(make_run("a", 50, windows=6))
        a = business.load_assumptions({"orders_per_minute": 100, "avg_order_value": 40, "customer_retry_rate": 0.5,
                                       "incident_cost_per_minute": 10, "incidents_per_year": 4,
                                       "slo_target": 0.999, "slo_window_days": 30})
        r = business.impact(m, a)
        assert r["orders_lost"] == pytest.approx(25.0)          # 0.25 min x 100
        assert r["revenue_at_risk"] == pytest.approx(1000.0)    # x 40
        assert r["revenue_lost"] == pytest.approx(500.0)        # x (1 - 0.5)
        assert r["ops_cost"] == pytest.approx(m["incident_s"] / 60 * 10, abs=0.01)
        assert r["support_tickets"] == pytest.approx(25 * 0.5 * a["support_ticket_rate"], abs=0.05)
        assert r["cost_per_incident"] == pytest.approx(r["revenue_lost"] + r["ops_cost"] + r["support_cost"], abs=0.02)
        assert r["annual_exposure"] == pytest.approx(r["cost_per_incident"] * 4, abs=0.01)
        assert r["error_budget_minutes"] == pytest.approx(43.2)
        assert r["slo_budget_burn_pct"] == pytest.approx(0.25 / 43.2 * 100, abs=0.1)

    def test_savings_between_runs(self):
        ms = [metrics.run_metrics(make_run(f"r{i}", e)) for i, e in enumerate([50, 10])]
        s = business.business_summary(ms)
        (t,) = s["trends"]
        assert t["cumulative"]["saved_per_incident"] > 0
        assert t["cumulative"]["orders_saved_per_incident"] > 0

    def test_overrides_are_clamped(self):
        a = business.load_assumptions({"customer_retry_rate": 3, "slo_target": 2, "bogus": 1, "currency": "INR"})
        assert a["customer_retry_rate"] == 1.0 and a["slo_target"] < 1 and "bogus" not in a
        assert a["currency"] == "INR"


# ---------------------------------------------------------------------------------------------- runs loader
def test_load_runs_masks_and_skips_bad_files(tmp_path):
    good = {"target": "cart", "fault": "container_crash", "label": "ping ops@shop.io", "windows": [],
            "baseline": {}, "summary": {}}
    (tmp_path / "day1_20261010_100000.json").write_text(json.dumps(good))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps({"no": "windows"}))
    loaded = runs.load_runs(tmp_path, pii.Masker(salt="t"))
    assert [r["run_id"] for r in loaded] == ["day1_20261010_100000"]
    assert "ops@shop.io" not in loaded[0]["label"]
    assert loaded[0]["started_at"] == "2026-10-10T10:00:00"


# ---------------------------------------------------------------------------------------------- chat
def _facts():
    rs = [make_run(f"r{i}", e, n_per_window=400) for i, e in enumerate([50, 30, 20, 10])]
    c = compare.compare_runs(rs)
    return {"metrics": c["metrics"], "groups": c["groups"], "method": c["method"],
            "business": business.business_summary(c["metrics"]), "pii": {"total_masked": 0}}


class TestChat:
    def test_offline_intents(self):
        bot = chat.Copilot(_facts(), api_key="")
        assert "IMPROVED" in bot.ask("did it improve between runs?")["answer"]
        assert "z-test" in bot.ask("how is improvement calculated?")["answer"]
        assert "orders lost" in bot.ask("what is the business impact?")["answer"]
        assert "pseudonymized" in bot.ask("how do you protect personal data?")["answer"]
        assert "Run r1" in bot.ask("tell me about run 2")["answer"]
        assert bot.ask("what should we fix?")["answer"].startswith("Recommended")
        assert bot.ask("anything")["mode"] == "offline"

    def test_question_is_masked(self):
        r = chat.Copilot(_facts(), api_key="").ask("I am bob@corp.com, hi")
        assert "bob@corp.com" not in r["masked_question"]

    def test_llm_payload_is_masked_and_errors_fall_back(self, monkeypatch):
        facts = _facts()
        facts["metrics"][0]["label"] = "fixed by alice@corp.com"
        sent = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"candidates": [{"content": {"parts": [{"text": "mail eve@x.com"}]}}]}).encode()

        def fake_urlopen(req, timeout):
            sent["body"] = req.data.decode()
            sent["headers"] = dict(req.headers)
            return Resp()

        monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
        r = chat.Copilot(facts, api_key="k").ask("why did carol@corp.com see errors?")
        assert r["mode"] == "llm"
        for leak in ("alice@corp.com", "carol@corp.com"):
            assert leak not in sent["body"]
        assert "key=" not in sent.get("url", "")  # key travels in a header, not the URL
        assert "eve@x.com" not in r["answer"]

        assert sent["headers"].get("X-goog-api-key") == "k"

        def boom(req, timeout):
            raise llm.urllib.error.URLError("offline")
        monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
        r = chat.Copilot(facts, api_key="k").ask("did it improve?")
        assert r["mode"] == "offline" and "IMPROVED" in r["answer"]
        assert "cannot reach Gemini" in r["note"]

    def test_llm_http_errors_are_readable_and_404_discovers_newer_model(self, monkeypatch):
        import io
        calls = []
        monkeypatch.setattr(llm, "_working_model", None)
        monkeypatch.delenv("CHAT_MODEL", raising=False)

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout):
            url = req.full_url
            calls.append(url)
            if url.endswith("pageSize=200"):  # ListModels
                return Resp(json.dumps({"models": [
                    {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]}).encode())
            if "gemini-flash-latest" in url:
                raise llm.urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO(b'{"error":{"message":"gone"}}'))
            raise llm.urllib.error.HTTPError(url, 400, "bad", {},
                                             io.BytesIO(b'{"error":{"message":"API key not valid"}}'))
        monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
        with pytest.raises(llm.LLMError, match="API key rejected.*API key not valid"):
            llm.generate("sys", [{"role": "user", "parts": [{"text": "hi"}]}], key="bad")
        tried = [u.split("/models/")[1].split(":")[0] for u in calls if ":generateContent" in u]
        assert tried == ["gemini-flash-latest", "gemini-3.8-flash"]  # newest discovered flash, no tts variant
        with pytest.raises(llm.LLMError, match="no GEMINI_API_KEY"):
            llm.generate("sys", [], key="")

    def test_llm_success_reports_model_version(self, monkeypatch):
        import io
        monkeypatch.setattr(llm, "_working_model", None)

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: Resp(json.dumps({
            "modelVersion": "gemini-3.8-flash",
            "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}).encode()))
        assert llm.generate("s", [], key="k") == {"text": "OK", "model": "gemini-3.8-flash"}

    def test_live_intent_and_brief(self):
        facts = _facts()
        facts["live"] = {"status": {"level": "injecting", "text": "INJECTING FAILURE: container_crash on cart"},
                         "window": {"err": 0.4, "req": 50, "rps": 9, "p95_ms": 900},
                         "services": {"cart": {"failing": True}}, "kill_switch": {"tripped": True},
                         "active_faults": [{"fault": "container_crash", "target": "cart"}]}
        ans = chat.Copilot(facts, api_key="").ask("what is happening right now?")["answer"]
        assert "INJECTING" in ans and "cart" in ans and "TRIPPED" in ans
        m = facts["metrics"][0]
        b = facts["business"]["runs"][0]
        r = chat.run_brief(m, b, facts["business"]["assumptions"], key="")
        assert r["mode"] == "offline" and "What happened:" in r["brief"] and "Recommendation:" in r["brief"]
        assert str(b["orders_lost"]) in r["brief"]


# ---------------------------------------------------------------------------------------------- dashboard API
def test_dashboard_api(tmp_path):
    from fastapi.testclient import TestClient
    from insights.dashboard.server import create_app

    for i, e in enumerate([50, 10]):
        r = make_run(f"x{i}", e, n_per_window=400)
        raw = {"target": r["target"], "fault": r["fault"], "windows": r["windows"],
               "baseline": r["baseline"], "summary": r["summary"]}
        (tmp_path / f"day1_20261010_10000{i}.json").write_text(json.dumps(raw))
    class DeadMonitor(live.LiveMonitor):  # every live source unreachable
        def _locust(self): return {"ok": False, "error": "URLError"}
        def _intents(self): return {"ok": False, "error": "URLError", "active": []}
        def _toxics(self): return {"ok": False, "error": "URLError", "active": []}
    dead = DeadMonitor(docker_cmd=("definitely-not-docker",))
    c = TestClient(create_app(runs_dir=tmp_path, api_key="", monitor=dead))
    assert c.get("/api/health").json() == {"status": "ok", "llm": False, "model": None}
    ov = c.get("/api/overview").json()
    assert len(ov["metrics"]) == 2 and ov["groups"][0]["steps"][0]["verdict"] == "improved"
    biz = c.post("/api/business", json={"orders_per_minute": 1000}).json()
    assert biz["assumptions"]["orders_per_minute"] == 1000
    ans = c.post("/api/chat", json={"question": "did it improve?", "history": []}).json()
    assert ans["mode"] == "offline" and "IMPROVED" in ans["answer"]
    assert "pii" not in ov  # masking is internal, not a dashboard feature
    assert c.post("/api/pii/preview", json={"text": "a@b.com"}).status_code in (404, 405)
    lv = c.get("/api/live").json()
    assert lv["status"]["level"] == "unknown" and lv["sources"]["locust"] is False   # no fake numbers when down
    assert lv["window"] is None and lv["vulnerability"]["rows"][0]["service"] == "catalogue"
    br = c.post("/api/brief/day1_20261010_100000").json()
    assert br["mode"] == "offline" and "What happened" in br["brief"]
    assert c.post("/api/brief/nope").status_code == 404
    assert c.get("/").status_code == 200 and "Resilience" in c.get("/").text


# ---------------------------------------------------------------------------------------------- live + vulnerability
def test_live_window_math_and_kill_switch(monkeypatch):
    mon = live.LiveMonitor(docker_cmd=("definitely-not-docker",))
    samples = iter([
        {"Aggregated": (100, 0), "cart:add": (40, 0), "web:index": (60, 0)},
        {"Aggregated": (200, 30), "cart:add": (80, 30), "web:index": (120, 0)},
        {"Aggregated": (300, 60), "cart:add": (120, 60), "web:index": (180, 0)},
    ])
    monkeypatch.setattr(mon, "_locust", lambda: {"ok": True, "rows": next(samples), "rps": 20, "p95_ms": 50, "users": 10})
    monkeypatch.setattr(mon, "_intents", lambda: {"ok": True, "active": [{"id": 1, "fault": "container_crash", "target": "cart"}]})
    monkeypatch.setattr(mon, "_toxics", lambda: {"ok": True, "active": []})
    monkeypatch.setattr(live, "_load_thresholds", lambda: {"max_error_rate": 0.05, "p95_latency_threshold_ms": 165.0,
                                                           "consecutive_breach_windows": 2, "source": "test"})
    first = mon.sample(force=True)
    assert first["window"] is None                       # needs two samples for a delta
    second = mon.sample(force=True)
    assert second["window"]["err"] == pytest.approx(0.30)
    assert second["services"]["cart"]["live_error_rate"] == pytest.approx(0.75)
    assert second["services"]["cart"]["failing"] and not second["services"]["web"]["failing"]
    assert second["kill_switch"]["breach_windows"] == 1 and not second["kill_switch"]["tripped"]
    third = mon.sample(force=True)
    assert third["kill_switch"]["tripped"]
    assert third["status"]["text"] == "INJECTING FAILURE: container_crash on cart"
    assert len(third["history"]) == 2


def test_callers_of_is_transitive():
    assert set(live.callers_of("catalogue")) >= {"cart", "ratings", "web", "shipping", "payment"}
    assert live.callers_of("web") == []


def test_vulnerability_scoreboard():
    ms = [metrics.run_metrics(make_run("a", 100, windows=6)), metrics.run_metrics(make_run("b", 50, windows=6))]
    sb = vulnerability.scoreboard(ms, {"services": {"redis": {"failing": True}}})
    top = sb["rows"][0]
    assert top["service"] == "catalogue" and top["runs"] == 2
    exp = 100 * (0.4 * 0.75 + 0.3 * mean_recovery(ms) + 0.3 * (1 / 7))
    assert top["score"] == pytest.approx(exp, abs=0.1)
    redis = next(r for r in sb["rows"] if r["service"] == "redis")
    assert redis["score"] is None and redis["status"] == "UNTESTED" and redis["live_failing"]
    assert "cart" in redis["cascade"] and redis["cascade_source"].startswith("predicted")
    assert sb["coverage"]["tested"] == 1


def mean_recovery(ms):
    return sum(min(1.0, m["incident_s"] / vulnerability.RECOVERY_CAP_S) for m in ms) / len(ms)


def test_stakeholder_rules():
    m = metrics.run_metrics(make_run("a", 60))
    st = business.stakeholder(m)
    assert st["churn_risk"] == "HIGH" and st["checkout_flow_hit"]
    assert next(f for f in st["funnel"] if f["stage"] == "Add to cart")["blocked"] == ["cart:add"]
    m["endpoints_failing"] = ["ratings:rate"]
    m["peak_error_rate"] = 0.1
    assert business.stakeholder(m)["churn_risk"] == "LOW"


def test_env_file_last_nonempty_key_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("\ufeff# template\nGEMINI_API_KEY=\nUSE_LLM=true\n\nGEMINI_API_KEY=\"AQ.realkey\"\n", encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "ENV_FILE", env)
    assert llm.api_key() == "AQ.realkey"
    env.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    assert llm.api_key() == ""


def test_new_google_key_format_is_masked():
    out = pii.Masker(salt="t").mask_text("key AQ.Ab8RN6Lx-abcdefghijklmnopqrstuvwxyz0123456789 end")
    assert "AQ.Ab8RN6" not in out and pii.REDACTED in out


def test_significance_question_routes_to_comparison():
    ans = chat.Copilot(_facts(), api_key="").ask("Did resilience improve between runs? Is the improvement significant?")
    assert "IMPROVED" in ans["answer"]


def test_hanging_model_falls_through_to_next_and_is_remembered(monkeypatch):
    import io
    monkeypatch.setattr(llm, "_working_model", None)
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout):
        url = req.full_url
        calls.append(url)
        if url.endswith("pageSize=200"):
            return Resp(json.dumps({"models": [
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]}]}).encode())
        if "gemini-flash-latest" in url or "gemini-3.8-flash" in url:
            raise TimeoutError("The read operation timed out")
        return Resp(json.dumps({"modelVersion": "gemini-3.6-flash",
                                "candidates": [{"content": {"parts": [{"text": "OK"}]}}]}).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    assert llm.generate("s", [], key="k") == {"text": "OK", "model": "gemini-3.6-flash"}
    tried = [u.split("/models/")[1].split(":")[0] for u in calls if ":generateContent" in u]
    assert tried == ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.5-flash"]
    assert llm.configured_model() == "gemini-3.5-flash"   # next call goes straight to the working model
    calls.clear()
    llm.generate("s", [], key="k")
    assert [u for u in calls if ":generateContent" in u][0].endswith("gemini-3.5-flash:generateContent")


def test_network_down_is_fatal_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "_working_model", None)
    def down(req, timeout):
        raise llm.urllib.error.URLError(OSError("Name or service not known"))
    monkeypatch.setattr(llm.urllib.request, "urlopen", down)
    with pytest.raises(llm.LLMError, match="cannot reach Gemini"):
        llm.generate("s", [], key="k")


def test_quota_exhausted_moves_to_next_model(monkeypatch):
    import io
    monkeypatch.setattr(llm, "_working_model", None)
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout):
        url = req.full_url
        if url.endswith("pageSize=200"):
            return Resp(json.dumps({"models": [{"name": "models/gemini-3.5-flash",
                                                "supportedGenerationMethods": ["generateContent"]}]}).encode())
        if "gemini-flash-latest" in url:
            raise llm.urllib.error.HTTPError(url, 429, "q", {}, io.BytesIO(
                b'{"error":{"message":"You exceeded your current quota"}}'))
        return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}).encode())
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    assert llm.generate("s", [], key="k")["model"] == "gemini-3.5-flash"
