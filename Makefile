PYTHON ?= python3
APP    := cka_trainer.py

.PHONY: help first-start start-exam start-exam-silent start-exam-here install-retro uninstall list validate test screenshot readme-media dist

# tmux lives in the 'cka-dev' toolbox on Fedora Silverblue
TMUX_RUN := $(if $(shell command -v tmux),,toolbox run -c cka-dev)

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-18s %s\n", $$1, $$2}'

first-start: install-retro ## Set up fonts and terminal, then play in the current terminal
	@$(MAKE) start-exam-here

start-exam: install-retro ## Set up missing dependencies and launch a retro window (ARGS="--domain storage")
	@tools/play.sh $(ARGS)

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

test: validate ## Validate the database and run the unit tests
	@$(PYTHON) -m unittest -v

screenshot: ## Text screenshot via tmux (SHOT="100 30 'Down Enter' 3")
	@$(TMUX_RUN) sh -c "tools/screenshot.sh $(SHOT)"

readme-media: ## Regenerate the README screenshots (docs/media/*.svg) via tmux
	@$(TMUX_RUN) $(PYTHON) tools/readme_media.py

dist: ## Build dist/cka-megatrainer.pyz: the whole game in one runnable file
	@$(PYTHON) tools/build_pyz.py
