.PHONY: test baseline controller watchdog up down map inject report

test:
	python -m unittest discover -s tests -p "test_*.py" -v

baseline:
	python controller/baseline.py

controller:
	python -m uvicorn controller.main:app --host 0.0.0.0 --port 8000

watchdog:
	python controller/watchdog.py --daemon

demo:
	bash demo_day1.sh
