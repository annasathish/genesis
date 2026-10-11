# Findings Report: AI Chaos Engineering on Stan's Robot Shop

Generated 2026-10-11T02:37:20+00:00 by the Genesis multi-agent pipeline (Planner -> Analyzer -> Recommender) from 26 measured run(s) in `runs/`. Every number below is measured or computed by Python; the agents explain, rank and recommend.

## Executive summary

- **Overall resilience score: 4.99/10** (mean of per-experiment scores, `controller/scorer.py`)
- Experiments: 5 distinct, 26 runs, failure types: container_crash, network_latency, rabbitmq_block
- Services tested: 4/13
- The e-commerce application exhibits severe fragility around its core transactional and catalog paths, yielding an overall resilience score of 4.99. Disruptions to backend dependencies like mongodb, catalogue, payment, and rabbitmq consistently trigger 100.0% worst checkout failure rates and severe latency degradation.

## Ranked weaknesses (by severity)

| # | Experiment | Severity | Peak error | p95 peak / baseline | Detect | Recover | Resilience |
|---|---|---|---|---|---|---|---|
| 1 | `container_crash` on `mongodb` | **Critical** (82.2) | 100.0% | 60000.0 / 23 ms | 67.7 s | 15.6 s | 1.9/10 |
| 2 | `container_crash` on `catalogue` | **Critical** (78.4) | 37.5% | 7900.0 / 36 ms | 16.5 s | 15.9 s | 4.56/10 |
| 3 | `container_crash` on `payment` | **Critical** (66.8) | 0.0% | 260.0 / 27 ms | 24.7 s | 15.8 s | 5.89/10 |
| 4 | `rabbitmq_block` on `rabbitmq` | **Critical** (63.2) | 7.9% | 83 / 41 ms | 41.6 s | 10.4 s | 6.71/10 |
| 5 | `network_latency` on `payment_to_gateway` | **High** (51.1) | 0.0% | 2200.0 / 51 ms | 16.4 s | 15.9 s | 5.89/10 |

### 1. `container_crash` on `mongodb` (Critical)

The mongodb container crashed, taking down the primary datastore relied upon by the catalogue and user services. This caused severe cascading failures where catalogue:categories degraded to 60002.4 ms mean latency and peak error rates reached 100.0%. Users faced a complete checkout failure of 100.0% alongside 69.1 checkouts lost in the sandbox and extreme latency reaching a p95 of 60000.0 ms (baseline 23 ms).

### 2. `container_crash` on `catalogue` (Critical)

A crash of the catalogue container caused its health probe to timeout at +5.2 s, disrupting dependent upstream services including cart and ratings. The cart:add endpoint slowed significantly to 6916.2 ms mean vs 9.6 ms baseline (720.4x) with 77 failed requests recorded. Users experienced a 100.0% worst checkout failure rate, a peak error rate of 37.5%, and an overall p95 latency spike to 7900.0 ms (baseline 36 ms).

Proof the service was down: `GET /api/catalogue/health` through the shop gateway returned **timeout** at +5.2 s (container: robotshop-catalogue-1).

### 3. `container_crash` on `payment` (Critical)

Crashing the payment container caused health check timeouts at +5.2 s for the payment service. Because checkout flows depend on the payment service, users suffered a 100.0% worst checkout failure rate with 12.0 checkouts lost in the sandbox and 39 failed requests. However, system-wide peak error rates remained at 0.0% while p95 latency increased modestly to 260.0 ms (baseline 27 ms).

Proof the service was down: `GET /api/payment/health` through the shop gateway returned **timeout** at +5.2 s (container: robotshop-payment-1).

Run-to-run (4 runs): **no significant change**.

### 4. `rabbitmq_block` on `rabbitmq` (Critical)

Blocking the rabbitmq message broker disrupted asynchronous communication paths from payment and dispatch. This direct dependency failure caused payment:pay to fail and shipping:match to slow down to 857.2 ms mean vs 143.5 ms baseline (6.0x). Consequently, users experienced a 100.0% worst checkout failure rate, 40.9 checkouts lost in the sandbox, and 47 failed requests.

Run-to-run (5 runs): **no significant change**.

### 5. `network_latency` on `payment_to_gateway` (High)

Injecting 2000 ms of network latency with 200 ms jitter between payment and gateway induced upstream health probe timeouts at +5.2 s. The payment:pay endpoint experienced significant delay, surging to 4925.7 ms mean vs 16.9 ms baseline (291.5x) and elevating overall p95 latency to 2200.0 ms (baseline 51 ms). As a result, users experienced a 77.7% worst checkout failure rate and 4.0 lost checkouts in the sandbox.

Proof the service was down: `GET /api/payment/health` through the shop gateway returned **timeout** at +5.2 s (container: n/a).

Run-to-run (5 runs): **no significant change**.

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
| 2 | `rabbitmq_block` on `rabbitmq` (Critical) | **Buffer Writes in a Durable Queue** | USD 13,660 | USD 2,277 |
| 3 | `container_crash` on `catalogue` (Critical) | **Adjust Timeouts** | USD 2,185 | USD 364 |
| 4 | `container_crash` on `payment` (Critical) | **Increase Replicas** | USD 3,426 | USD 571 |
| 5 | `network_latency` on `payment` (High) | **Adjust Timeouts** | USD 1,467 | USD 245 |

**1. Implement Circuit Breaker (mongodb)**: Configure a circuit breaker with cached fallbacks in callers like cart and catalogue when communicating with mongodb. This prevents cascading timeouts and complete request failure when mongodb becomes unavailable.  
Verify: `python scripts/day1_demo.py --fault container_crash --target mongodb --label "fix: implement circuit breaker"  (after applying the fix; the Experiments tab then compares before vs after)`

**2. Buffer Writes in a Durable Queue (rabbitmq)**: Have payment buffer order events in a local transactional outbox when rabbitmq is blocked or unavailable. This ensures checkout can proceed without dropping messages while delaying dispatch processing.  
Verify: `python scripts/day1_demo.py --fault rabbitmq_block --target rabbitmq --label "fix: buffer writes in a durable queue"  (after applying the fix; the Experiments tab then compares before vs after)`

**3. Adjust Timeouts (catalogue)**: Configure short upstream connect timeouts and quick failover retries to healthy replicas at the gateway. This avoids high latency spikes on catalogue endpoints during instance crashes.  
Verify: `python scripts/day1_demo.py --fault container_crash --target catalogue --label "fix: adjust timeouts"  (after applying the fix; the Experiments tab then compares before vs after)`

**4. Increase Replicas (payment)**: Run multiple replicas of the payment service behind the load balancer with active health checks. This ensures uninterrupted checkout processing if a single container crashes.  
Verify: `python scripts/day1_demo.py --fault container_crash --target payment --label "fix: increase replicas"  (after applying the fix; the Experiments tab then compares before vs after)`

**5. Adjust Timeouts (payment)**: Enforce strict call timeouts and bounded retries on the payment service's calls to the gateway. This enables fast failure and prevents requests from hanging when high network latency occurs.  
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

- `container_crash:catalogue`: 11 run(s), first -> latest error-minutes -61.8%, verdict **no significant change** (p = 0.43)
- `network_latency:payment_to_gateway:{"jitter_ms": 200, "latency_ms": 2000}`: 5 run(s), first -> latest error-minutes +0.0%, verdict **no significant change** (p = 1.0)
- `rabbitmq_block:rabbitmq`: 5 run(s), first -> latest error-minutes +236.4%, verdict **no significant change** (p = 0.776)
- `container_crash:payment`: 4 run(s), first -> latest error-minutes +8.0%, verdict **no significant change** (p = 0.000785)
- `container_crash:mongodb`: 1 run(s), single run

## Business assumptions

120 orders/min, average order USD 45, 40% of customers retry, 6 incidents/year, SLO 99.90% (`controller/insights/business_config.json`).

## How this report was produced (agents)

| Agent | Role | Mode | Model | Time | Notes |
|---|---|---|---|---|---|
| planner | Ranks and selects chaos experiments from the dependency map, one reason per experiment | llm | gemini-3.8-flash | 0.01 s | - |
| analyzer | Explains each experiment's findings, ranks weaknesses by severity, gives a resilience score | llm | gemini-3.8-flash | 11.17 s | - |
| recommender | Recommends one prioritised, verifiable fix per weakness, with the money it protects | llm | gemini-3.8-flash | 6.62 s | - |

Source runs: day1_20261010_153655, day1_20261010_153826, day1_20261010_154738, day1_20261010_171315, day1_20261010_225203, day1_20261010_225315, day1_20261010_225519, day1_20261010_231011, day1_20261010_231230, day1_20261011_020947, day1_20261011_023112, day1_20261011_023425, day1_20261011_030826, day1_20261011_031546, day1_20261011_033931, day1_20261011_035458, day1_20261011_035621, day1_20261011_035739, day1_20261011_035922, day1_20261011_074557, day1_20261011_074720, day1_20261011_075037, day1_20261011_080210, day1_20261011_080333, day1_20261011_080521, day1_20261011_080702
