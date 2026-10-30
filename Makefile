PYTHON ?= python3
APP    := cka_trainer.py

.PHONY: help start-exam start-exam-silent start-exam-here install-retro list validate test screenshot readme-media

# tmux lives in the 'cka-dev' toolbox on Fedora Silverblue
TMUX_RUN := $(if $(shell command -v tmux),,toolbox run -c cka-dev)

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-18s %s\n", $$1, $$2}'

start-exam: ## Launch in a retro-font window (ARGS="--domain storage", RETRO_FONT="Press Start 2P")
	@tools/play.sh $(ARGS)

start-exam-silent: ## Same, without music and sound effects
	@tools/play.sh --silent $(ARGS)

start-exam-here: ## Launch in the current terminal (no new window)
	@$(PYTHON) $(APP) $(ARGS)

install-retro: ## One-time setup: 80s fonts + terminal for the game window
	@tools/install-retro.sh

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
