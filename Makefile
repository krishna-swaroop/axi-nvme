PYTHON := $(CURDIR)/.venv/bin/python
.PHONY: check upstream-test budget timing-help
check:
	$(PYTHON) scripts/check.py
upstream-test:
	$(PYTHON) scripts/upstream.py
budget:
	$(PYTHON) scripts/budget.py
timing-help:
	@cat constraints/README.md
