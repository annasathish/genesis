"""Reads and writes the sandbox shop's customer data for the data-quality check (insights/dataquality.py).

Read:   through the shop's own API, like a client would (GET /api/user/users, /api/user/history/<name>,
        /api/catalogue/products). Passwords are dropped the moment a record is read.
Inject: demo customer input through the real customer endpoints (POST /api/user/register, /api/user/order/<name>);
        every demo record's name starts with "dq-" (any case/spaces), so it can be removed again.
Write:  the shop has no update API, so fixes go straight into the sandbox MongoDB with `docker exec ... mongo`.
        Only records present in the fresh snapshot are touched; a backup of every affected document is written to
        runs/dataquality/ first (git-ignored).
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SHOP_URL = os.environ.get("SHOP_URL", "http://127.0.0.1:8080")
MONGO_CONTAINER = os.environ.get("DQ_MONGO_CONTAINER", "robotshop-mongodb-1")
DEMO_PASSWORD = "dq-demo"            # demo customers only; never shown
_OID = re.compile(r"^[0-9a-f]{24}$")


class DataSourceError(RuntimeError):
    pass


def _http(method: str, path: str, body: Any = None, timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SHOP_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "null"
            return r.status, (json.loads(raw) if raw[:1] in "[{" else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DataSourceError(f"shop not reachable at {SHOP_URL}: {e}") from e


# The demo batch: what real customers could type into the shop today (the user service accepts all of it).
DEMO_CUSTOMERS = [
    {"name": "dq-alice", "email": "alice@example.com"},                       # clean reference record
    {"name": "DQ-Alice ", "email": " Alice@Example.COM "},                    # same person again: duplicate
    {"name": "dq-bob", "email": None},                                        # null email
    {"name": "dq-carol", "email": "carol@@example..com"},                     # typo: doubled separators
    {"name": "dq-dave", "email": ""},                                         # blank email
    {"name": "  dq-erin", "email": "ERIN@EXAMPLE.COM"},                       # stray spaces, upper case
    {"name": "dq-frank", "email": "frank-at-example"},                        # invalid, not repairable
]


def _item(sku, price, qty=1, subtotal=None, name="item"):
    return {"qty": qty, "sku": sku, "name": name, "price": price,
            "subtotal": price * qty if subtotal is None and price is not None and qty is not None else subtotal}


def demo_orders(catalogue: Dict[str, float]) -> List[Any]:
    """Orders built from the live catalogue so the clean ones are exactly right and the broken ones are wrong."""
    skus = sorted(catalogue, key=lambda s: catalogue[s])
    cheap, other = skus[0], skus[1] if len(skus) > 1 else skus[0]
    p1, p2 = catalogue[cheap], catalogue[other]
    ok = {"orderid": "dq-order-1001", "cart": {"items": [_item(cheap, p1)], "total": p1,
                                                "tax": round(p1 - p1 / 1.2, 2)}}
    return [
        ("dq-alice", ok),                                                     # correct order
        ("dq-alice", ok),                                                     # same order submitted twice
        ("DQ-Alice ", {"orderid": "dq-order-1002",                            # belongs to the duplicate customer
                       "cart": {"items": [_item(other, p2)], "total": p2, "tax": round(p2 - p2 / 1.2, 2)}}),
        ("dq-bob", {"orderid": "dq-order-1003",                               # price, subtotal, total, tax all off
                    "cart": {"items": [_item(other, p2 * 10, qty=2, subtotal=p2)], "total": p2 * 50, "tax": 1}}),
        ("dq-dave", {"cart": {"items": [_item(cheap, p1, qty=None, subtotal=p1)],   # no order id, no quantity
                              "total": p1, "tax": round(p1 - p1 / 1.2, 2)}}),
    ]


class ShopData:
    def __init__(self, mongo_container: str = MONGO_CONTAINER, docker_cmd=("docker",), backup_dir: Path = None):
        self.container, self.docker_cmd = mongo_container, tuple(docker_cmd)
        self.backup_dir = Path(backup_dir) if backup_dir else None

    # ------------------------------------------------------------------ read
    def snapshot(self) -> Dict[str, Any]:
        code, users = _http("GET", "/api/user/users")
        if code != 200 or not isinstance(users, list):
            raise DataSourceError(f"could not read customers from the shop (HTTP {code})")
        code, products = _http("GET", "/api/catalogue/products")
        if code != 200 or not isinstance(products, list):
            raise DataSourceError(f"could not read the catalogue from the shop (HTTP {code})")
        customers, orders = [], {}
        for u in users:
            customers.append({"id": str(u.get("_id")), "name": u.get("name"), "email": u.get("email"),
                              **({"dq_status": u["dq_status"]} if u.get("dq_status") else {})})
            if u.get("name") is None:
                continue
            code, hist = _http("GET", "/api/user/history/" + urllib.parse.quote(str(u["name"]), safe=""))
            if code == 200 and isinstance(hist, dict):
                orders[u["name"]] = hist.get("history") or []
        return {"customers": customers, "orders": orders,
                "catalogue": {p["sku"]: p["price"] for p in products if "sku" in p and "price" in p},
                "taken_at": time.time()}

    # ------------------------------------------------------------------ inject (the real customer path)
    def inject_demo(self) -> Dict[str, Any]:
        self.reset_demo()
        snap = self.snapshot()
        sent, refused = 0, []
        for c in DEMO_CUSTOMERS:
            code, msg = _http("POST", "/api/user/register", {"name": c["name"], "password": DEMO_PASSWORD,
                                                              "email": c["email"]})
            sent += code == 200
            if code != 200:
                refused.append(f"{c['name'].strip()}: {str(msg)[:60]}")
        for name, order in demo_orders(snap["catalogue"]):
            code, msg = _http("POST", "/api/user/order/" + urllib.parse.quote(name, safe=""), order)
            sent += code == 200
            if code != 200:
                refused.append(f"order for {name.strip()}: {str(msg)[:60]}")
        return {"accepted": sent, "refused": refused}

    def reset_demo(self) -> Dict[str, Any]:
        out = self._mongo("var r = /^\\s*dq-/i;"
                          "var u = db.users.deleteMany({name: r}).deletedCount;"
                          "var o = db.orders.deleteMany({name: r}).deletedCount;"
                          "print(JSON.stringify({customers: u, orders: o}));")
        return json.loads(out.strip().splitlines()[-1])

    # ------------------------------------------------------------------ write
    def apply(self, ops: List[Dict[str, Any]], snapshot: Dict[str, Any]) -> Dict[str, Any]:
        ids = {c["id"] for c in snapshot["customers"]}
        names = set(snapshot["orders"]) | {c["name"] for c in snapshot["customers"] if c.get("name")}
        lines = []
        for op in ops:
            if op["op"] in ("update_customer", "delete_customer"):
                if op["id"] not in ids or not _OID.match(op["id"]):
                    raise DataSourceError(f"refusing to touch unknown customer {op['id']!r}")
                sel = "{_id: ObjectId(%s)}" % json.dumps(op["id"])
                if op["op"] == "update_customer":
                    lines.append("db.users.updateOne(%s, {$set: %s});" % (sel, json.dumps(op["set"])))
                else:
                    lines.append("db.users.deleteOne(%s);" % sel)
            elif op["op"] == "delete_orders":
                if op["name"] not in names:
                    raise DataSourceError(f"refusing to touch unknown order history {op['name']!r}")
                lines.append("db.orders.deleteOne({name: %s});" % json.dumps(op["name"]))
            elif op["op"] == "set_orders":
                if op["name"] not in names:
                    raise DataSourceError(f"refusing to write orders for unknown customer {op['name']!r}")
                lines.append("db.orders.updateOne({name: %s}, {$set: {history: %s}}, {upsert: true});"
                             % (json.dumps(op["name"]), json.dumps(op["history"])))
        if not lines:
            return {"applied": 0, "backup": None}
        backup = self._backup(snapshot)
        self._mongo("\n".join(lines) + "\nprint('ok');")
        return {"applied": len(lines), "backup": backup}

    def _backup(self, snapshot: Dict[str, Any]) -> Optional[str]:
        """Full documents of everything in the snapshot (passwords included, so a restore is exact)."""
        if not self.backup_dir:
            return None
        names = [c["name"] for c in snapshot["customers"] if c.get("name") is not None]
        out = self._mongo("print(JSON.stringify({users: db.users.find({name: {$in: %s}}).toArray(),"
                          " orders: db.orders.find({name: {$in: %s}}).toArray()}));"
                          % (json.dumps(names), json.dumps(list(snapshot["orders"]))))
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        path = self.backup_dir / time.strftime("backup-%Y%m%d-%H%M%S.json")
        path.write_text(out.strip().splitlines()[-1], encoding="utf-8")
        return str(path)

    def _mongo(self, script: str) -> str:
        try:
            r = subprocess.run(list(self.docker_cmd) + ["exec", "-i", self.container, "mongo", "users", "--quiet"],
                               input=script, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DataSourceError(f"cannot reach the sandbox database: {e}") from e
        if r.returncode != 0:
            raise DataSourceError(f"sandbox database refused the change: {(r.stderr or r.stdout)[-200:]}")
        return r.stdout
