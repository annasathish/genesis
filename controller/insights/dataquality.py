"""Data quality for customer-entered data in the sandbox shop: detect, fix, and report before vs after.

Pure functions (no I/O): the dashboard reads a snapshot of the shop's own data (insights/dataquality_shop.py),
`analyze()` finds the problems and builds the cleaned version, `plan_writes()` turns that into database updates.

Why this exists: Robot Shop's user service only checks that name, password and email are *present*
(`// TODO - validate email address format` in its register handler); the duplicate check is an exact name match,
and orders are stored exactly as sent. So real customer input can arrive inconsistent, duplicated, null or with
amounts that do not add up. The four issue types, mapped to the usual data-quality dimensions:

  null           completeness  required value missing or blank (email, name, order id, item qty/price/sku)
  duplicate      uniqueness    the same customer twice (name or email equal after trimming / lowercasing),
                               or the same order id stored twice
  inconsistency  consistency   stray spaces or upper case in names/emails, invalid email format,
  / validity                   unknown product, non-positive or fractional quantity
  discrepancy    accuracy      amounts that disagree: item price vs the catalogue, subtotal vs price x qty,
                               total vs sum of subtotals, tax vs the shop's 20 % included tax

Fix policy (conservative, every change is listed as an action):
  * fixable      normalised formats, merged duplicates (orders moved to the first-registered record),
                 recomputed amounts from the catalogue price, a recovery id for a missing order id,
                 an email typo with doubled separators ("@@", "..") collapsed only if the result is valid
  * quarantined  anything that would need a guess (missing email, unrecoverable email, missing or invalid qty,
                 unknown product): left as is and flagged `dq_status: needs_review` for a human
Passwords are never read into the analysis.
"""
import copy
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
SHIPPING_SKU = "SHIP"          # the cart adds shipping as an item with its own price
TAX_DIVISOR = 1.2              # cart service: tax = total - total / 1.2 (20 % included in prices)
TOL = 0.01                     # money tolerance (rounding)
ISSUE_TYPES = ("null", "duplicate", "inconsistency", "discrepancy")
LABELS = {"null": "Null / missing", "duplicate": "Duplicate", "inconsistency": "Inconsistent / invalid",
          "discrepancy": "Discrepancy"}
REVIEW = "needs_review"


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def clean_name(n: Any) -> str:
    return " ".join(str(n).split()) if n is not None else ""


def name_key(n: Any) -> str:
    return clean_name(n).lower()


def clean_email(e: Any) -> str:
    return str(e).strip().lower() if e is not None else ""


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.match(e or ""))


def repair_email(e: Any) -> Optional[str]:
    """Normalise; collapse doubled separators ("@@", ".."). Returns a valid address or None (needs a human)."""
    s = clean_email(e)
    if valid_email(s):
        return s
    s = re.sub(r"\.{2,}", ".", re.sub(r"@{2,}", "@", s.replace(" ", "")))
    return s if valid_email(s) else None


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _money(v: float) -> float:
    return round(v + 0.0, 2)


def tax_for(total: float) -> float:
    return _money(total - total / TAX_DIVISOR)


class _Issues:
    def __init__(self):
        self.items: List[Dict[str, Any]] = []

    def add(self, entity, record, field, kind, rule, before, after, action, fixable=True, key=None):
        self.items.append({"id": len(self.items) + 1, "entity": entity, "record": record, "field": field,
                           "type": kind, "type_label": LABELS[kind], "rule": rule, "before": before,
                           "after": after, "action": action, "fixable": fixable, "key": key})


def analyze(customers: List[Dict[str, Any]], orders: Dict[str, List[Dict[str, Any]]],
            catalogue: Dict[str, float]) -> Dict[str, Any]:
    """customers: [{id, name, email, dq_status?}] in registration order; orders: {customer name: [order]};
    catalogue: {sku: price}. Returns the issues, the cleaned data and a summary (scores per dimension)."""
    iss = _Issues()
    cust = [dict(c) for c in customers]
    fixed_c: Dict[str, Dict[str, Any]] = {}          # id -> cleaned customer
    quarantine_c: Dict[str, List[str]] = {}

    # ---- customers: completeness, consistency, validity
    for c in cust:
        cid, raw_name, raw_email = c["id"], c.get("name"), c.get("email")
        rec = clean_name(raw_name) or f"(no name, id …{str(cid)[-6:]})"
        new = {"id": cid, "name": raw_name, "email": raw_email}
        if _blank(raw_name):
            iss.add("customer", rec, "name", "null", "Customer name is required", raw_name, None,
                    "Quarantined: needs the customer's name", fixable=False, key=cid)
            quarantine_c.setdefault(cid, []).append("missing name")
        elif clean_name(raw_name) != raw_name:
            new["name"] = clean_name(raw_name)
            iss.add("customer", rec, "name", "inconsistency", "No leading, trailing or double spaces",
                    raw_name, new["name"], "Trimmed spaces", key=cid)
        if _blank(raw_email):
            iss.add("customer", rec, "email", "null", "Contact email is required", raw_email, None,
                    "Quarantined: ask the customer for an email", fixable=False, key=cid)
            quarantine_c.setdefault(cid, []).append("missing email")
        else:
            norm = clean_email(raw_email)
            if valid_email(norm):
                if norm != raw_email:
                    new["email"] = norm
                    iss.add("customer", rec, "email", "inconsistency", "Email in lower case, no spaces",
                            raw_email, norm, "Normalised format", key=cid)
            else:
                fixed = repair_email(raw_email)
                if fixed:
                    new["email"] = fixed
                    iss.add("customer", rec, "email", "inconsistency", "Email must be a valid address",
                            raw_email, fixed, "Corrected a doubled '@' / '.' typo", key=cid)
                else:
                    iss.add("customer", rec, "email", "inconsistency", "Email must be a valid address",
                            raw_email, None, "Quarantined: email cannot be repaired safely", fixable=False,
                            key=cid)
                    quarantine_c.setdefault(cid, []).append("invalid email")
        fixed_c[cid] = new

    # ---- customers: uniqueness (same name or same email after cleaning); first registered survives
    parent = {c["id"]: c["id"] for c in cust}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    order_ix = {c["id"]: i for i, c in enumerate(cust)}
    seen: Dict[Tuple[str, str], str] = {}
    for c in cust:
        f = fixed_c[c["id"]]
        for k in (("name", name_key(f["name"])), ("email", clean_email(f["email"]))):
            if not k[1]:
                continue
            if k in seen:
                a, b = find(seen[k]), find(c["id"])
                if a != b:
                    keep, drop = (a, b) if order_ix[a] < order_ix[b] else (b, a)
                    parent[drop] = keep
            else:
                seen[k] = c["id"]
    merged_into: Dict[str, str] = {}
    for c in cust:
        root = find(c["id"])
        if root != c["id"]:
            merged_into[c["id"]] = root
            dup, keep = fixed_c[c["id"]], fixed_c[root]
            iss.add("customer", clean_name(c.get("name")) or c["id"], "customer", "duplicate",
                    "One record per customer (same name or email)",
                    f"{clean_name(c.get('name'))} = {clean_name(keep['name'])}",
                    clean_name(keep["name"]), "Merged into the first registration; orders moved", key=c["id"])

    # ---- orders: per customer, in stored order
    name_of = {c["id"]: c.get("name") for c in cust}
    id_by_name = {c.get("name"): c["id"] for c in cust}
    fixed_orders: Dict[str, List[Dict[str, Any]]] = {}     # survivor's FINAL name -> orders
    seen_oid: Dict[str, str] = {}
    order_total = 0
    order_bad = 0
    quarantined_orders = 0
    for owner, hist in orders.items():
        cid = id_by_name.get(owner)
        survivor = merged_into.get(cid, cid) if cid else None
        final_name = fixed_c[survivor]["name"] if survivor else owner
        bucket = fixed_orders.setdefault(final_name, [])
        for i, o in enumerate(hist or []):
            order_total += 1
            before_n = len(iss.items)
            o2 = copy.deepcopy(o) if isinstance(o, dict) else {}
            oid = o2.get("orderid")
            rec = f"{clean_name(owner)} · order {i + 1}"
            if _blank(oid):
                seed = "%s/%d/%r" % (owner, i, o2.get("cart"))     # stable: same order -> same recovery id
                o2["orderid"] = "recovered-" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:12]
                iss.add("order", rec, "orderid", "null", "Every order needs an id", oid, o2["orderid"],
                        "Assigned a recovery id", key=f"{owner}#{i}")
            elif oid in seen_oid:
                iss.add("order", rec, "orderid", "duplicate", "An order is stored once",
                        f"{oid} (also in {seen_oid[oid]})", "(removed)", "Removed the duplicate copy",
                        key=f"{owner}#{i}")
                order_bad += 1
                continue
            seen_oid[o2["orderid"]] = rec
            cart = o2.get("cart") if isinstance(o2.get("cart"), dict) else {}
            o2["cart"] = cart
            items = cart.get("items") if isinstance(cart.get("items"), list) else []
            cart["items"] = items
            needs_review = []
            for j, it in enumerate(items):
                sku, qty, price, sub = it.get("sku"), it.get("qty"), it.get("price"), it.get("subtotal")
                where = f"{rec} · item {j + 1}" + (f" ({sku})" if not _blank(sku) else "")
                if _blank(sku):
                    iss.add("order", where, "sku", "null", "Every item needs a product", sku, None,
                            "Quarantined: product unknown", fixable=False, key=f"{owner}#{i}")
                    needs_review.append("missing sku")
                    continue
                if sku != SHIPPING_SKU and sku not in catalogue:
                    iss.add("order", where, "sku", "inconsistency", "Product must exist in the catalogue", sku, None,
                            "Quarantined: unknown product", fixable=False, key=f"{owner}#{i}")
                    needs_review.append("unknown product")
                    continue
                if qty is None:
                    iss.add("order", where, "qty", "null", "Quantity is required", qty, None,
                            "Quarantined: quantity unknown", fixable=False, key=f"{owner}#{i}")
                    needs_review.append("missing qty")
                    continue
                q = _num(qty)
                if q is None or q <= 0 or q != int(q):
                    iss.add("order", where, "qty", "inconsistency", "Quantity is a whole number above 0", qty, None,
                            "Quarantined: invalid quantity", fixable=False, key=f"{owner}#{i}")
                    needs_review.append("invalid qty")
                    continue
                ref = catalogue.get(sku) if sku != SHIPPING_SKU else None
                p = _num(price)
                if p is None:
                    if ref is None:
                        iss.add("order", where, "price", "null", "Price is required", price, None,
                                "Quarantined: no reference price", fixable=False, key=f"{owner}#{i}")
                        needs_review.append("missing price")
                        continue
                    it["price"] = ref
                    iss.add("order", where, "price", "null", "Price is required", price, ref,
                            "Filled from the catalogue", key=f"{owner}#{i}")
                elif ref is not None and abs(p - ref) > TOL:
                    it["price"] = ref
                    iss.add("order", where, "price", "discrepancy", "Price matches the catalogue", price, ref,
                            "Corrected to the catalogue price", key=f"{owner}#{i}")
                want = _money(float(it["price"]) * int(q))
                s = _num(sub)
                if s is None or abs(s - want) > TOL:
                    kind = "null" if s is None else "discrepancy"
                    iss.add("order", where, "subtotal", kind, "Subtotal = price × qty", sub, want,
                            "Recomputed", key=f"{owner}#{i}")
                it["subtotal"] = want
            if needs_review:
                o2["dq_status"] = REVIEW
                quarantined_orders += 1
            else:
                total = _money(sum(float(it.get("subtotal") or 0) for it in items))
                t = _num(cart.get("total"))
                if t is None or abs(t - total) > TOL:
                    iss.add("order", rec, "total", "null" if t is None else "discrepancy",
                            "Total = sum of item subtotals", cart.get("total"), total, "Recomputed", key=f"{owner}#{i}")
                cart["total"] = total
                tax = tax_for(total)
                x = _num(cart.get("tax"))
                if x is None or abs(x - tax) > TOL:
                    iss.add("order", rec, "tax", "null" if x is None else "discrepancy",
                            "Tax = total − total ÷ 1.2 (20 % included)", cart.get("tax"), tax, "Recomputed",
                            key=f"{owner}#{i}")
                cart["tax"] = tax
            if len(iss.items) > before_n:
                order_bad += 1
            bucket.append(o2)

    # ---- the cleaned customer list: survivors only, flagged when a human must act
    clean_customers = []
    for c in cust:
        if c["id"] in merged_into:
            continue
        f = dict(fixed_c[c["id"]])
        if c["id"] in quarantine_c:
            f["dq_status"] = REVIEW
            f["dq_reasons"] = quarantine_c[c["id"]]
        clean_customers.append(f)

    cust_bad = len({i["key"] for i in iss.items if i["entity"] == "customer"})
    records = len(cust) + order_total
    by_type = {t: sum(1 for i in iss.items if i["type"] == t) for t in ISSUE_TYPES}
    clean_records = records - cust_bad - order_bad
    summary = {
        "customers": len(cust), "orders": order_total, "records": records,
        "issues": len(iss.items), "by_type": by_type,
        "fixable": sum(1 for i in iss.items if i["fixable"]),
        "quarantined": sum(1 for i in iss.items if not i["fixable"]),
        "clean_records": max(0, clean_records),
        "quality_score": round(100.0 * max(0, clean_records) / records, 1) if records else 100.0,
        "duplicates_merged": len(merged_into),
        "quarantined_customers": len(quarantine_c), "quarantined_orders": quarantined_orders,
    }
    return {"summary": summary, "issues": iss.items, "merged_into": merged_into,
            "clean": {"customers": clean_customers, "orders": fixed_orders}, "names": name_of}


def residual_summary(after: Dict[str, Any]) -> Dict[str, Any]:
    """After the fix only quarantined (flagged) problems may remain; anything else is a bug in the fixer."""
    s = after["summary"]
    open_fixable = [i for i in after["issues"] if i["fixable"]]
    return {**s, "open_fixable": len(open_fixable)}


def plan_writes(customers: List[Dict[str, Any]], orders: Dict[str, List[Dict[str, Any]]],
                analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Database operations that turn the snapshot into the cleaned data. Only touches records in the snapshot.
    Order matters: customer updates, duplicate removals, then order histories (old names removed before the
    cleaned history is written under the customer's final name)."""
    ops: List[Dict[str, Any]] = []
    by_id = {c["id"]: c for c in customers}
    for c in analysis["clean"]["customers"]:
        old = by_id[c["id"]]
        st = {k: c[k] for k in ("name", "email") if c.get(k) != old.get(k)}
        if c.get("dq_status") != old.get("dq_status"):
            st["dq_status"] = c.get("dq_status")
        if st:
            ops.append({"op": "update_customer", "id": c["id"], "set": st})
    for dup in analysis["merged_into"]:
        ops.append({"op": "delete_customer", "id": dup})
    new_orders = analysis["clean"]["orders"]
    for name in orders:
        if name not in new_orders:
            ops.append({"op": "delete_orders", "name": name})
    for name, hist in new_orders.items():
        if orders.get(name) != hist:
            ops.append({"op": "set_orders", "name": name, "history": hist})
    return ops


def mask_email(e: Any) -> Any:
    """For the screen: keep the problem visible (case, spaces, '@@') but hide most of the address."""
    if e is None or not isinstance(e, str):
        return e
    if not e.strip():
        return "(empty)"
    lead = "␣" * (len(e) - len(e.lstrip()))
    trail = "␣" * (len(e) - len(e.rstrip()))
    core = e.strip()
    if "@" in core:
        local, _, domain = core.partition("@")
        return f"{lead}{local[:2]}***@{domain}{trail}"
    return f"{lead}{core[:2]}***{trail}"


def for_screen(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Issues and records with emails masked; spaces shown as ␣ so whitespace problems stay visible."""
    def show(field, v):
        if field == "email":
            return mask_email(v)
        if isinstance(v, str) and v != v.strip():
            return v.replace(" ", "␣")
        return v
    issues = []
    for i in analysis["issues"]:
        j = {k: v for k, v in i.items() if k != "key"}
        j["before"], j["after"] = show(i["field"], i["before"]), show(i["field"], i["after"])
        issues.append(j)
    return {"summary": analysis["summary"], "issues": issues}
