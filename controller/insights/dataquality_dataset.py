"""Data quality on a large e-commerce dataset: 20,000 order lines, detect -> fix -> before vs after.

The live shop has only a handful of registered customers, so for scale the dashboard also checks a generated
e-commerce order export (order lines of an Indian online marketplace: INR prices, 18 % GST, UPI / cards / COD).
The data is SYNTHETIC: `generate()` builds realistic orders with a fixed seed, then plants problems at known rates and
writes which rows it changed to a separate ground-truth file. The detector never reads that file; it is only used
afterwards to measure detection accuracy (recall: planted problems found; precision: flagged rows that really had one).

Columns: order_id, line_no, order_date, customer_id, customer_name, email, phone, country, product_id, product_name,
category, quantity, unit_price, discount_pct, tax, line_total, payment_method, status.
Reference data used by the checks: the product catalogue (id, name, list price) and the GST rate.

Rules (same four types as the live-shop check, insights/dataquality.py):
  null           required value blank: order id/date, customer id, email, country, product id, quantity, price
  duplicate      the same order line exported twice; the same customer under two ids (same email)
  inconsistency  name spacing/case, email format, country spelling (IN, india, U.S.A ...), date format,
                 phone format, status / payment spelling, quantity not a whole number above 0
  discrepancy    product name or unit price differs from the catalogue, tax != 18 % of the net amount,
                 line total != net + tax, discount outside 0-60 %, order date in the future
Fix policy: fill or correct only from reference data or the same customer's/order's other rows; anything needing a
guess (unknown quantity, unrepairable email, impossible discount or date) is kept and flagged needs_review.
"""
import csv
import datetime as dt
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from insights.dataquality import EMAIL_RE, LABELS, ISSUE_TYPES, mask_email, repair_email

COLUMNS = ["order_id", "line_no", "order_date", "customer_id", "customer_name", "email", "phone", "country",
           "product_id", "product_name", "category", "quantity", "unit_price", "discount_pct", "tax", "line_total",
           "payment_method", "status"]
GST = 0.18
TODAY = dt.date(2026, 10, 11)              # fixed reference date, so results are reproducible
MAX_DISCOUNT = 60
REVIEW = "needs_review"

COUNTRIES = {"India": ("+91", 10), "United States": ("+1", 10), "United Kingdom": ("+44", 10),
             "Singapore": ("+65", 8), "United Arab Emirates": ("+971", 9)}
COUNTRY_ALIASES = {
    "india": "India", "in": "India", "ind": "India", "bharat": "India",
    "united states": "United States", "usa": "United States", "us": "United States", "u.s.a": "United States",
    "u.s.a.": "United States", "united states of america": "United States",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "u.k.": "United Kingdom", "great britain": "United Kingdom",
    "singapore": "Singapore", "sg": "Singapore",
    "united arab emirates": "United Arab Emirates", "uae": "United Arab Emirates",
}
STATUSES = ["Delivered", "Shipped", "Processing", "Cancelled", "Returned"]
PAYMENTS = ["UPI", "Credit Card", "Debit Card", "Net Banking", "Cash on Delivery"]
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y"]

_CATS = {
    "Electronics": [("Wireless Earbuds", 1999), ("Smartwatch", 3499), ("Power Bank 20000mAh", 1499),
                    ("Bluetooth Speaker", 2499), ("USB-C Charger 65W", 1299), ("Laptop Stand", 899),
                    ("Mechanical Keyboard", 3999), ("Wireless Mouse", 699)],
    "Fashion": [("Cotton Kurta", 1199), ("Denim Jeans", 1799), ("Running Shoes", 2999), ("Leather Wallet", 799),
                ("Silk Saree", 4599), ("Sports T-Shirt", 599), ("Sunglasses", 1299), ("Backpack", 1499)],
    "Home & Kitchen": [("Pressure Cooker 5L", 2199), ("Non-stick Pan", 1099), ("Mixer Grinder", 3299),
                       ("Water Bottle Steel", 499), ("Bedsheet Double", 1399), ("Table Lamp", 999),
                       ("Air Fryer", 5999), ("Dinner Set 24pc", 2799)],
    "Beauty": [("Face Wash", 299), ("Sunscreen SPF50", 449), ("Hair Dryer", 1599), ("Perfume 100ml", 1899),
               ("Lipstick", 399), ("Beard Trimmer", 1499), ("Moisturiser", 349), ("Shampoo 650ml", 549)],
    "Books & Toys": [("Building Blocks Set", 1299), ("Board Game", 999), ("Novel Paperback", 349),
                     ("Remote Control Car", 1799), ("Puzzle 1000pc", 699), ("Colouring Kit", 449),
                     ("Exam Guide", 599), ("Soft Toy", 549)],
}
CATALOGUE: Dict[str, Dict[str, Any]] = {}
for _ci, (_cat, _items) in enumerate(_CATS.items()):
    for _ii, (_name, _price) in enumerate(_items):
        CATALOGUE[f"P{_ci + 1}{_ii + 1:02d}"] = {"name": _name, "category": _cat, "price": float(_price)}

_FIRST = ["Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan", "Krishna", "Ishaan", "Ananya",
          "Diya", "Saanvi", "Aadhya", "Kavya", "Myra", "Riya", "Priya", "Neha", "Pooja", "Rahul", "Rohan", "Karan",
          "Sneha", "Meera", "Tara", "Nikhil", "Arnav", "Isha", "Zara", "John", "Emma", "Liam", "Olivia", "Wei",
          "Mei", "Omar", "Fatima", "Noah", "Sophia"]
_LAST = ["Sharma", "Verma", "Gupta", "Patel", "Reddy", "Nair", "Iyer", "Singh", "Kumar", "Agarwal", "Mehta", "Joshi",
         "Rao", "Das", "Khan", "Kapoor", "Bose", "Menon", "Pillai", "Chopra", "Smith", "Brown", "Tan", "Lim", "Ali"]
_DOMAINS = ["gmail.com", "yahoo.co.in", "outlook.com", "rediffmail.com", "hotmail.com"]


# ------------------------------------------------------------------------------------------------ generate
def _money(v: float) -> float:
    return round(v + 1e-9, 2)


def amounts(qty: float, price: float, disc: float) -> Tuple[float, float]:
    net = qty * price * (1 - disc / 100.0)
    tax = _money(net * GST)
    return tax, _money(net + tax)


def _fmt(v: float) -> str:
    return f"{v:.2f}"


def generate(n_lines: int = 20000, seed: int = 7, n_customers: int = 3000) -> Tuple[List[Dict[str, str]], Dict[int, List[str]]]:
    rng = random.Random(seed)
    customers, used_emails = [], set()
    for k in range(n_customers):
        f, l = rng.choice(_FIRST), rng.choice(_LAST)
        email = f"{f}.{l}{rng.randint(1, 999)}@{rng.choice(_DOMAINS)}".lower()
        while email in used_emails:                               # one email per real customer
            email = f"{f}.{l}{rng.randint(1, 9999)}@{rng.choice(_DOMAINS)}".lower()
        used_emails.add(email)
        country = rng.choices(list(COUNTRIES), weights=[78, 8, 6, 4, 4])[0]
        cc, nd = COUNTRIES[country]
        num = str(rng.choice([6, 7, 8, 9])) + "".join(str(rng.randint(0, 9)) for _ in range(nd - 1))
        customers.append({"customer_id": f"C{10001 + k}", "customer_name": f"{f} {l}",
                          "email": email,
                          "phone": f"{cc} {num}", "country": country})
    skus = list(CATALOGUE)
    rows: List[Dict[str, str]] = []
    oid = 100000
    while len(rows) < n_lines:
        oid += 1
        c = rng.choice(customers)
        date = TODAY - dt.timedelta(days=rng.randint(1, 365))
        pay, status = rng.choice(PAYMENTS), rng.choices(STATUSES, weights=[60, 15, 10, 8, 7])[0]
        for ln in range(1, rng.choice([1, 1, 2, 2, 3, 4]) + 1):
            if len(rows) >= n_lines:
                break
            sku = rng.choice(skus)
            p = CATALOGUE[sku]
            qty = rng.choices([1, 2, 3, 4, 5], weights=[55, 25, 10, 6, 4])[0]
            disc = rng.choice([0, 0, 0, 5, 10, 15, 20])
            tax, total = amounts(qty, p["price"], disc)
            rows.append({"order_id": f"O{oid}", "line_no": str(ln), "order_date": date.isoformat(), **c,
                         "product_id": sku, "product_name": p["name"], "category": p["category"],
                         "quantity": str(qty), "unit_price": _fmt(p["price"]), "discount_pct": str(disc),
                         "tax": _fmt(tax), "line_total": _fmt(total), "payment_method": pay, "status": status})

    truth: Dict[int, List[str]] = defaultdict(list)
    pool = list(range(len(rows)))
    rng.shuffle(pool)
    cursor = [0]

    def pick(rate):
        k = int(len(rows) * rate)
        out = pool[cursor[0]:cursor[0] + k]
        cursor[0] += k
        return out

    def recompute(r):
        tax, total = amounts(float(r["quantity"]), float(r["unit_price"]), float(r["discount_pct"]))
        r["tax"], r["line_total"] = _fmt(tax), _fmt(total)

    # ---- null / missing
    for i in pick(0.010): rows[i]["email"] = ""; truth[i].append("null:email")
    for i in pick(0.006): rows[i]["customer_id"] = ""; truth[i].append("null:customer_id")
    for i in pick(0.005): rows[i]["quantity"] = ""; truth[i].append("null:quantity")
    for i in pick(0.006): rows[i]["unit_price"] = ""; truth[i].append("null:unit_price")
    for i in pick(0.008): rows[i]["country"] = ""; truth[i].append("null:country")
    for i in pick(0.004): rows[i]["order_date"] = ""; truth[i].append("null:order_date")
    # ---- inconsistency
    for i in pick(0.020):
        e = rows[i]["email"]
        rows[i]["email"] = rng.choice([e.upper(), " " + e, e + " ", e.title()]); truth[i].append("inconsistency:email")
    for i in pick(0.003):
        rows[i]["email"] = rows[i]["email"].replace("@", "@@", 1); truth[i].append("inconsistency:email_typo")
    for i in pick(0.002):
        rows[i]["email"] = rows[i]["email"].replace("@", "-at-", 1); truth[i].append("inconsistency:email_invalid")
    for i in pick(0.030):
        alias = {"India": ["india", "IN", "Ind", "INDIA ", "Bharat"], "United States": ["USA", "U.S.A", "us"],
                 "United Kingdom": ["UK", "U.K.", "Great Britain"], "Singapore": ["SG", "singapore"],
                 "United Arab Emirates": ["UAE", "uae"]}[rows[i]["country"]]
        rows[i]["country"] = rng.choice(alias); truth[i].append("inconsistency:country")
    for i in pick(0.020):
        d = dt.date.fromisoformat(rows[i]["order_date"])
        rows[i]["order_date"] = d.strftime(rng.choice(DATE_FORMATS[1:])); truth[i].append("inconsistency:date")
    for i in pick(0.020):
        r = rows[i]
        cc, num = r["phone"].split(" ")
        r["phone"] = rng.choice([num, f"0{num}", f"{cc}-{num[:5]} {num[5:]}", f"{cc[1:]}{num}"])
        truth[i].append("inconsistency:phone")
    for i in pick(0.020):
        rows[i]["status"] = rng.choice([rows[i]["status"].upper(), rows[i]["status"].lower(), rows[i]["status"] + " "])
        truth[i].append("inconsistency:status")
    for i in pick(0.020):
        p = rows[i]["payment_method"]
        rows[i]["payment_method"] = rng.choice([v for v in (p.lower(), p.upper(), " " + p) if v != p])
        truth[i].append("inconsistency:payment")
    for i in pick(0.020):
        n = rows[i]["customer_name"]
        rows[i]["customer_name"] = rng.choice([n.lower(), n.upper(), "  " + n, n.replace(" ", "   ")])
        truth[i].append("inconsistency:name")
    for i in pick(0.004):
        rows[i]["quantity"] = rng.choice(["-1", "0", "-2"]); truth[i].append("inconsistency:quantity")
    for i in pick(0.002):
        rows[i]["quantity"] = rng.choice(["1.5", "2.5"]); truth[i].append("inconsistency:quantity_fraction")
    # ---- discrepancy
    for i in pick(0.015):
        r = rows[i]
        r["unit_price"] = _fmt(float(r["unit_price"]) * rng.choice([0.5, 1.1, 1.25, 1.5, 10]))
        recompute(r); truth[i].append("discrepancy:unit_price")
    for i in pick(0.015):
        r = rows[i]
        r["line_total"] = _fmt(float(r["line_total"]) + rng.choice([-500, -100, 100, 250, 1000])); truth[i].append("discrepancy:line_total")
    for i in pick(0.010):
        r = rows[i]
        r["tax"] = _fmt(float(r["tax"]) * rng.choice([0, 0.5, 2])); r["line_total"] = r["line_total"]
        truth[i].append("discrepancy:tax")
    for i in pick(0.005):
        r = rows[i]
        other = rng.choice([s for s in skus if s != r["product_id"]])
        r["product_name"] = CATALOGUE[other]["name"]; truth[i].append("discrepancy:product_name")
    for i in pick(0.003):
        rows[i]["discount_pct"] = rng.choice(["75", "90", "-10", "120"]); truth[i].append("discrepancy:discount")
    for i in pick(0.003):
        rows[i]["order_date"] = (TODAY + dt.timedelta(days=rng.randint(30, 400))).isoformat()
        truth[i].append("discrepancy:future_date")
    # ---- duplicate customer: the same person under a second id (same email)
    for i in pick(0.010):
        rows[i]["customer_id"] = "C9" + rows[i]["customer_id"][1:]; truth[i].append("duplicate:customer")
    # ---- exact duplicate lines (the export repeated them)
    clean = [i for i in range(len(rows)) if i not in truth]
    for src_row in [rows[i] for i in rng.sample(clean, int(len(rows) * 0.015))]:
        pos = next(k for k, r in enumerate(rows) if r is src_row)
        at = rng.randint(pos + 1, len(rows))          # the repeated copy always comes after the original
        rows.insert(at, dict(src_row))
        # shift truth indices at/after the insertion point
        truth = defaultdict(list, {(k + 1 if k >= at else k): v for k, v in truth.items()})
        truth[at].append("duplicate:row")
    return rows, dict(truth)


def write_dataset(path: Path, n_lines: int = 20000, seed: int = 7) -> None:
    rows, truth = generate(n_lines, seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    path.with_name(path.stem + "_truth.json").write_text(
        json.dumps({"note": "Rows (0-based, data rows only) where problems were planted. The detector never reads "
                            "this file; it is only used to measure detection accuracy.",
                    "seed": seed, "rows": {str(k): v for k, v in sorted(truth.items())}}, indent=0),
        encoding="utf-8")


def read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (v if v is not None else "") for k, v in r.items()} for r in csv.DictReader(f)]


# ------------------------------------------------------------------------------------------------ analyze
def _f(v: str) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_date(v: str) -> Optional[dt.date]:
    v = (v or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def norm_phone(phone: str, country: str) -> Optional[str]:
    if country not in COUNTRIES:
        return None
    cc, nd = COUNTRIES[country]
    digits = re.sub(r"\D", "", phone or "")
    ccd = cc[1:]
    if digits.startswith(ccd) and len(digits) == len(ccd) + nd:
        digits = digits[len(ccd):]
    elif digits.startswith("0") and len(digits) == nd + 1:
        digits = digits[1:]
    return f"{cc} {digits}" if len(digits) == nd else None


def _name(n: str) -> str:
    return " ".join(w.capitalize() for w in (n or "").split())


def _canon(value: str, choices: List[str]) -> Optional[str]:
    k = (value or "").strip().lower()
    return next((c for c in choices if c.lower() == k), None)


def analyze(rows: List[Dict[str, str]], cap_issues: int = 400) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    flagged, review = set(), set()
    fixed = [dict(r) for r in rows]

    def add(i, col, kind, rule, before, after, action, fixable=True):
        issues.append({"row": i, "record": f"row {i + 2} · {rows[i].get('order_id') or 'no order id'}", "field": col,
                       "type": kind, "type_label": LABELS[kind], "rule": rule, "before": before,
                       "after": after if fixable else None, "action": action, "fixable": fixable})
        flagged.add(i)
        if not fixable:
            review.add(i)

    # reference maps built from the data itself (majority vote per customer / order)
    ids_by_email = defaultdict(Counter)
    email_by_id, country_by_id, date_by_order = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for r in rows:
        e = (r["email"] or "").strip().lower()
        if e and r["customer_id"]:
            ids_by_email[e][r["customer_id"]] += 1
            email_by_id[r["customer_id"]][e] += 1
        d = parse_date(r["order_date"])
        if d and r["order_id"]:
            date_by_order[r["order_id"]][d.isoformat()] += 1
    # the id used most for an email is the customer's real id (ties: the older, lower id)
    canonical_id = {e: min(c, key=lambda k: (-c[k], k)) for e, c in ids_by_email.items()}
    for r in rows:   # countries per customer, after duplicate ids are resolved to the real one
        cn = COUNTRY_ALIASES.get((r["country"] or "").strip().lower())
        cid = canonical_id.get((r["email"] or "").strip().lower()) or r["customer_id"]
        if cn and cid:
            country_by_id[cid][cn] += 1

    seen_rows = {}
    drop = set()
    for i, r in enumerate(rows):
        key = tuple(r[c] for c in COLUMNS)
        if key in seen_rows:
            add(i, "row", "duplicate", "Each order line appears once", f"same as row {seen_rows[key] + 2}",
                "(removed)", "Removed the duplicate line")
            drop.add(i)
            continue
        seen_rows[key] = i
        x = fixed[i]

        # -- customer
        e_raw = r["email"]
        if not e_raw.strip():
            known = email_by_id.get(r["customer_id"])
            if known:
                x["email"] = known.most_common(1)[0][0]
                add(i, "email", "null", "Email is required", e_raw, x["email"], "Filled from the customer's other orders")
            else:
                add(i, "email", "null", "Email is required", e_raw, None, "Quarantined: no email on record", False)
        else:
            norm = e_raw.strip().lower()
            if EMAIL_RE.match(norm):
                if norm != e_raw:
                    x["email"] = norm
                    add(i, "email", "inconsistency", "Email in lower case, no spaces", e_raw, norm, "Normalised format")
            else:
                rep = repair_email(e_raw)
                if rep:
                    x["email"] = rep
                    add(i, "email", "inconsistency", "Email must be a valid address", e_raw, rep, "Corrected a doubled '@' / '.' typo")
                else:
                    add(i, "email", "inconsistency", "Email must be a valid address", e_raw, None,
                        "Quarantined: email cannot be repaired safely", False)
        cid = r["customer_id"]
        canon = canonical_id.get((x["email"] or "").strip().lower())
        if not cid:
            if canon:
                x["customer_id"] = canon
                add(i, "customer_id", "null", "Customer id is required", cid, canon, "Filled from the email's customer record")
            else:
                add(i, "customer_id", "null", "Customer id is required", cid, None, "Quarantined: customer unknown", False)
        elif canon and cid != canon:
            x["customer_id"] = canon
            add(i, "customer_id", "duplicate", "One id per customer (same email)", cid, canon, "Merged into the main customer id")
        if _name(r["customer_name"]) != r["customer_name"]:
            x["customer_name"] = _name(r["customer_name"])
            add(i, "customer_name", "inconsistency", "Name in Title Case, single spaces", r["customer_name"],
                x["customer_name"], "Normalised format")
        c_raw = r["country"]
        cn = COUNTRY_ALIASES.get(c_raw.strip().lower()) if c_raw.strip() else None
        if not c_raw.strip():
            known = country_by_id.get(x["customer_id"])
            if known:
                x["country"] = known.most_common(1)[0][0]
                add(i, "country", "null", "Country is required", c_raw, x["country"], "Filled from the customer's other orders")
            else:
                add(i, "country", "null", "Country is required", c_raw, None, "Quarantined: country unknown", False)
        elif cn is None:
            add(i, "country", "inconsistency", "Country must be a known value", c_raw, None, "Quarantined: unknown country", False)
        elif cn != c_raw:
            x["country"] = cn
            add(i, "country", "inconsistency", "One spelling per country", c_raw, cn, "Standardised spelling")
        ph = norm_phone(r["phone"], x["country"])
        if ph and ph != r["phone"]:
            x["phone"] = ph
            add(i, "phone", "inconsistency", "Phone as +<country code> <number>", r["phone"], ph, "Standardised format")

        # -- order
        d_raw = r["order_date"]
        d = parse_date(d_raw)
        if not d_raw.strip():
            known = date_by_order.get(r["order_id"])
            if known:
                x["order_date"] = known.most_common(1)[0][0]
                add(i, "order_date", "null", "Order date is required", d_raw, x["order_date"], "Filled from the same order's other lines")
            else:
                add(i, "order_date", "null", "Order date is required", d_raw, None, "Quarantined: date unknown", False)
        elif d is None:
            add(i, "order_date", "inconsistency", "Date must be a valid date", d_raw, None, "Quarantined: unreadable date", False)
        else:
            if d.isoformat() != d_raw:
                x["order_date"] = d.isoformat()
                add(i, "order_date", "inconsistency", "Dates as YYYY-MM-DD (day first when ambiguous)", d_raw,
                    x["order_date"], "Standardised format")
            if d > TODAY:
                add(i, "order_date", "discrepancy", "Order date cannot be in the future", d_raw, None,
                    "Quarantined: impossible date", False)
        for col, choices in (("status", STATUSES), ("payment_method", PAYMENTS)):
            v = r[col]
            c = _canon(v, choices)
            if c is None:
                add(i, col, "null" if not v.strip() else "inconsistency", f"{col.replace('_', ' ').capitalize()} must be a known value",
                    v, None, "Quarantined: unknown value", False)
            elif c != v:
                x[col] = c
                add(i, col, "inconsistency", "One spelling per value", v, c, "Standardised spelling")

        # -- product and amounts
        sku = r["product_id"]
        ref = CATALOGUE.get(sku)
        if not sku.strip() or ref is None:
            add(i, "product_id", "null" if not sku.strip() else "inconsistency", "Product must exist in the catalogue",
                sku, None, "Quarantined: product unknown", False)
            continue
        if r["product_name"] != ref["name"]:
            x["product_name"] = ref["name"]
            add(i, "product_name", "discrepancy", "Product name matches the catalogue", r["product_name"], ref["name"],
                "Corrected from the catalogue")
        if r["category"] != ref["category"]:
            x["category"] = ref["category"]
            add(i, "category", "discrepancy", "Category matches the catalogue", r["category"], ref["category"],
                "Corrected from the catalogue")
        price = _f(r["unit_price"])
        if price is None:
            x["unit_price"] = _fmt(ref["price"])
            add(i, "unit_price", "null", "Unit price is required", r["unit_price"], x["unit_price"], "Filled from the catalogue")
        elif abs(price - ref["price"]) > 0.01:
            x["unit_price"] = _fmt(ref["price"])
            add(i, "unit_price", "discrepancy", "Unit price matches the catalogue", r["unit_price"], x["unit_price"],
                "Corrected to the catalogue price")
        disc = _f(r["discount_pct"])
        if disc is None or not 0 <= disc <= MAX_DISCOUNT:
            add(i, "discount_pct", "discrepancy", f"Discount between 0 and {MAX_DISCOUNT} %", r["discount_pct"], None,
                "Quarantined: impossible discount", False)
            continue
        q_raw = r["quantity"]
        q = _f(q_raw)
        if not q_raw.strip():
            add(i, "quantity", "null", "Quantity is required", q_raw, None, "Quarantined: quantity unknown", False)
            continue
        if q is None or q <= 0 or q != int(q):
            add(i, "quantity", "inconsistency", "Quantity is a whole number above 0", q_raw, None,
                "Quarantined: invalid quantity", False)
            continue
        tax, total = amounts(q, float(x["unit_price"]), disc)
        t = _f(r["tax"])
        if t is None or abs(t - tax) > 0.01:
            x["tax"] = _fmt(tax)
            add(i, "tax", "null" if t is None else "discrepancy", "Tax = 18 % GST of the net amount", r["tax"], x["tax"], "Recomputed")
        lt = _f(r["line_total"])
        if lt is None or abs(lt - total) > 0.01:
            x["line_total"] = _fmt(total)
            add(i, "line_total", "null" if lt is None else "discrepancy", "Line total = qty × price × (1 − discount) + tax",
                r["line_total"], x["line_total"], "Recomputed")

    cleaned = []
    for i, x in enumerate(fixed):
        if i in drop:
            continue
        cleaned.append({**{c: x[c] for c in COLUMNS}, "dq_status": REVIEW if i in review else ""})
    by_type = {t: sum(1 for k in issues if k["type"] == t) for t in ISSUE_TYPES}
    records = len(rows)
    clean_records = records - len(flagged)
    summary = {
        "records": records, "customers": len({(r["customer_id"] or "?") for r in rows}),
        "orders": len({r["order_id"] for r in rows if r["order_id"]}),
        "issues": len(issues), "by_type": by_type,
        "fixable": sum(1 for k in issues if k["fixable"]), "quarantined": sum(1 for k in issues if not k["fixable"]),
        "clean_records": clean_records, "quality_score": round(100.0 * clean_records / records, 1) if records else 100.0,
        "duplicates_merged": sum(1 for k in issues if k["type"] == "duplicate"),
        "rows_flagged": len(flagged), "rows_for_review": len(review), "rows_removed": len(drop),
    }
    by_rule = Counter((k["type"], k["field"], k["rule"]) for k in issues)
    return {"summary": summary, "issues": issues, "cleaned": cleaned, "flagged_rows": flagged,
            "by_rule": [{"type": t, "type_label": LABELS[t], "field": f, "rule": ru, "count": n}
                        for (t, f, ru), n in by_rule.most_common()]}


def profile(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, int]]:
    """Per column: blank values and distinct spellings (for the categorical columns)."""
    out = {}
    for c in COLUMNS:
        vals = [r.get(c, "") for r in rows]
        out[c] = {"blank": sum(1 for v in vals if not (v or "").strip())}
        if c in ("country", "status", "payment_method", "category"):
            out[c]["distinct"] = len({v for v in vals if (v or "").strip()})
    return out


def accuracy(flagged: set, truth: Dict[str, List[str]]) -> Dict[str, Any]:
    planted = {int(k) for k in truth}
    hit = planted & flagged
    return {"planted_rows": len(planted), "flagged_rows": len(flagged), "found": len(hit),
            "recall": round(100.0 * len(hit) / len(planted), 1) if planted else 100.0,
            "precision": round(100.0 * len(hit) / len(flagged), 1) if flagged else 100.0}


def sample_issues(issues: List[Dict[str, Any]], n: int = 400) -> List[Dict[str, Any]]:
    """A readable sample for the screen: round-robin over the rules so every kind of problem is shown."""
    groups = defaultdict(list)
    for k in issues:
        groups[(k["type"], k["field"], k["rule"])].append(k)
    out, idx = [], 0
    while len(out) < n and any(idx < len(g) for g in groups.values()):
        for g in groups.values():
            if idx < len(g) and len(out) < n:
                out.append(g[idx])
        idx += 1
    out.sort(key=lambda k: k["row"])
    shown = []
    for k in out:
        j = {kk: v for kk, v in k.items() if kk != "row"}
        if k["field"] == "email":
            j["before"], j["after"] = mask_email(k["before"]), mask_email(k["after"])
        else:
            for side in ("before", "after"):
                v = j[side]
                if isinstance(v, str) and v != v.strip() or (isinstance(v, str) and "  " in v):
                    j[side] = v.replace(" ", "␣")
        shown.append(j)
    return shown


def write_csv(rows: List[Dict[str, str]], path: Path, columns: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
