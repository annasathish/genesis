"""Insights layer: run comparison, business impact, PII masking, dashboard + chat.

Read-only over experiment data (runs/*.json written by scripts/day1_demo.py). Nothing here
injects faults or talks to the sandbox, so it is safe to run next to a live experiment.
"""
import os
import sys

# Reuse the M5 scorer (controller/scorer.py) whether we are started from the repo root or from controller/.
_CONTROLLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, _CONTROLLER_DIR)
