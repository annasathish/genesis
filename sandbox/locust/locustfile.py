"""Steady shopper load for Robot Shop.

Based on upstream load-gen/robot-shop.py (Apache-2.0), changed for chaos experiments:
  - every request has a stable `name=` so Locust stats aggregate per endpoint, not per id
  - JSON parsing is guarded, so a faulty service shows up as a failed request
    instead of crashing the simulated user
  - the full flow (browse -> cart -> shipping -> pay) is one task, so checkout
    success rate is a clean end-to-end SLI
  - no fake X-Forwarded-For IPs (they only fed Instana geo maps)
  - city lookup uses /match/<code>/<prefix> like the real UI's autocomplete, not
    /cities/<code> (unbounded: /cities/ru returns 216k rows and OOMs shipping)

Stats the Observer reads: GET http://127.0.0.1:8089/stats/requests
"""
import random

from locust import HttpUser, between, task

# country -> 3-letter city prefix (UI autocomplete needs >= 3 chars). Each verified against the
# Robot Shop cities dump: 7..1445 matching rows, so /match stays small (it returns <= 9).
CITY_PREFIX = {
    "gb": "lon", "us": "new", "de": "ber", "fr": "par", "in": "del", "es": "mad", "it": "rom",
    "ru": "mos", "br": "sao", "ca": "tor", "jp": "tok", "se": "sto", "nl": "ams", "tr": "ist", "cz": "pra",
}


def _json(resp):
    """Parse JSON or mark the request failed. Needs catch_response=True."""
    try:
        data = resp.json()
    except ValueError:
        resp.failure(f"non-JSON response (HTTP {resp.status_code})")
        return None
    if resp.status_code >= 400:
        resp.failure(f"HTTP {resp.status_code}")
        return None
    resp.success()
    return data


class Shopper(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.products = []

    def _uid(self):
        with self.client.get("/api/user/uniqueid", name="user:uniqueid", catch_response=True) as r:
            data = _json(r)
        return data.get("uuid") if data else None

    def _in_stock(self):
        if not self.products:
            with self.client.get("/api/catalogue/products", name="catalogue:products", catch_response=True) as r:
                self.products = [p for p in (_json(r) or []) if p.get("instock", 0) > 0]
        return self.products

    @task(5)
    def browse(self):
        self.client.get("/", name="web:index")
        self.client.get("/api/catalogue/categories", name="catalogue:categories")
        items = self._in_stock()
        if not items:
            return
        sku = random.choice(items)["sku"]
        self.client.get(f"/api/catalogue/product/{sku}", name="catalogue:product")
        self.client.get(f"/api/ratings/api/fetch/{sku}", name="ratings:fetch")

    @task(1)
    def rate(self):
        items = self._in_stock()
        if items:
            sku = random.choice(items)["sku"]
            self.client.put(f"/api/ratings/api/rate/{sku}/{random.randint(1, 5)}", name="ratings:rate")

    @task(1)
    def login(self):
        self.client.post("/api/user/login", json={"name": "user", "password": "password"}, name="user:login")

    @task(2)
    def checkout(self):
        uid = self._uid()
        items = self._in_stock()
        if not uid or not items:
            return

        for item in random.sample(items, k=min(2, len(items))):
            with self.client.get(f"/api/cart/add/{uid}/{item['sku']}/1", name="cart:add", catch_response=True) as r:
                if _json(r) is None:
                    return

        with self.client.get("/api/shipping/codes", name="shipping:codes", catch_response=True) as r:
            codes = _json(r)
        if not codes:
            return
        # The real UI never calls /cities/<code>: it autocompletes via /match/<code>/<3+ chars> (<= 9 results).
        # /cities/ru materialises 216k JPA rows (~200 MB heap) per request and OOM-kills shipping's Tomcat
        # threads at 10 users. That unbounded endpoint is a finding for the report, not baseline traffic.
        known = [c for c in codes if c.get("code") in CITY_PREFIX]
        if not known:
            return
        code = random.choice(known)

        with self.client.get(f"/api/shipping/match/{code['code']}/{CITY_PREFIX[code['code']]}",
                             name="shipping:match", catch_response=True) as r:
            cities = _json(r)
            if cities == []:
                r.failure("match returned no cities")
        if not cities:
            return
        city = random.choice(cities)

        with self.client.get(f"/api/shipping/calc/{city['uuid']}", name="shipping:calc", catch_response=True) as r:
            shipping = _json(r)
        if not shipping:
            return
        shipping["location"] = f"{code['name']} {city['name']}"

        with self.client.post(f"/api/shipping/confirm/{uid}", json=shipping, name="shipping:confirm", catch_response=True) as r:
            cart = _json(r)
        if not cart:
            return

        with self.client.post(f"/api/payment/pay/{uid}", json=cart, name="payment:pay", catch_response=True) as r:
            order = _json(r)
            if order is not None and "orderid" not in order:
                r.failure("no orderid in payment response")
