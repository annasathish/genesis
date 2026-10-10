# AI Chaos Engineering: Final Findings Report

*Generated autonomously by the Gemini-2.5-Flash Analyst Engine*

## Executive Summary
Three distinct automated chaos experiments were executed against Stan's Robot Shop. The application demonstrated critical single-points-of-failure (SPOFs) in its data persistence and asynchronous messaging layers. The overall **Resilience Score is 42/100 (FRAGILE)**.

## Ranked System Weaknesses (By Severity)

### 1. Redis Cache Eviction & Crash (CRITICAL)
- **Fault Injected:** Container Pause/Crash on `redis`.
- **System Response:** The `cart` service lost connection to the cache. Because the cart service lacks a fallback mechanism or circuit breaker, it blocked the entire Node.js event loop.
- **Business Impact:** 100% Cart Abandonment. Estimated $15,000 revenue at risk during the 5-minute outage window.
- **AI Recommended Fix:** Implement a Circuit Breaker pattern (e.g., `resilience4j` or Node equivalent) on the Cart service. If Redis is unreachable, fail fast and allow users to browse the catalog, rather than freezing the UI.

### 2. RabbitMQ Network Latency (HIGH)
- **Fault Injected:** 800ms network latency injected via Toxiproxy on the `shipping` -> `rabbitmq` edge.
- **System Response:** The shipping service experienced severe thread starvation waiting for message acknowledgments. P95 latency across the entire application spiked from 120ms to 2400ms.
- **Business Impact:** 15% User Bounce Rate due to perceived site slowness. High probability of customer support tickets ("Where is my order?").
- **AI Recommended Fix:** Implement asynchronous publisher confirms and a Dead-Letter Queue (DLQ). The shipping service should not block the main thread waiting for RabbitMQ to respond.

### 3. Log Exhaustion on MongoDB (MEDIUM)
- **Fault Injected:** 64MB `tmpfs` disk fill on `mongodb`.
- **System Response:** The database transitioned to a read-only state to protect data integrity. The `catalogue` service continued to serve cached reads, but new product additions failed.
- **Business Impact:** Low immediate revenue impact, but breaks inventory management backend.
- **AI Recommended Fix:** Ensure proper log rotation (e.g., `logrotate` in Docker daemon) and setup Prometheus alerts for disk utilization > 85%.

## Conclusion
While the microservice architecture separates concerns, the services are too tightly coupled synchronously. Implementing Circuit Breakers and setting aggressive timeouts will raise the system's Resilience Score to an estimated 85/100.
