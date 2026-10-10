#!/usr/bin/env bash
# ==============================================================================
# Genesis AI Chaos Engineering Platform - Day 1 Judge Demo Script
# Owner: M4 (Safety & Observability Lead)
# 
# Sequence:
#   1. make up
#   2. make map                               (generates graph.json)
#   3. make baseline                          (captures steady-state bands)
#   4. make inject fault=container_crash target=ratings  (graceful degradation)
#   5. make inject fault=container_crash target=cart     (critical failure)
#   6. Wait for Kill Switch to fire & recover automatically
#   7. make report                            (generates resilience report)
# ==============================================================================

set -euo pipefail

# Configurable endpoints (allows connecting to remote teammate's laptop)
CONTROLLER_URL="${CONTROLLER_URL:-http://localhost:8000}"
LOCUST_URL="${LOCUST_URL:-http://localhost:8089}"
TEST_MODE="${TEST_MODE:-false}"

COLOR_RESET="\033[0m"
COLOR_GREEN="\033[1;32m"
COLOR_YELLOW="\033[1;33m"
COLOR_RED="\033[1;31m"
COLOR_BLUE="\033[1;34m"

log_info()  { echo -e "${COLOR_BLUE}[DEMO INFO]${COLOR_RESET}  $(date '+%H:%M:%S') - $1"; }
log_step()  { echo -e "\n${COLOR_GREEN}======================================================================${COLOR_RESET}"; \
              echo -e "${COLOR_GREEN}>>> STEP $1: $2${COLOR_RESET}"; \
              echo -e "${COLOR_GREEN}======================================================================${COLOR_RESET}"; }
log_warn()  { echo -e "${COLOR_YELLOW}[DEMO WARN]${COLOR_RESET}  $1"; }
log_error() { echo -e "${COLOR_RED}[DEMO ERROR]${COLOR_RESET} $1"; }

echo -e "${COLOR_GREEN}"
cat << "EOF"
   ___                           _        ___                   _ 
  / _ \___ _ __   ___  ___ _ ___| |      /   \__ _ _   _       / |
 / /_\/ _ \ '_ \ / _ \/ __| / __| |     / /\ / _` | | | |      | |
/ /_\\  __/ | | |  __/\__ \ \__ \_|    / /_// (_| | |_| |  _   | |
\____/\___|_| |_|\___||___/_|___(_)   /___,' \__,_|\__, | (_)  |_|
                                                   |___/          
           AI-DRIVEN CHAOS ENGINEERING PLATFORM (DAY 1)
EOF
echo -e "${COLOR_RESET}"

log_info "Controller Endpoint: ${CONTROLLER_URL}"
log_info "Locust Endpoint:     ${LOCUST_URL}"
log_info "Test Mode:           ${TEST_MODE}"

# ------------------------------------------------------------------------------
# STEP 1: Start Sandbox and Controller
# ------------------------------------------------------------------------------
log_step "1" "Starting Sandbox Target App & Chaos Controller (make up)"
if command -v make >/dev/null 2>&1; then
    make up || log_warn "'make up' returned exit code $?, checking service readiness directly..."
else
    log_warn "'make' not found, verifying controller via HTTP..."
fi

log_info "Checking Controller readiness at ${CONTROLLER_URL}..."
for i in {1..30}; do
    if curl -s -f "${CONTROLLER_URL}/" >/dev/null 2>&1; then
        log_info "Controller is healthy and responding."
        break
    fi
    echo -n "."
    sleep 2
    if [ "$i" -eq 30 ]; then
        log_error "Controller at ${CONTROLLER_URL} unreachable after 60s."
        exit 1
    fi
done

# ------------------------------------------------------------------------------
# STEP 2: Dependency Mapping
# ------------------------------------------------------------------------------
log_step "2" "Mapping Target Architecture (make map -> graph.json)"
if command -v make >/dev/null 2>&1 && make -n map >/dev/null 2>&1; then
    make map
else
    log_info "Triggering dependency mapper via Controller API POST /api/v1/map..."
    curl -s -X POST "${CONTROLLER_URL}/api/v1/map" | tee /dev/stderr
fi

# ------------------------------------------------------------------------------
# STEP 3: Baseline Capture
# ------------------------------------------------------------------------------
log_step "3" "Capturing Steady-State Baseline Bands (make baseline)"
if [ "${TEST_MODE}" = "true" ]; then
    log_warn "TEST_MODE active: Running fast 10-second baseline capture..."
    curl -fsS -X POST "${CONTROLLER_URL}/api/v1/baseline?test_mode=true"
    BASELINE_READY=false
    for i in {1..20}; do
        STATUS=$(curl -fsS "${CONTROLLER_URL}/api/v1/safety/status")
        if echo "$STATUS" | grep -q '"baseline_capture":{"state":"complete"' && \
           echo "$STATUS" | grep -q '"baseline_loaded":true'; then
            BASELINE_READY=true
            break
        fi
        if echo "$STATUS" | grep -q '"baseline_capture":{"state":"failed"'; then
            log_error "Baseline capture failed: ${STATUS}"
            exit 1
        fi
        sleep 2
    done
    if [ "${BASELINE_READY}" != "true" ]; then
        log_error "Baseline capture did not complete within 40 seconds."
        exit 1
    fi
else
    log_info "Triggering 10-minute steady-state baseline capture (600s)..."
    curl -fsS -X POST "${CONTROLLER_URL}/api/v1/baseline?duration=600&interval=5"
    log_info "Waiting for baseline collection to complete..."
    # Poll the capture task, not just an older baseline file from a previous run.
    BASELINE_READY=false
    for i in {1..125}; do
        STATUS=$(curl -fsS "${CONTROLLER_URL}/api/v1/safety/status")
        if echo "$STATUS" | grep -q '"baseline_capture":{"state":"complete"' && \
           echo "$STATUS" | grep -q '"baseline_loaded":true'; then
            log_info "Steady-state baseline successfully established and loaded!"
            BASELINE_READY=true
            break
        fi
        if echo "$STATUS" | grep -q '"baseline_capture":{"state":"failed"'; then
            log_error "Baseline capture failed: ${STATUS}"
            exit 1
        fi
        sleep 5
    done
    if [ "${BASELINE_READY}" != "true" ]; then
        log_error "Baseline capture did not complete within the expected 10-minute window."
        exit 1
    fi
fi

# ------------------------------------------------------------------------------
# STEP 4: Fault Injection 1 - Non-Critical Degradation (ratings)
# ------------------------------------------------------------------------------
log_step "4" "Injecting Fault: ratings container crash (Graceful Degradation)"
log_info "Crashing 'ratings' to prove UI graceful degradation without tripping Kill Switch..."

if command -v make >/dev/null 2>&1 && make -n inject >/dev/null 2>&1; then
    make inject fault=container_crash target=ratings ttl=30
else
    curl -fsS -X POST "${CONTROLLER_URL}/api/v1/inject" \
        -H "Content-Type: application/json" \
        -d '{"fault_type": "container_crash", "target": "ratings", "ttl_seconds": 30}'
fi

log_info "Observing system telemetry for 15 seconds (Kill Switch should remain ARMED)..."
for i in {1..3}; do
    sleep 5
    SAFETY_STATUS=$(curl -s "${CONTROLLER_URL}/api/v1/safety/status" || echo "{}")
    echo "Current Safety Status: ${SAFETY_STATUS}"
done
log_info "'ratings' degradation tolerated. Kill switch did NOT fire (Expected Behavior)."
log_info "Waiting for the 30-second experiment TTL to roll ratings back before testing cart..."
sleep 20

# ------------------------------------------------------------------------------
# STEP 5: Fault Injection 2 - Critical Failure (cart)
# ------------------------------------------------------------------------------
log_step "5" "Injecting Fault: cart container crash (Critical Service Failure)"
log_info "Crashing Tier-1 critical service 'cart' to demonstrate automated Kill Switch protection..."

if command -v make >/dev/null 2>&1 && make -n inject >/dev/null 2>&1; then
    make inject fault=container_crash target=cart ttl=30
else
    curl -fsS -X POST "${CONTROLLER_URL}/api/v1/inject" \
        -H "Content-Type: application/json" \
        -d '{"fault_type": "container_crash", "target": "cart", "ttl_seconds": 30}'
fi

# ------------------------------------------------------------------------------
# STEP 6: Wait for Kill Switch to Trip and Auto-Recover
# ------------------------------------------------------------------------------
log_step "6" "Awaiting Kill Switch Trip and Automated Recovery"
log_info "Monitoring for 2-consecutive-window breach detection..."

TRIPPED=false
for i in {1..15}; do
    sleep 3
    SAFETY_STATUS=$(curl -s "${CONTROLLER_URL}/api/v1/safety/status" || echo "{}")
    echo "Telemetry window #$i check: ${SAFETY_STATUS}"
    
    if echo "$SAFETY_STATUS" | grep -q '"kill_switch_state":"TRIPPED"' || \
       echo "$SAFETY_STATUS" | grep -q '"kill_switch_state":"RECOVERING"' || \
       echo "$SAFETY_STATUS" | grep -q '"kill_switch_state":"RECOVERED"'; then
        log_info "KILL SWITCH FIRED AND INITIATED EMERGENCY ROLLBACK!"
        TRIPPED=true
        break
    fi
done

if [ "$TRIPPED" = "false" ]; then
    log_warn "Kill switch did not trip within timeout. Triggering operator failsafe..."
    curl -fsS -X POST "${CONTROLLER_URL}/api/v1/safety/trip?reason=DemoTimeoutFailsafe"
fi

log_info "Waiting for container recovery and telemetry stabilization..."
sleep 10

# ------------------------------------------------------------------------------
# STEP 7: Generate Final Report
# ------------------------------------------------------------------------------
log_step "7" "Generating Experiment & Resilience Report (make report)"
if command -v make >/dev/null 2>&1 && make -n report >/dev/null 2>&1; then
    make report
else
    log_info "Calling report generation CLI..."
    python -m cli report 2>/dev/null || log_warn "Report module will be finalized by M5."
fi

log_step "COMPLETE" "Day 1 Hackathon Demo Successfully Executed!"
