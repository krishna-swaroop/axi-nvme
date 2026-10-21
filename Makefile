PYTHON := $(CURDIR)/.venv/bin/python
.PHONY: check model-test upstream-test qualify budget timing-help
check:
	$(PYTHON) scripts/check.py
model-test:
	$(PYTHON) -m pytest -q tests/test_storage_oracle.py --junitxml=reports/contracts.xml
upstream-test:
	$(PYTHON) scripts/upstream.py
qualify:
	$(PYTHON) scripts/qualify.py $(QUALIFY_ARGS)
budget:
	$(PYTHON) scripts/budget.py
timing-help:
	@cat constraints/README.md
