#!/bin/bash
# apply_resilience_fix.sh
# This script fulfills the "Re-run experiments after applying a fix" stretch goal.
# It scales the shipping service to 3 replicas to handle traffic spikes and faults more gracefully.

set -e

echo "================================================="
echo "   Applying Resilience Fix (Stretch Goal)        "
echo "================================================="
echo ""
echo "[1/2] Modifying Docker Compose to scale the shipping service..."

# We use docker compose up --scale to increase replicas dynamically without tearing down the network.
cd sandbox
docker compose up -d --scale shipping=3

echo ""
echo "[2/2] Fix Applied Successfully!"
echo "-------------------------------------------------"
echo "The shipping service now has 3 replicas running."
echo "You can now re-run the 'Combined Failures' or CPU spike faults from the AI Planner."
echo "Observe the Live Dashboard: you should see improved P95 latency and lower error rates,"
echo "demonstrating system resilience and fulfilling the final stretch goal."
echo "================================================="
