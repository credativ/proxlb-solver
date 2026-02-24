VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
FLAKE8 := $(VENV)/bin/flake8

.PHONY: install test lint report clean

install:
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

test:
	$(PYTEST) -v --junitxml=results.xml

lint:
	$(FLAKE8) proxlb_solver/ tests/

report:
	$(PYTHON) -m proxlb_solver.cli --markdown results.md --html results.html --junit results.xml

clean:
	rm -rf $(VENV) *.egg-info results.xml results.md results.html __pycache__
