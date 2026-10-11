# Findings Report: AI Chaos Engineering on Stan's Robot Shop

Generated 2026-10-11T08:01:22+00:00 by the Genesis multi-agent pipeline (Planner -> Analyzer -> Recommender) from 33 measured run(s) in `runs/`. Every number below is measured or computed by Python; the agents explain, rank and recommend.

## Executive summary

- **Overall resilience score: 4.93/10** (mean of per-experiment scores, `controller/scorer.py`)
- Experiments: 5 distinct, 33 runs, failure types: container_crash, network_latency, rabbitmq_block
- Services tested: 4/13
- The e-commerce application demonstrates high vulnerability to core component disruptions, yielding an overall resilience score of 4.93. Critical infrastructure crashes in catalogue, mongodb, and payment consistently trigger 100.0% checkout failure rates, primary driven by missing resilience patterns for downstream database and HTTP dependencies.

## Ranked weaknesses (by severity)

| # | Experiment | Severity | Peak error | p95 peak / baseline | Detect | Recover | Resilience |
|---|---|---|---|---|---|---|---|
| 1 | `container_crash` on `catalogue` | **Critical** (86.9) | 100.0% | 60000.0 / 39 ms | 68.7 s | 26.9 s | 1.82/10 |
| 2 | `container_crash` on `mongodb` | **Critical** (82.2) | 100.0% | 60000.0 / 23 ms | 67.7 s | 15.6 s | 1.9/10 |
| 3 | `container_crash` on `payment` | **Critical** (77.2) | 100.0% | 37000.0 / 47 ms | 55.7 s | 16.0 s | 5.39/10 |
| 4 | `rabbitmq_block` on `rabbitmq` | **High** (52.6) | 7.5% | 150.0 / 77 ms | 31.3 s | 10.4 s | 6.85/10 |
| 5 | `network_latency` on `payment_to_gateway` | **Medium** (39.4) | 0.0% | 84 / 38 ms | 16.4 s | 15.8 s | 8.68/10 |

### 1. `container_crash` on `catalogue` (Critical)

The catalogue container crashed, causing health probe timeouts at +5.2 s and severe request delays with a p95 of 60000.0 ms. Because downstream services rely on the catalogue service, endpoints such as cart:add, catalogue:categories, catalogue:product, and ratings:rate failed. Consequently, users experienced a 100.0% peak error rate and lost 63.7 checkouts in the sandbox.

Proof the service was down: `GET /api/catalogue/health` through the shop gateway returned **timeout** at +5.2 s (container: robotshop-catalogue-1).

### 2. `container_crash` on `mongodb` (Critical)

A container crash in mongodb severed the database connection required by the catalogue service. This downstream dependency failure led to extreme latency, with catalogue:categories reaching a mean of 60002.4 ms and a p95 of 60000.0 ms. Users faced a 100.0% worst checkout failure rate and 69.1 lost checkouts due to failing cart and catalogue endpoints.

### 3. `container_crash` on `payment` (Critical)

Crashing the payment container caused payment health probes to time out at +5.2 s and pushed p95 latencies to 37000.0 ms. The root cause was the complete unavailability of the payment service, directly blocking payment processing. As a result, the payment:pay endpoint failed, leading to a 100.0% worst checkout failure rate and 26.6 lost checkouts.

Proof the service was down: `GET /api/payment/health` through the shop gateway returned **timeout** at +5.2 s (container: robotshop-payment-1).

Run-to-run (7 runs): **no significant change**.

### 4. `rabbitmq_block` on `rabbitmq` (High)

Blocking traffic on rabbitmq prevented message processing required during checkout operations. Because the payment service depends on rabbitmq for handling transaction messages, the payment:pay endpoint experienced failure. Users encountered a 100.0% worst checkout failure and 34.5 lost checkouts despite overall response times remaining relatively low at a p95 of 150.0 ms.

Run-to-run (6 runs): **no significant change**.

### 5. `network_latency` on `payment_to_gateway` (Medium)

Injecting network latency on the edge between payment and gateway slowed response times, causing payment:pay to average 4179.3 ms compared to its 14.6 ms baseline. The root cause was delayed network communication to the payment gateway, triggering health probe timeouts at +5.2 s. Although no individual requests reported hard errors, users suffered a worst checkout failure rate of 48.4% and 8.4 lost checkouts.

Proof the service was down: `GET /api/payment/health` through the shop gateway returned **timeout** at +5.2 s (container: n/a).

Run-to-run (7 runs): **no significant change**.

## Fix verification (re-run after applying a fix)

Each fix phase is compared with the runs before it, all runs pooled (two-proportion z-test).

| Experiment | Fix | Runs before / after | Error-minutes | Fault error rate | Resilience | p | Verdict |
|---|---|---|---|---|---|---|---|
| `container_crash` on `catalogue` | fix: catalogue x2 replicas | 4 / 2 | 0.1415 -> 0.0015 min (-98.9%) | 3.8% -> 0.0% | 8.35 -> 5.90 | < 0.000001 | **improved** |

Side effect found by the re-run: with 'fix: catalogue x2 replicas', `catalogue:product` hung at 51436.6 ms (7913.3x its 6.5 ms baseline) while traffic failed over, which is why the resilience score fell. The errors are gone; the next fix is a short gateway connect timeout.

## Recommended fixes (prioritised)

| Priority | Weakness | Fix | Protects (per year) | Per incident |
|---|---|---|---|---|
| 1 | `container_crash` on `mongodb` (Critical) | **Implement Circuit Breaker** | USD 21,679 | USD 3,613 |
| 2 | `container_crash` on `catalogue` (Critical) | **Adjust Timeouts** | USD 16,053 | USD 2,676 |
| 3 | `container_crash` on `payment` (Critical) | **Increase Replicas** | USD 10,345 | USD 1,724 |
| 4 | `rabbitmq_block` on `rabbitmq` (High) | **Buffer Writes in a Durable Queue** | USD 10,818 | USD 1,803 |
| 5 | `network_latency` on `payment` (Medium) | **Adjust Timeouts** | USD 2,399 | USD 400 |

**1. Implement Circuit Breaker (mongodb)**: Run at least 2 replicas of mongodb, and put a circuit breaker with a cached fallback in its callers (cart, catalogue) so they degrade instead of failing.  
Verify: `python scripts/day1_demo.py --fault container_crash --target mongodb --label "fix: implement circuit breaker"  (after applying the fix; the Experiments tab then compares before vs after)`

**2. Adjust Timeouts (catalogue)**: Keep the replicas ('fix: catalogue x2 replicas' removed the errors) and give the gateway a short upstream connect timeout with retry to the next replica: during failover catalogue:product hung at 51436.6 ms (7913.3x its baseline) before succeeding.  
Verify: `python scripts/day1_demo.py --fault container_crash --target catalogue --label "fix: adjust timeouts"  (after applying the fix; the Experiments tab then compares before vs after)`

**3. Increase Replicas (payment)**: Run at least 2 replicas of payment behind the gateway with health checks, so one crashed container no longer takes the endpoint down.  
Verify: `python scripts/day1_demo.py --fault container_crash --target payment --label "fix: increase replicas"  (after applying the fix; the Experiments tab then compares before vs after)`

**4. Buffer Writes in a Durable Queue (rabbitmq)**: Have payment write orders to a local outbox (retry + dead-letter queue) when the broker is unavailable, so a queue outage delays dispatch instead of failing checkout.  
Verify: `python scripts/day1_demo.py --fault rabbitmq_block --target rabbitmq --label "fix: buffer writes in a durable queue"  (after applying the fix; the Experiments tab then compares before vs after)`

**5. Adjust Timeouts (payment)**: Give payment's calls to gateway a timeout well below the user-facing budget, with a bounded retry and a circuit breaker, so a slow gateway fails fast instead of holding requests.  
Verify: `python scripts/day1_demo.py --fault network_latency --target payment_to_gateway --params '{"jitter_ms": 200, "latency_ms": 2000}' --label "fix: adjust timeouts"  (after applying the fix; the Experiments tab then compares before vs after)`

## Next experiments (Planner)

Method: score = 100 x (0.40 x revenue proximity [1/(1+hops from payment)] + 0.20 x blast radius [services that break / all services] + 0.25 x novelty [1 never tested, 0.3 service tested with another fault, 0 already run] + 0.15 x new fault type); ties go to the experiment closer to payment; select the highest, one per fault type, up to 4 experiments in total.

| Rank | Experiment | Score | Reason |
|---|---|---|---|
| 1 | `container_crash:cart` | 50.0 | With score 50.0 and 1 hop to payment, crashing cart tests checkout resilience by potentially breaking web, shipping, and payment. |
| 2 | `network_latency:cart_to_catalogue` | 50.0 | Scoring 50.0 at 1 hop to payment, this reveals latency impacts on checkout by potentially breaking web, shipping, and payment. |
| 3 | `network_latency:cart_to_redis` | 50.0 | With a 50.0 score and 1 hop to payment, this evaluates cart caching failures that can break web, shipping, and payment. |
| 4 | `network_latency:payment_to_rabbitmq` | 49.2 | Directly on payment with 0 hops and score 49.2, it tests immediate checkout revenue degradation that can break web. |
| 5 | `container_crash:payment-gateway` | 48.3 | Scoring 48.3 at 1 hop to payment, crashing payment-gateway exposes external checkout failures that can break payment and web. |
| 6 | `container_crash:user` | 48.3 | With score 48.3 and 1 hop to payment, this tests user session dependencies on checkout that can break web and payment. |
| 7 | `network_latency:user_to_mongodb` | 48.3 | Scoring 48.3 at 1 hop to payment, introducing database latency checks user profile checkout flow breaking web and payment. |
| 8 | `disk_fill:mongodb` | 47.5 | Scoring 47.5, this high blast radius test reveals checkout storage tolerance, potentially breaking catalogue, user, web, payment, cart, ratings, and shipping. |

## Run-to-run improvement

- `container_crash:catalogue`: 12 run(s), first -> latest error-minutes +13.7%, verdict **regressed** (p = 0.000282)
- `network_latency:payment_to_gateway:{"jitter_ms": 200, "latency_ms": 2000}`: 7 run(s), first -> latest error-minutes +0.0%, verdict **no significant change** (p = 1.0)
- `rabbitmq_block:rabbitmq`: 6 run(s), first -> latest error-minutes +154.6%, verdict **no significant change** (p = 0.715)
- `container_crash:payment`: 7 run(s), first -> latest error-minutes +400.0%, verdict **no significant change** (p = 0.093)
- `container_crash:mongodb`: 1 run(s), single run

## Business assumptions

120 orders/min, average order USD 45, 40% of customers retry, 6 incidents/year, SLO 99.90% (`controller/insights/business_config.json`).

## How this report was produced (agents)

| Agent | Role | Mode | Model | Time | Notes |
|---|---|---|---|---|---|
| planner | Ranks and selects chaos experiments from the dependency map, one reason per experiment | llm | gemini-3.8-flash | 0.07 s | - |
| analyzer | Explains each experiment's findings, ranks weaknesses by severity, gives a resilience score | llm | gemini-3.6-flash | 9.86 s | - |
| recommender | Recommends one prioritised, verifiable fix per weakness, with the money it protects | llm | gemini-3.6-flash | 2.3 s | - |

Source runs: day1_20261010_153655, day1_20261010_153826, day1_20261010_154738, day1_20261010_171315, day1_20261010_225203, day1_20261010_225315, day1_20261010_225519, day1_20261010_231011, day1_20261010_231230, day1_20261011_020947, day1_20261011_023112, day1_20261011_023425, day1_20261011_030826, day1_20261011_031546, day1_20261011_033931, day1_20261011_035458, day1_20261011_035621, day1_20261011_035739, day1_20261011_035922, day1_20261011_074557, day1_20261011_074720, day1_20261011_075037, day1_20261011_080210, day1_20261011_080333, day1_20261011_080521, day1_20261011_080702, day1_20261011_093921, day1_20261011_095204, day1_20261011_095333, day1_20261011_132526, day1_20261011_132649, day1_20261011_132827, day1_20261011_133109
