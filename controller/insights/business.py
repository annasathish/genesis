"""Business impact of an experiment: what this failure would cost if it happened in production.

Inputs: measured per-run KPIs (insights.metrics) + explicit assumptions (business_config.json).
Every output names the formula that produced it, so a reviewer can check the numbers by hand.

  orders_lost          = checkout_loss_min * orders_per_minute
  revenue_at_risk      = orders_lost * avg_order_value                     (gross: every failed checkout)
  revenue_lost         = revenue_at_risk * (1 - customer_retry_rate)        (net: customers who never come back)
  ops_cost             = incident_minutes * incident_cost_per_minute         (on-call + engineering time)
  support_tickets      = orders_lost * (1 - customer_retry_rate) * support_ticket_rate
  support_cost         = support_tickets * cost_per_ticket
  cost_per_incident    = revenue_lost + ops_cost + support_cost
  annual_exposure      = cost_per_incident * incidents_per_year
  slo_budget_burn_pct  = error_minutes / error_budget_minutes * 100
                         error_budget_minutes = slo_window_days * 1440 * (1 - slo_target)   (99.9 %/30 d = 43.2 min)

Stakeholder view (rules, not guesses):
  churn_risk     HIGH   checkout flow blocked (payment/cart/shipping endpoints failed) and peak error >= 50 %
                 MEDIUM any checkout-flow endpoint failed, or peak error >= 20 %, or p95 > 2000 ms
                 LOW    otherwise
  funnel_impact  which shopper journey stages had failing endpoints during the run (browse, login, cart, checkout)

Between two runs of the same experiment, savings = old cost - new cost: this is how a resilience fix is
expressed in money ("adding retries to cart cut cost per incident from X to Y, Z per year").
"""
import json
from pathlib import Path
from typing import Any, Dict, List

CONFIG_PATH = Path(__file__).with_name("business_config.json")
NUMERIC_KEYS = ("avg_order_value", "orders_per_minute", "customer_retry_rate", "incident_cost_per_minute",
                "incidents_per_year", "slo_target", "slo_window_days", "support_ticket_rate", "cost_per_ticket")

# Shopper journey stage -> Locust endpoints (sandbox/locust/locustfile.py)
FUNNEL = [
    ("Browse catalogue", ["web:index", "catalogue:categories", "catalogue:products", "catalogue:product", "ratings:fetch"]),
    ("Rate products", ["ratings:rate"]),
    ("Login", ["user:login", "user:uniqueid"]),
    ("Add to cart", ["cart:add"]),
    ("Checkout (shipping + payment)", ["shipping:codes", "shipping:match", "shipping:calc", "shipping:confirm",
                                       "payment:pay"]),
]
CHECKOUT_FLOW = {"cart:add", "shipping:codes", "shipping:match", "shipping:calc", "shipping:confirm", "payment:pay",
                 "user:uniqueid"}


def load_assumptions(overrides: Dict[str, Any] = None) -> Dict[str, Any]:
    cfg = {k: v for k, v in json.loads(CONFIG_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    for k, v in (overrides or {}).items():
        if k in NUMERIC_KEYS and v is not None:
            cfg[k] = float(v)
        elif k == "currency" and isinstance(v, str) and v.strip():
            cfg[k] = v.strip()[:5]
    cfg.setdefault("support_ticket_rate", 0.15)
    cfg.setdefault("cost_per_ticket", 6.0)
    cfg["customer_retry_rate"] = min(1.0, max(0.0, float(cfg["customer_retry_rate"])))
    cfg["support_ticket_rate"] = min(1.0, max(0.0, float(cfg["support_ticket_rate"])))
    cfg["slo_target"] = min(0.99999, max(0.5, float(cfg["slo_target"])))
    return cfg


def error_budget_minutes(a: Dict[str, Any]) -> float:
    return a["slo_window_days"] * 1440 * (1 - a["slo_target"])


def impact(m: Dict[str, Any], a: Dict[str, Any]) -> Dict[str, Any]:
    orders_lost = m["checkout_loss_min"] * a["orders_per_minute"]
    at_risk = orders_lost * a["avg_order_value"]
    lost = at_risk * (1 - a["customer_retry_rate"])
    incident_min = m["incident_s"] / 60
    ops = incident_min * a["incident_cost_per_minute"]
    tickets = orders_lost * (1 - a["customer_retry_rate"]) * a["support_ticket_rate"]
    support = tickets * a["cost_per_ticket"]
    per_incident = lost + ops + support
    budget = error_budget_minutes(a)
    return {
        "run_id": m["run_id"],
        "label": m["label"],
        "signature": m["signature"],
        "estimated": m["checkout_method"] != "measured",
        "checkout_method": m["checkout_method"],
        "orders_lost": round(orders_lost, 1),
        "revenue_at_risk": round(at_risk, 2),
        "revenue_lost": round(lost, 2),
        "incident_minutes": round(incident_min, 2),
        "ops_cost": round(ops, 2),
        "support_tickets": round(tickets, 1),
        "support_cost": round(support, 2),
        "cost_per_incident": round(per_incident, 2),
        "annual_exposure": round(per_incident * a["incidents_per_year"], 2),
        "error_budget_minutes": round(budget, 2),
        "slo_budget_burn_pct": round(m["error_minutes"] / budget * 100, 1) if budget else None,
        "sandbox_checkouts_lost": m.get("sandbox_checkouts_lost"),
        **stakeholder(m),
    }


def stakeholder(m: Dict[str, Any]) -> Dict[str, Any]:
    failing = set(m.get("endpoints_failing", []))
    stages = [{"stage": name, "blocked": sorted(failing & set(eps))} for name, eps in FUNNEL]
    checkout_hit = bool(failing & CHECKOUT_FLOW)
    peak = m.get("peak_error_rate", 0.0)
    if checkout_hit and peak >= 0.5:
        churn = "HIGH"
    elif checkout_hit or peak >= 0.2 or (m.get("p95_peak_ms") or 0) > 2000:
        churn = "MEDIUM"
    else:
        churn = "LOW"
    return {"churn_risk": churn, "funnel": stages, "checkout_flow_hit": checkout_hit}


def savings(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    d = old["cost_per_incident"] - new["cost_per_incident"]
    return {
        "from": old["run_id"], "to": new["run_id"],
        "saved_per_incident": round(d, 2),
        "saved_per_year": round(old["annual_exposure"] - new["annual_exposure"], 2),
        "relative": round(d / old["cost_per_incident"], 4) if old["cost_per_incident"] else None,
        "orders_saved_per_incident": round(old["orders_lost"] - new["orders_lost"], 1),
    }


def business_summary(metrics: List[Dict[str, Any]], overrides: Dict[str, Any] = None) -> Dict[str, Any]:
    a = load_assumptions(overrides)
    rows = [impact(m, a) for m in metrics]
    by_sig: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_sig.setdefault(r["signature"], []).append(r)
    trends = []
    for sig, rs in by_sig.items():
        trends.append({
            "signature": sig,
            "steps": [savings(rs[i - 1], rs[i]) for i in range(1, len(rs))],
            "cumulative": savings(rs[0], rs[-1]) if len(rs) > 1 else None,
        })
    worst = max(rows, key=lambda r: r["annual_exposure"], default=None)
    return {
        "assumptions": a,
        "formulas": FORMULAS,
        "runs": rows,
        "trends": trends,
        "worst_run": worst,
        "any_estimated": any(r["estimated"] for r in rows),
    }


FORMULAS = {
    "orders_lost": "checkout_loss_min x orders_per_minute",
    "revenue_at_risk": "orders_lost x avg_order_value",
    "revenue_lost": "revenue_at_risk x (1 - customer_retry_rate)",
    "ops_cost": "incident_minutes x incident_cost_per_minute",
    "support_tickets": "orders_lost x (1 - customer_retry_rate) x support_ticket_rate",
    "support_cost": "support_tickets x cost_per_ticket",
    "cost_per_incident": "revenue_lost + ops_cost + support_cost",
    "annual_exposure": "cost_per_incident x incidents_per_year",
    "slo_budget_burn_pct": "error_minutes / (slo_window_days x 1440 x (1 - slo_target)) x 100",
}
