"""Multi-agent findings pipeline: Planner, Analyzer, Recommender, run by the Coordinator (agents/coordinator.py)."""
import insights  # noqa: F401  (puts controller/ on sys.path so scorer.py and friends import from anywhere)
