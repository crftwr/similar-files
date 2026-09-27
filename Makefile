# Development tasks for similar-files. Run from the repo root.
#
#   make dev       set up .venv with the package, extras and build tools
#   make test      run the full test suite
#   make dist      build the sdist and wheel into dist/
#
# `make help` lists every target.

# Use the project venv when it exists, whatever shell `make` was started from.
VENV   := .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)

.DEFAULT_GOAL := help

.PHONY: help
help:  ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "} {printf "  %-12s %s\n", $$1, $$2}'

# --- Setup ---------------------------------------------------------------

$(VENV)/bin/python:
	python3 -m venv $(VENV)

.PHONY: venv
venv: $(VENV)/bin/python  ## Create .venv if it is missing

.PHONY: dev
dev: venv  ## Install the package editable, with all extras and build tools
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/python -m pip install -e ".[image,test]" build twine

# --- Tests ---------------------------------------------------------------

.PHONY: test
test:  ## Run the full test suite
	$(PYTHON) -m pytest test/ -v

.PHONY: test-core
test-core:  ## Run the tests on a bare install (no optional extras), in a temp venv
	rm -rf temp/venv-core
	python3 -m venv temp/venv-core
	temp/venv-core/bin/python -m pip install --quiet ".[test]"
	cd temp && venv-core/bin/python -m pytest ../test/ -v

# --- Distribution --------------------------------------------------------

.PHONY: dist
dist: clean-dist  ## Build the sdist and wheel into dist/
	$(PYTHON) -m build

.PHONY: check
check: dist  ## Build, then validate the package metadata
	$(PYTHON) -m twine check dist/*

.PHONY: upload-test
upload-test: check  ## Upload dist/ to TestPyPI
	$(PYTHON) -m twine upload --repository testpypi dist/*

.PHONY: upload
upload: check  ## Upload dist/ to PyPI
	$(PYTHON) -m twine upload dist/*

# --- Cleanup -------------------------------------------------------------

.PHONY: clean-dist
clean-dist:
	rm -rf build/ dist/ *.egg-info

.PHONY: clean
clean: clean-dist  ## Remove build output and caches (keeps .venv)
	rm -rf .pytest_cache temp/venv-core
	find . -path ./$(VENV) -prune -o -type d -name __pycache__ -exec rm -rf {} +
