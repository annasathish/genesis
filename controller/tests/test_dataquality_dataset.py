"""Large e-commerce dataset: planted problems are found, fixes leave only flagged rows, endpoints work."""
from insights import dataquality_dataset as dqd


def test_detects_planted_problems_with_high_accuracy():
    rows, truth = dqd.generate(3000, seed=3, n_customers=500)
    a = dqd.analyze(rows)
    acc = dqd.accuracy(a["flagged_rows"], {str(k): v for k, v in truth.items()})
    assert acc["recall"] >= 98 and acc["precision"] >= 99
    assert all(a["summary"]["by_type"][t] > 0 for t in ("null", "duplicate", "inconsistency", "discrepancy"))


def test_fix_leaves_only_flagged_rows_and_raises_quality():
    rows, _ = dqd.generate(3000, seed=5, n_customers=500)
    a = dqd.analyze(rows)
    b = dqd.analyze(a["cleaned"])
    assert sum(1 for k in b["issues"] if k["fixable"]) == 0
    assert b["summary"]["quality_score"] > a["summary"]["quality_score"]
    assert len(a["cleaned"]) == len(rows) - a["summary"]["rows_removed"]
    flagged = [r for r in a["cleaned"] if r["dq_status"] == dqd.REVIEW]
    assert len(flagged) == a["summary"]["rows_for_review"]


def test_individual_rules():
    base = dict(order_id="O1", line_no="1", order_date="2026-01-05", customer_id="C10001", customer_name="Riya Nair",
                email="riya.nair1@gmail.com", phone="+91 9876543210", country="India", product_id="P101",
                product_name=dqd.CATALOGUE["P101"]["name"], category=dqd.CATALOGUE["P101"]["category"], quantity="2",
                unit_price="1999.00", discount_pct="10", payment_method="UPI", status="Delivered")
    tax, total = dqd.amounts(2, 1999.0, 10)
    base.update(tax=f"{tax:.2f}", line_total=f"{total:.2f}")
    assert dqd.analyze([base])["issues"] == []                                   # a clean line is left alone

    def one(**chg):
        r = dict(base, **chg)
        return {(k["field"], k["type"], k["fixable"], k["after"]) for k in dqd.analyze([base, r])["issues"]}
    assert ("country", "inconsistency", True, "India") in one(country="IN")
    assert ("order_date", "inconsistency", True, "2026-01-05") in one(order_date="05/01/2026")
    assert ("phone", "inconsistency", True, "+91 9876543210") in one(phone="09876543210")
    assert ("unit_price", "discrepancy", True, "1999.00") in one(unit_price="2999.00")
    assert ("email", "null", True, "riya.nair1@gmail.com") in one(email="")         # filled from the customer's rows
    assert ("quantity", "inconsistency", False, None) in one(quantity="-1")        # flagged, never guessed
    assert ("customer_id", "duplicate", True, "C10001") in one(customer_id="C90001", order_id="O2")
    dup = dqd.analyze([base, dict(base)])
    assert [k["type"] for k in dup["issues"]] == ["duplicate"] and len(dup["cleaned"]) == 1


def test_dataset_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from insights.dashboard.server import create_app
    data = tmp_path / "data" / "orders.csv"
    dqd.write_dataset(data, n_lines=1500, seed=2)
    c = TestClient(create_app(runs_dir=tmp_path / "runs", api_key="", dataset_path=data))
    r = c.post("/api/data/dataset/scan").json()
    assert r["source"] == "dataset" and r["before"]["summary"]["records"] == len(dqd.read_csv(data))
    r = c.post("/api/data/dataset/fix").json()
    assert r["residual"]["open_fixable"] == 0 and r["accuracy"]["recall"] >= 95
    assert r["after"]["summary"]["quality_score"] > r["before"]["summary"]["quality_score"]
    for kind in ("raw", "clean", "issues", "truth"):
        assert c.get(f"/api/data/dataset/download/{kind}").status_code == 200
    assert c.get("/api/data/dataset/download/nope").status_code == 404
