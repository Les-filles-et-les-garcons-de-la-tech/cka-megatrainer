PYTHON ?= python3
APP    := cka_trainer.py

.PHONY: help first-start start-exam start-learn start-learn-here start-exam-silent start-exam-here install-retro uninstall list validate test check-links iaac-up iaac-down iaac-status iaac-test iaac-test-destructive iaac-coverage screenshot readme-media dist

# tmux lives in the 'cka-dev' toolbox on Fedora Silverblue
TMUX_RUN := $(if $(shell command -v tmux),,toolbox run -c cka-dev)

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-18s %s\n", $$1, $$2}'

first-start: install-retro ## Set up fonts and terminal, then play in the current terminal
	@$(MAKE) start-exam-here

start-exam: install-retro ## Set up missing dependencies and launch a retro window (ARGS="--domain storage")
	@tools/play.sh $(ARGS)

start-learn: install-retro ## Learn mode: right answers marked, links to the Kubernetes docs
	@tools/play.sh --learn $(ARGS)

start-learn-here: ## Learn mode in the current terminal (no new window)
	@$(PYTHON) $(APP) --learn $(ARGS)

start-exam-silent: install-retro ## Same, without music and sound effects
	@tools/play.sh --silent $(ARGS)

start-exam-here: ## Launch in the current terminal (no new window)
	@$(PYTHON) $(APP) $(ARGS)

install-retro:
	@tools/install-retro.sh

uninstall: ## Remove setup-installed components, game caches and local builds
	@sh tools/uninstall.sh

list: ## List all questions
	@$(PYTHON) $(APP) --list

validate: ## Check questions.json
	@$(PYTHON) $(APP) --validate

test: validate ## Validate the database and run the unit tests (plus the iaac coverage check)
	@$(PYTHON) -m unittest -v
	@$(PYTHON) -m unittest discover -s iaac/tests -p 'test_coverage.py'

iaac-up: ## Create the v1.37 test cluster (kind) used to check the answers (see iaac/README.md)
	@iaac/cluster.sh up

iaac-down: ## Delete the test cluster
	@iaac/cluster.sh down

iaac-status: ## Show the test cluster's nodes and version
	@iaac/cluster.sh status

iaac-test: ## Run every question's answers against the test cluster (ARGS=test_storage for one file)
	@iaac/run-tests.sh $(ARGS)

iaac-test-destructive: ## Also run the tests that break and repair control-plane components
	@DESTRUCTIVE=1 iaac/run-tests.sh $(ARGS)

iaac-coverage: ## Which tests exercise which question (no cluster needed)
	@cd iaac/tests && $(PYTHON) test_coverage.py --table

check-links: ## Check that every documentation link of questions.json still works (needs network)
	@$(PYTHON) tools/check_links.py

screenshot: ## Text screenshot via tmux (SHOT="100 30 'Down Enter' 3")
	@$(TMUX_RUN) sh -c "tools/screenshot.sh $(SHOT)"

readme-media: ## Regenerate the README screenshots (docs/media/*.svg) via tmux
	@$(TMUX_RUN) $(PYTHON) tools/readme_media.py

dist: ## Build dist/cka-megatrainer.pyz: the whole game in one runnable file
	@$(PYTHON) tools/build_pyz.py
