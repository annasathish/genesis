.PHONY: report test-report

# ── M5: Reporting ────────────────────────────────────────────────────
report:
	@echo "🧪 Generating resilience report..."
	python cli.py report $(ARGS)

report-llm:
	@echo "🧪 Generating resilience report (with LLM analysis)..."
	python cli.py report --llm $(ARGS)

test-report:
	@echo "🧪 Running M5 integration tests..."
	python -m pytest test_reporting.py -v
