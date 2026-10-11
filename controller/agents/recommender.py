"""Recommender agent: one prioritised, verifiable fix per weakness found by the Analyzer.

  fix category   from FIXES (extends the M5 analyzer vocabulary: Increase Replicas, Adjust Timeouts, Add Retries,
                 Implement Circuit Breaker), chosen by rules from the fault type and the measured evidence
  priority       severity score x (1 + annual exposure / largest annual exposure): severe AND expensive first
  money at stake the finding's projected annual exposure (insights/business.py), the amount the fix protects
  verify         re-run the same experiment with --label "<fix>"; the dashboard's run-to-run table must show a
                 significant improvement (insights/compare.py) before the fix counts as done
The LLM may rewrite the action into a more specific, service-aware instruction and pick another category from
FIXES; it cannot change priorities or numbers.
"""
import json
from typing import Any, Dict, List

from agents.base import Agent, accept_text

FIXES = ["Increase Replicas", "Adjust Timeouts", "Add Retries", "Implement Circuit Breaker",
         "Add Fallback / Graceful Degradation", "Buffer Writes in a Durable Queue"]


def _rule_fix(f: Dict[str, Any]) -> Dict[str, str]:
    svc, ev = f["service"], f["evidence"]
    cascaded = [e.split(":", 1)[0] for e in ev.get("endpoints_failing", []) if e.split(":", 1)[0] != svc]
    if f["fault"] == "container_crash":
        if cascaded:
            return {"fix": "Implement Circuit Breaker",
                    "action": f"Run at least 2 replicas of {svc}, and put a circuit breaker with a cached fallback in "
                              f"its callers ({', '.join(sorted(set(cascaded)))}) so they degrade instead of failing."}
        return {"fix": "Increase Replicas",
                "action": f"Run at least 2 replicas of {svc} behind the gateway with health checks, so one crashed "
                          f"container no longer takes the endpoint down."}
    if f["fault"] == "network_latency":
        dep = (f.get("edge") or {}).get("dependency", "its dependency")
        return {"fix": "Adjust Timeouts",
                "action": f"Give {svc}'s calls to {dep} a timeout well below the user-facing budget, with a bounded "
                          f"retry and a circuit breaker, so a slow {dep} fails fast instead of holding requests."}
    if f["fault"] == "rabbitmq_block":
        return {"fix": "Buffer Writes in a Durable Queue",
                "action": "Have payment write orders to a local outbox (retry + dead-letter queue) when the broker is "
                          "unavailable, so a queue outage delays dispatch instead of failing checkout."}
    if f["fault"] == "disk_fill":
        return {"fix": "Add Fallback / Graceful Degradation",
                "action": f"Alert on {svc} disk usage, cap logs/temp files, and let writes fail with a clear retry "
                          f"message while reads keep serving."}
    return {"fix": "Add Retries", "action": f"Add bounded retries with backoff around calls to {svc}."}


class RecommenderAgent(Agent):
    name = "recommender"
    role = "Recommends one prioritised, verifiable fix per weakness, with the money it protects"
    system = """You are the Recommender agent of a chaos-engineering platform for an e-commerce app (Stan's Robot
Shop: web, catalogue, user, cart, shipping, ratings, payment, dispatch; mongodb, mysql, redis, rabbitmq, a payment
gateway). You receive ranked weaknesses with measured evidence and a draft fix for each.
Return JSON: {"recommendations": [{"id": <weakness id>, "fix": <one of the allowed fix categories>,
"action": <1-2 sentences, a concrete change for this service and dependency path>}]}.
Rules: use only ids from the input; "fix" must be one of allowed_fixes exactly; no numbers that are not in the
input; do not change priorities."""

    def rules(self, facts):
        findings: List[Dict[str, Any]] = facts["analysis"]["findings"]
        biz = {r["signature"]: r for r in facts["business"]["runs"]}
        latest = {f["id"]: f["latest_run"] for f in findings}
        biz_by_run = {r["run_id"]: r for r in facts["business"]["runs"]}
        exposures = [biz_by_run.get(latest[f["id"]], {}).get("annual_exposure", 0) for f in findings]
        top = max(exposures) if exposures and max(exposures) > 0 else 1.0
        cur = facts["business"]["assumptions"].get("currency", "USD")
        recs = []
        for f, exp in zip(findings, exposures):
            b = biz_by_run.get(f["latest_run"], {}) or biz.get(f["id"], {})
            rf = _rule_fix(f)
            verified = next((v for v in f.get("fix_verification", []) if v["verdict"] == "improved"), None)
            if verified and verified.get("side_effect"):
                se = verified["side_effect"]
                rf = {"fix": "Adjust Timeouts",
                      "action": f"Keep the replicas ('{verified['fix']}' removed the errors) and give the gateway a short "
                                f"upstream connect timeout with retry to the next replica: during failover {se['endpoint']} "
                                f"hung at {se['peak_ms']} ms ({se['ratio']}x its baseline) before succeeding."}
            recs.append({
                "id": f["id"], "weakness_rank": f["rank"], "service": f["service"], "fault": f["fault"],
                "severity": f["severity"], **rf, "verified_fix": verified,
                "priority_score": round(f["severity_score"] * (1 + exp / top), 1),
                "protects": {"annual_exposure": exp, "cost_per_incident": b.get("cost_per_incident"),
                             "orders_lost_per_incident": b.get("orders_lost"), "currency": cur,
                             "estimated": b.get("estimated", True)},
                "verify": f"python scripts/day1_demo.py --fault {f['fault']} --target {f['target']}"
                          + (f" --params '{json.dumps(f['params'], sort_keys=True)}'" if f.get("params") else "")
                          + f" --label \"fix: {rf['fix'].lower()}\"  (after applying the fix; the Experiments tab "
                            f"then compares before vs after)",
            })
        recs.sort(key=lambda r: -r["priority_score"])
        for i, r in enumerate(recs, 1):
            r["priority"] = i
        return {"recommendations": recs, "allowed_fixes": FIXES}

    def llm_task(self, facts, draft):
        if not draft["recommendations"]:
            return None
        ev = {f["id"]: f["evidence"] for f in facts["analysis"]["findings"]}
        return {"allowed_fixes": FIXES,
                "weaknesses": [{"id": r["id"], "service": r["service"], "fault": r["fault"], "severity": r["severity"],
                                "evidence": ev.get(r["id"]), "draft_fix": r["fix"], "draft_action": r["action"]}
                               for r in draft["recommendations"]]}

    def merge(self, draft, answer, allowed, notes):
        if not isinstance(answer, dict):
            return draft
        ai = {str(r.get("id")): r for r in answer.get("recommendations", []) if isinstance(r, dict)}
        out = []
        for r in draft["recommendations"]:
            r = dict(r)
            a = ai.get(r["id"])
            if a:
                if a.get("fix") in FIXES:
                    r["fix"] = a["fix"]
                elif a.get("fix"):
                    notes.append(f"fix[{r['id']}]: ignored category {a.get('fix')!r} (not in the allowed list)")
                txt = accept_text(f"action[{r['id']}]", a.get("action"), allowed, notes, 400)
                if txt:
                    r["rule_action"], r["action"], r["written_by"] = r["action"], txt, "ai"
            out.append(r)
        return {**draft, "recommendations": out}
