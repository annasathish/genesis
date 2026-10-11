"""Data quality of customer-entered data: detection rules, the fix plan, masking and the dashboard endpoints."""
import copy

import pytest

from insights import dataquality as dq

CAT = {"RMC": 42, "STAN-1": 67}
GOOD = {"orderid": "o1", "cart": {"total": 42, "tax": 7.0,
                                 "items": [{"qty": 1, "sku": "RMC", "name": "x", "price": 42, "subtotal": 42}]}}


def _data():
    customers = [
        {"id": "a" * 24, "name": "stan", "email": "stan@instana.com"},
        {"id": "b" * 24, "name": "dq-alice", "email": "alice@example.com"},
        {"id": "c" * 24, "name": "DQ-Alice ", "email": " Alice@Example.COM "},
        {"id": "d" * 24, "name": "dq-bob", "email": None},
        {"id": "e" * 24, "name": "dq-carol", "email": "carol@@example..com"},
        {"id": "f" * 24, "name": "dq-frank", "email": "frank-at-example"},
    ]
    orders = {
        "dq-alice": [copy.deepcopy(GOOD), copy.deepcopy(GOOD)],
        "DQ-Alice ": [{"orderid": "o2", "cart": {"total": 67, "tax": 11.17,
                                                  "items": [{"qty": 1, "sku": "STAN-1", "price": 67, "subtotal": 67}]}}],
        "dq-bob": [{"orderid": "o3", "cart": {"total": 5000, "tax": 1, "items": [
            {"qty": 2, "sku": "STAN-1", "price": 2001, "subtotal": 100}]}}],
        "stan": [{"cart": {"total": 42, "tax": 7, "items": [{"qty": None, "sku": "RMC", "price": 42, "subtotal": 42}]}}],
    }
    return customers, orders


def _kinds(a, field=None, record=None):
    return [i["type"] for i in a["issues"] if (field is None or i["field"] == field)
            and (record is None or i["record"].startswith(record))]


def test_detects_all_four_issue_types():
    c, o = _data()
    a = dq.analyze(c, o, CAT)
    assert set(a["summary"]["by_type"]) == set(dq.ISSUE_TYPES)
    assert all(a["summary"]["by_type"][t] > 0 for t in dq.ISSUE_TYPES)
    assert "null" in _kinds(a, "email", "dq-bob")                     # null email
    assert "duplicate" in _kinds(a, "customer", "DQ-Alice")           # same person, different case/spaces
    assert "inconsistency" in _kinds(a, "email", "DQ-Alice")          # format
    assert "duplicate" in _kinds(a, "orderid", "dq-alice")            # same order twice
    assert _kinds(a, "price", "dq-bob") == ["discrepancy"]            # price vs catalogue
    assert _kinds(a, "total", "dq-bob") == ["discrepancy"]
    assert _kinds(a, "tax", "dq-bob") == ["discrepancy"]
    assert a["summary"]["quality_score"] < 100


def test_clean_records_raise_no_issue():
    a = dq.analyze([{"id": "a" * 24, "name": "stan", "email": "stan@instana.com"}],
                   {"stan": [copy.deepcopy(GOOD)]}, CAT)
    assert a["issues"] == [] and a["summary"]["quality_score"] == 100.0


def test_fix_policy_never_guesses():
    c, o = _data()
    a = dq.analyze(c, o, CAT)
    by = {(i["record"].split(" ·")[0], i["field"]): i for i in a["issues"]}
    assert by[("dq-carol", "email")]["after"] == "carol@example.com"   # doubled separators: safe repair
    assert not by[("dq-frank", "email")]["fixable"]                    # cannot be repaired: flagged, not guessed
    assert not by[("dq-bob", "email")]["fixable"]                      # null email: ask the customer
    qty = [i for i in a["issues"] if i["field"] == "qty"]
    assert qty and not qty[0]["fixable"]                               # unknown quantity: flagged
    bob = a["clean"]["orders"]["dq-bob"][0]["cart"]
    assert bob["items"][0]["price"] == 67 and bob["items"][0]["subtotal"] == 134
    assert bob["total"] == 134 and bob["tax"] == dq.tax_for(134)
    alice = a["clean"]["orders"]["dq-alice"]
    assert [x["orderid"] for x in alice] == ["o1", "o2"]               # duplicate dropped, merged orders moved
    assert [x["name"] for x in a["clean"]["customers"]].count("dq-alice") == 1
    flagged = {x["name"]: x.get("dq_status") for x in a["clean"]["customers"]}
    assert flagged["dq-bob"] == dq.REVIEW and flagged["stan"] is None
    assert a["clean"]["orders"]["stan"][0]["orderid"].startswith("recovered-")


def test_second_pass_leaves_only_flagged_problems():
    c, o = _data()
    a = dq.analyze(c, o, CAT)
    b = dq.analyze(a["clean"]["customers"], a["clean"]["orders"], CAT)
    assert b["summary"]["issues"] < a["summary"]["issues"]
    assert dq.residual_summary(b)["open_fixable"] == 0
    assert b["summary"]["quality_score"] > a["summary"]["quality_score"]


def test_plan_writes_touches_only_what_changed():
    c, o = _data()
    a = dq.analyze(c, o, CAT)
    ops = dq.plan_writes(c, o, a)
    kinds = [(op["op"], op.get("id") or op.get("name")) for op in ops]
    assert ("delete_customer", "c" * 24) in kinds                      # the duplicate
    assert ("delete_orders", "DQ-Alice ") in kinds                     # its history moved to the survivor
    assert not any(k == ("update_customer", "a" * 24) for k in kinds)  # clean customer untouched
    order = [op["op"] for op in ops]
    assert order.index("delete_orders") < order.index("set_orders")


def test_mask_keeps_the_problem_visible_but_hides_the_address():
    assert dq.mask_email(" Alice@Example.COM ") == "␣Al***@Example.COM␣"
    assert dq.mask_email("") == "(empty)" and dq.mask_email(None) is None
    s = dq.for_screen(dq.analyze(*_data(), CAT))
    assert not any("alice@example.com" == i["before"] for i in s["issues"])
    assert all("key" not in i for i in s["issues"])


def test_apply_refuses_records_outside_the_snapshot():
    from insights.dataquality_shop import DataSourceError, ShopData
    src = ShopData(docker_cmd=("definitely-not-docker",))
    snap = {"customers": [{"id": "a" * 24, "name": "stan"}], "orders": {}}
    with pytest.raises(DataSourceError, match="unknown customer"):
        src.apply([{"op": "delete_customer", "id": "b" * 24}], snap)
    with pytest.raises(DataSourceError, match="unknown"):
        src.apply([{"op": "set_orders", "name": "someone-else", "history": []}], snap)


class _FakeSource:
    def __init__(self):
        self.c, self.o = _data()
        self.applied = None

    def snapshot(self):
        return {"customers": copy.deepcopy(self.c), "orders": copy.deepcopy(self.o), "catalogue": dict(CAT),
                "taken_at": 1.0}

    def inject_demo(self):
        return {"accepted": 12, "refused": []}

    def reset_demo(self):
        return {"customers": 0, "orders": 0}

    def apply(self, ops, snap):
        a = dq.analyze(snap["customers"], snap["orders"], snap["catalogue"])
        self.c, self.o = a["clean"]["customers"], a["clean"]["orders"]
        self.applied = ops
        return {"applied": len(ops), "backup": None}


def test_dashboard_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from insights.dashboard.server import create_app
    src = _FakeSource()
    c = TestClient(create_app(runs_dir=tmp_path, api_key="", dq_source=src))
    assert c.get("/api/data/last").json() == {}
    r = c.post("/api/data/scan").json()
    assert r["stage"] == "scanned" and r["before"]["summary"]["issues"] > 0
    r = c.post("/api/data/fix").json()
    assert r["stage"] == "fixed" and src.applied
    assert r["after"]["summary"]["issues"] < r["before"]["summary"]["issues"]
    assert r["residual"]["open_fixable"] == 0
    assert c.get("/api/data/last").json()["stage"] == "fixed"          # survives a page reload
    assert "password" not in str(r)
    assert c.post("/api/data/inject").json()["injected"]["accepted"] == 12
    assert c.post("/api/data/reset").json()["removed"] == {"customers": 0, "orders": 0}
