.DEFAULT_GOAL := help
DEMO_PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
.PHONY: help lint format demo demo-attach demo-stop demo-record demo-record-open doctor test typecheck
help: ## Show supported commands; dependency installation is separate.
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
lint: ## Daily lint-only gate; no install, tests, build or network.
	$(RUFF) check .
	$(RUFF) format --check .
format: ## Format project-owned Python explicitly.
	$(RUFF) format .
demo: ## Open a manual server and two players in this demo's own tmux session.
	$(DEMO_PYTHON) -m pokerdemo launch --workspace "$(CURDIR)"
demo-attach: ## Return to this workspace's running demo.
	$(DEMO_PYTHON) -m pokerdemo attach --workspace "$(CURDIR)"
demo-stop: ## Stop only this workspace's demo processes.
	$(DEMO_PYTHON) -m pokerdemo stop --workspace "$(CURDIR)"
demo-record: ## Capture the real three-process scenario, GIF and frames locally.
	$(DEMO_PYTHON) -m pokerdemo record --workspace "$(CURDIR)"
demo-record-open: ## Record locally and open the completed GIF.
	$(DEMO_PYTHON) -m pokerdemo record --workspace "$(CURDIR)" --open
doctor: ## Check already installed prerequisites; no game or installation.
	$(DEMO_PYTHON) -m pokerdemo doctor --workspace "$(CURDIR)"
test: ## Manual only: installed-package CLI and demo protocol tests.
	$(DEMO_PYTHON) -m pytest tests
typecheck: ## Manual only: strict owned-package typing.
	$(DEMO_PYTHON) -m mypy
