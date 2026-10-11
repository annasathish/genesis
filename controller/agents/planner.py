"""Planner agent: ranks and selects chaos experiments from the dependency map, with a reason for each.

Builds only *executable* candidates, from the team's own interfaces:
  - services + dependency edges: insights/live.py EDGES (merged compose config + app defaults); M2 mapper graph.json
    node kinds are used when present
  - network edges: the Toxiproxy proxies from M1 (docs/M1_PLATFORM.md), the only valid network_latency targets
  - fault types and approved targets: M3 injectors (faults/*.py): disk_fill only on approved tmpfs mounts,
    rabbitmq_block only on rabbitmq. (Kept in sync by tests/test_agents.py.)

Score (0-100) = 100 x (0.40 x revenue proximity + 0.20 x blast radius + 0.25 x novelty + 0.15 x new fault type)
  revenue proximity  1 / (1 + hops from the payment step in the dependency graph): payment = 1, its direct
                     dependencies 0.5, ... (payment:pay is the revenue event of the shop)
  blast radius       services that break if the impacted service fails / all services
  novelty            1 = never tested, 0.3 = service tested with another fault, 0 = this exact experiment ran
  new fault type     1 if this fault type has never been run
Selection: highest score first, at most one experiment per fault type, until the experiment budget
(MAX_EXPERIMENTS distinct experiments in total, already-run ones included) is used.
"""
import json
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, List

from agents.base import Agent, accept_text, by_id
from insights.live import EDGES, SERVICES, callers_of

MAX_EXPERIMENTS = 4
REVENUE_STEP = "payment"

# M1 Toxiproxy edges (docs/M1_PLATFORM.md "Toxiproxy edges"): proxy name = <caller>_to_<dependency>
PROXIES = ["catalogue_to_mongodb", "user_to_mongodb", "cart_to_redis", "cart_to_catalogue", "shipping_to_mysql",
           "ratings_to_mysql", "shipping_to_cart", "payment_to_rabbitmq", "payment_to_gateway"]
# M3 injector constraints (faults/disk_stress.py APPROVED_MOUNTS, faults/rabbitmq_block.py APPROVED_TARGETS)
DISK_TARGETS = {"mongodb": "ok", "mysql": "skip: disk_stress.py points mysql at /var/lib/mysql, which is not tmpfs "
                                             "here (M1 issue 6), so the injector always refuses"}
RABBIT_TARGETS = ["rabbitmq"]
CRASH_EXCLUDE = {"web": "gateway: crashing it takes the whole shop down, so it measures nothing about resilience"}
PARAMS = {"network_latency": {"latency_ms": 2000, "jitter_ms": 200}, "disk_fill": {"fill_mb": 120}}
GRAPH_FILE = Path(__file__).resolve().parents[1] / "graph.json"
METHOD = ("score = 100 x (0.40 x revenue proximity [1/(1+hops from payment)] + 0.20 x blast radius [services that "
          "break / all services] + 0.25 x novelty [1 never tested, 0.3 service tested with another fault, 0 already run] "
          "+ 0.15 x new fault type); ties go to the experiment closer to payment; select the highest, one per fault "
          "type, up to %d experiments in total" % MAX_EXPERIMENTS)


def _hops_to_revenue() -> Dict[str, int]:
    """Undirected hop distance from the payment step over the dependency edges."""
    adj: Dict[str, set] = {s: set() for s in SERVICES}
    for a, b in EDGES:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    dist, q = {REVENUE_STEP: 0}, deque([REVENUE_STEP])
    while q:
        n = q.popleft()
        for m in adj.get(n, ()):
            if m not in dist:
                dist[m] = dist[n] + 1
                q.append(m)
    return dist


def _kinds() -> Dict[str, str]:
    try:
        g = json.loads(GRAPH_FILE.read_text(encoding="utf-8"))
        return {n["id"]: n.get("kind", "") for n in g.get("nodes", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def candidates() -> List[Dict[str, Any]]:
    out = []
    for s in SERVICES:
        if s in CRASH_EXCLUDE:
            continue
        out.append({"fault": "container_crash", "target": s, "service": s})
    for p in PROXIES:
        caller, _, dep = p.partition("_to_")
        out.append({"fault": "network_latency", "target": p, "service": caller, "edge": {"caller": caller, "dependency": dep}})
    for t, status in DISK_TARGETS.items():
        if status == "ok":
            out.append({"fault": "disk_fill", "target": t, "service": t})
    for t in RABBIT_TARGETS:
        out.append({"fault": "rabbitmq_block", "target": t, "service": t})
    for c in out:
        c["params"] = PARAMS.get(c["fault"], {})
        c["id"] = f"{c['fault']}:{c['target']}"
    return out


def _reason(c: Dict[str, Any], hops: int, breaks: List[str], tested: str, new_type: bool) -> str:
    if c.get("edge"):
        subject = f"the {c['edge']['caller']} -> {c['edge']['dependency']} call"
        where = "sits on the revenue step (payment)" if hops == 0 else f"is {hops} hop{'s' if hops > 1 else ''} from payment"
    else:
        subject = c["service"]
        where = "is the revenue step" if hops == 0 else f"is {hops} hop{'s' if hops > 1 else ''} from payment"
    parts = [f"{subject} {where}"]
    parts.append(f"if {c['service']} fails it breaks {', '.join(breaks)}" if breaks else "no other service calls it")
    parts.append({"never": "never tested", "service": "service tested only with other faults",
                  "exact": "already run"}[tested])
    if new_type:
        parts.append(f"adds a new failure type ({c['fault']})")
    s = "; ".join(parts) + "."
    return s[0].upper() + s[1:]


def score_all(metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    hops = _hops_to_revenue()
    kinds = _kinds()
    run_sigs = {(m["fault"], m["target"]) for m in metrics}
    run_services = {m.get("service", m["target"]) for m in metrics}
    run_faults = {m["fault"] for m in metrics}
    rows = []
    for c in candidates():
        svc = c["service"]
        breaks = callers_of(svc)
        h = hops.get(svc, len(SERVICES))
        tested = "exact" if (c["fault"], c["target"]) in run_sigs else "service" if svc in run_services else "never"
        comp = {
            "revenue_proximity": round(1 / (1 + h), 3),
            "blast_radius": round(len(breaks) / (len(SERVICES) - 1), 3),
            "novelty": {"never": 1.0, "service": 0.3, "exact": 0.0}[tested],
            "new_fault_type": 1.0 if c["fault"] not in run_faults else 0.0,
        }
        score = 100 * (0.40 * comp["revenue_proximity"] + 0.20 * comp["blast_radius"] + 0.25 * comp["novelty"]
                       + 0.15 * comp["new_fault_type"])
        rows.append({**c, "score": round(score, 1), "components": comp, "hops_to_payment": h, "breaks": breaks,
                     "kind": kinds.get(svc, ""), "already_run": tested == "exact",
                     "reason": _reason(c, h, breaks, tested, comp["new_fault_type"] == 1.0)})
    rows.sort(key=lambda r: (-r["score"], -r["components"]["revenue_proximity"], r["id"]))
    return rows


def select(ranked: List[Dict[str, Any]], already_run: int, budget: int = MAX_EXPERIMENTS) -> List[Dict[str, Any]]:
    picked, types = [], set()
    for r in ranked:
        if already_run + len(picked) >= budget:
            break
        if r["already_run"] or r["fault"] in types:
            continue
        picked.append(r)
        types.add(r["fault"])
    return picked


def campaign(metrics: List[Dict[str, Any]], size: int = MAX_EXPERIMENTS) -> List[Dict[str, Any]]:
    """The autonomous campaign: `size` experiments, no human picking.

    Recorded experiments are re-run first (that is what makes run-to-run comparison possible), then the budget is
    filled with the Planner's selection. Order = the Planner's dependency-map score without the novelty bonus, so the
    campaign always walks the revenue path in the same order (most valuable first).
    """
    fresh = {r["id"]: r for r in score_all([])}           # ranking from the dependency map alone
    recorded = []
    for m in metrics:                                     # one entry per recorded experiment (latest params win)
        cid = f"{m['fault']}:{m['target']}"
        if cid in fresh and cid not in [c["id"] for c in recorded]:
            recorded.append(dict(fresh[cid], params=m.get("params") or fresh[cid]["params"], recorded=True,
                                 reason=re.sub(r"; (never tested|adds a new failure type \([a-z_]+\))", "",
                                               fresh[cid]["reason"])))
    plan = recorded[:size]
    if len(plan) < size:
        ranked = score_all(metrics)
        for r in select(ranked, already_run=0, budget=size):
            if len(plan) >= size:
                break
            if r["id"] not in [c["id"] for c in plan]:
                plan.append(dict(r, recorded=False))
    plan.sort(key=lambda c: (-fresh[c["id"]]["score"], c["id"]))
    return plan


class PlannerAgent(Agent):
    name = "planner"
    role = "Ranks and selects chaos experiments from the dependency map, one reason per experiment"
    system = """You are the Planner agent of a chaos-engineering platform for an e-commerce app.
You receive executable candidate experiments that Python already scored from the dependency map.
Return JSON: {"ranking": [{"id": <candidate id>, "reason": <one sentence, why this experiment is worth running:
what it should reveal about the checkout/revenue path and which services it can break>}]}.
Rules: use only ids from the input; rank the most valuable first; keep each reason under 30 words; mention only
services and numbers that appear in the input; never invent metrics."""

    def rules(self, facts):
        ranked = score_all(facts["metrics"])
        already = len({(m["fault"], m["target"]) for m in facts["metrics"]})
        return {"ranked": ranked, "selected": select(ranked, already), "already_run": already,
                "budget": MAX_EXPERIMENTS, "method": METHOD}

    def llm_task(self, facts, draft):
        top = [{k: r[k] for k in ("id", "fault", "target", "service", "score", "components", "hops_to_payment",
                                  "breaks", "already_run")} for r in draft["ranked"][:10]]
        return {"candidates": top, "already_run_experiments": draft["already_run"], "budget": draft["budget"]}

    def merge(self, draft, answer, allowed, notes):
        items = answer.get("ranking") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            notes.append("planner: AI answer had no ranking list")
            return draft
        known = by_id(draft["ranked"])
        order, seen = [], set()
        for it in items:
            cid = str(it.get("id")) if isinstance(it, dict) else None
            if cid in known and cid not in seen:
                seen.add(cid)
                row = dict(known[cid])
                txt = accept_text(f"reason[{cid}]", it.get("reason"), allowed, notes, 300)
                if txt:
                    row["ai_reason"] = txt
                order.append(row)
            elif cid is not None and cid not in known:
                notes.append(f"planner: ignored unknown candidate id {cid!r}")
        if not order:
            return draft
        ai_rank = order + [r for r in draft["ranked"] if r["id"] not in seen]
        for i, r in enumerate(ai_rank, 1):
            r["ai_rank"] = i
        return {**draft, "ranked": ai_rank, "selected": select(ai_rank, draft["already_run"]), "ranked_by": "ai"}
