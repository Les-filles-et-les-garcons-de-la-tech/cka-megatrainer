PYTHON ?= python3
APP    := cka_trainer.py

.PHONY: help first-start start-exam start-learn start-learn-here start-exam-silent start-exam-here install-retro uninstall list validate test check-links iaac-up iaac-tools iaac-down iaac-status iaac-test iaac-test-destructive iaac-coverage vm-venv vm-up vm-down vm-status vm-test vm-test-destructive screenshot readme-media dist

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

iaac-tools: ## Fetch the pinned tools the questions use (helm) into iaac/.bin (checksum-verified)
	@iaac/tools.sh

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

# --- the libvirt lab (iaac/vm): Ubuntu 24.04 VMs, kubeadm, like the exam (HA=1: 3 control planes + lb) ---
VM_ANSIBLE = cd iaac/vm && .venv/bin/ansible-playbook
HA ?= 0
ADDONS ?=
VM_HA = $(if $(filter 1 true yes,$(HA)),true,false)

vm-venv: ## Install the pinned Ansible into iaac/vm/.venv (no system change)
	@test -x iaac/vm/.venv/bin/ansible-playbook || { $(PYTHON) -m venv iaac/vm/.venv && \
	  iaac/vm/.venv/bin/pip install -q -r iaac/vm/requirements.txt; }

vm-up: vm-venv ## Build the VM lab (libvirt): cp1 + node01, kubeadm v1.37, Calico, add-ons (HA=1: cp1..cp3 + lb + node01; ADDONS=full: controllers)
	@$(VM_ANSIBLE) site.yml -e ha=$(VM_HA) -e addons=$(ADDONS)

vm-down: vm-venv ## Delete the VM lab (VMs, disks, network); keeps the image cache and SSH key
	@$(VM_ANSIBLE) destroy.yml

vm-status: ## Show the VM lab: domains and Kubernetes nodes
	@virsh -c qemu:///system list --all | grep -E ' cka-|Name|Nom' || true
	@KUBECONFIG=iaac/.kubeconfig-vm iaac/.bin/kubectl get nodes -o wide 2>/dev/null || echo "no VM cluster (make vm-up)"

vm-test: ## Run the cluster tests against the VM lab (ARGS=test_cluster for one file)
	@IAAC_TARGET=vm iaac/run-tests.sh $(ARGS)

vm-test-destructive: ## The same, with the control-plane tests
	@IAAC_TARGET=vm DESTRUCTIVE=1 iaac/run-tests.sh $(ARGS)

check-links: ## Check that every documentation link of questions.json still works (needs network)
	@$(PYTHON) tools/check_links.py

screenshot: ## Text screenshot via tmux (SHOT="100 30 'Down Enter' 3")
	@$(TMUX_RUN) sh -c "tools/screenshot.sh $(SHOT)"

readme-media: ## Regenerate the README screenshots (docs/media/*.svg) via tmux
	@$(TMUX_RUN) $(PYTHON) tools/readme_media.py

dist: ## Build dist/cka-megatrainer.pyz: the whole game in one runnable file
	@$(PYTHON) tools/build_pyz.py
