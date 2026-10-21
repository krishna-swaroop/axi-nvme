PYTHON := $(CURDIR)/.venv/bin/python
.PHONY: check upstream-test qualify budget timing-help
check:
	$(PYTHON) scripts/check.py
upstream-test:
	$(PYTHON) scripts/upstream.py
qualify:
	$(PYTHON) scripts/qualify.py $(QUALIFY_ARGS)
budget:
	$(PYTHON) scripts/budget.py
timing-help:
	@cat constraints/README.md
