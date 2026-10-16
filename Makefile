PY ?= .venv/bin/python

.PHONY: setup test lint greenfield brownfield ambiguous demo dashboard clean

setup:            ## create venv and install
	python3 -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q -e ".[dev]"

test:             ## offline test-suite (scripted agents, real engine/git/pytest)
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check sdlc tests

greenfield:       ## build the URL shortener from scratch (real LLM agents)
	$(PY) -m sdlc.cli run scenarios/greenfield.yaml --approve console

brownfield:       ## enhance the greenfield output (needs a succeeded greenfield run)
	$(PY) -m sdlc.cli run scenarios/brownfield.yaml --approve console

ambiguous:        ## vague requirement -> clarification -> mid-run amendment -> re-plan
	$(PY) -m sdlc.cli run scenarios/ambiguous.yaml --approve console

dashboard:        ## oversight UI on http://127.0.0.1:8765
	$(PY) -m sdlc.cli dashboard

clean:
	rm -rf .sdlc .pytest_cache .ruff_cache
