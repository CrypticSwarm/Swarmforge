SHELL := /bin/bash

NETWORK      ?= opencode-net

OLLAMA_IMG   ?= ollama/ollama
OLLAMA_CTR   ?= ollama
OLLAMA_PORT  ?= 11434
OLLAMA_CTX   ?= 32768

BROKER_IMG  ?= swarmforge-docker-broker:latest

SWARMFORGE_REPO_SLUG ?=
SWARMFORGE_REMOTE_NAME ?= origin
GITCONFIG_FILE ?= $(HOME)/.gitconfig
ENV_FILE ?= $(PROJECT_DIR)/.swarmforge/env

# Change the value to bust the install layer of the image being built.
SWARMFORGE_HARNESS_INSTALL_BUST ?= 0

MODEL        ?=
EVAL_MODEL   ?= $(MODEL)
TEST_SKILL   ?=
# DATA_DIR is opencode's data-dir knob, declared in its harness.mk fragment.
TEST_DATA_DIR ?= $(DATA_DIR)
TEST_ENABLE_JUDGE ?=
TEST_TIMEOUT_S ?= 600
DEBIAN_TAG   ?= trixie-slim
TIMEZONE     ?= Etc/UTC

# Passed into the container so what it writes stays owned by the invoking user.
UID          := $(shell id -u)
GID          := $(shell id -g)

# The host's python: the launcher and the unit suite run outside any image.
PYTHON ?= python3

SWARMFORGE_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
PROJECT_DIR  := $(CURDIR)
# Single-quoted: a `$` or a backtick in the path is shell syntax on a $(shell)
# command line, and not every make propagates an export there instead.
PROJECT_DIR_SQ := $(subst ','\'',$(PROJECT_DIR))
PROJECT_NAME := $(shell $(PYTHON) "$(SWARMFORGE_DIR)/bin/project-name" '$(PROJECT_DIR_SQ)')
ifeq ($(strip $(PROJECT_NAME)),)
$(error Could not name a container for $(PROJECT_DIR); see the error above from $(SWARMFORGE_DIR)/bin/project-name)
endif
# Not for overriding -- the entrypoint and docs name it; a variable only so the git-dir guard tracks it.
WORKSPACE_MOUNT := /workspace
# The entrypoint hardcodes this same path, so no overrides.
ANVIL_HOME := /home/anvil
SHARED_SKILLS_DIR ?= $(SWARMFORGE_DIR)/skills
SHARED_COMMAND_DIR ?= $(SWARMFORGE_DIR)/commands
SWARMFORGE_ORG_CONFIG_ROOT ?=
# Harness-neutral asset layers; unified agents live under <layer>/agents.
SWARMFORGE_USER_ASSETS_DIR ?= $(HOME)/.swarmforge
SWARMFORGE_ORG_ASSETS_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge,)
# The repo layers name their one subdirectory so the rest of the checkout is never exposed.
SWARMFORGE_REPO_AGENTS_DIR ?= $(SWARMFORGE_DIR)/agents
SWARMFORGE_REPO_TONGS_DIR ?= $(SWARMFORGE_DIR)/tongs

# The portable .agents/{skills,commands} overlay, distinct from the .swarmforge asset layers above.
SWARMFORGE_USER_DOTAGENTS_DIR ?= $(HOME)/.agents
SWARMFORGE_ORG_DOTAGENTS_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.agents,)

# The one tool outside the stdlib this repo asks for, and only for `make lint`.
RUFF ?= ruff

SWARMFORGE_LAYER_MOUNTS = \
	-v "$(SWARMFORGE_USER_CONFIG_DIR)":/tmp/swarmforge-config/user:ro \
	$(if $(and $(strip $(SWARMFORGE_ORG_CONFIG_DIR)),$(wildcard $(SWARMFORGE_ORG_CONFIG_DIR))),-v "$(SWARMFORGE_ORG_CONFIG_DIR)":/tmp/swarmforge-config/org:ro,) \
	$(if $(and $(strip $(SWARMFORGE_REPO_CONFIG_DIR)),$(wildcard $(SWARMFORGE_REPO_CONFIG_DIR))),-v "$(SWARMFORGE_REPO_CONFIG_DIR)":/tmp/swarmforge-config/repo:ro,) \
	$(if $(and $(strip $(SWARMFORGE_USER_ASSETS_DIR)),$(wildcard $(SWARMFORGE_USER_ASSETS_DIR))),-v "$(SWARMFORGE_USER_ASSETS_DIR)":/tmp/swarmforge-assets/user:ro,) \
	$(if $(and $(strip $(SWARMFORGE_ORG_ASSETS_DIR)),$(wildcard $(SWARMFORGE_ORG_ASSETS_DIR))),-v "$(SWARMFORGE_ORG_ASSETS_DIR)":/tmp/swarmforge-assets/org:ro,) \
	$(if $(and $(strip $(SWARMFORGE_REPO_AGENTS_DIR)),$(wildcard $(SWARMFORGE_REPO_AGENTS_DIR))),-v "$(SWARMFORGE_REPO_AGENTS_DIR)":/tmp/swarmforge-assets/repo/agents:ro,) \
	$(if $(and $(strip $(SWARMFORGE_USER_DOTAGENTS_DIR)),$(wildcard $(SWARMFORGE_USER_DOTAGENTS_DIR))),-v "$(SWARMFORGE_USER_DOTAGENTS_DIR)":/tmp/swarmforge-dotagents/user:ro,) \
	$(if $(and $(strip $(SWARMFORGE_ORG_DOTAGENTS_DIR)),$(wildcard $(SWARMFORGE_ORG_DOTAGENTS_DIR))),-v "$(SWARMFORGE_ORG_DOTAGENTS_DIR)":/tmp/swarmforge-dotagents/org:ro,) \
	-v "$(SHARED_SKILLS_DIR)":$(ANVIL_HOME)/.swarmforge/skills:ro \
	-v "$(SHARED_COMMAND_DIR)":$(ANVIL_HOME)/.swarmforge/command:ro

SWARMFORGE_LAYER_ENV = \
	-e SWARMFORGE_CONFIG_USER_DIR=/tmp/swarmforge-config/user \
	-e SWARMFORGE_CONFIG_ORG_DIR=/tmp/swarmforge-config/org \
	-e SWARMFORGE_CONFIG_REPO_DIR=/tmp/swarmforge-config/repo \
	-e SWARMFORGE_ASSETS_USER_DIR=/tmp/swarmforge-assets/user \
	-e SWARMFORGE_ASSETS_ORG_DIR=/tmp/swarmforge-assets/org \
	-e SWARMFORGE_ASSETS_REPO_DIR=/tmp/swarmforge-assets/repo \
	-e SWARMFORGE_DOTAGENTS_USER_DIR=/tmp/swarmforge-dotagents/user \
	-e SWARMFORGE_DOTAGENTS_ORG_DIR=/tmp/swarmforge-dotagents/org \
	-e SWARMFORGE_CONFIG_DEST=$(SWARMFORGE_CONFIG_DEST) \
	-e SWARMFORGE_CONFIG_RESET=$(SWARMFORGE_CONFIG_RESET) \
	-e SWARMFORGE_SKILLS_DIR=$(ANVIL_HOME)/.swarmforge/skills \
	-e SWARMFORGE_COMMAND_DIR=$(ANVIL_HOME)/.swarmforge/command

# Read on the host, never mounted into the anvil; the workspace layer is appended at run time.
TONGS_LAYER_ARGS = \
	$(if $(and $(strip $(SWARMFORGE_USER_ASSETS_DIR)),$(wildcard $(SWARMFORGE_USER_ASSETS_DIR)/tongs)),--user-tongs "$(SWARMFORGE_USER_ASSETS_DIR)/tongs",) \
	$(if $(and $(strip $(SWARMFORGE_ORG_ASSETS_DIR)),$(wildcard $(SWARMFORGE_ORG_ASSETS_DIR)/tongs)),--org-tongs "$(SWARMFORGE_ORG_ASSETS_DIR)/tongs",) \
	$(if $(and $(strip $(SWARMFORGE_REPO_TONGS_DIR)),$(wildcard $(SWARMFORGE_REPO_TONGS_DIR))),--repo-tongs "$(SWARMFORGE_REPO_TONGS_DIR)",)

.PHONY: opencode_network build_harnesses build_broker run_ollama logs_ollama stop_ollama gpu_stat clean \
	run_llama_3-1-8b run_gpt-oss-20b run_gpt-oss-120b run_devstral2_small test test-skills lint

# bin/git-guard prints the git-dir mounts per --target, one docker -v per line; its docstring has the why.
define run_agent_container
	@docker rm -f "$(1)" >/dev/null 2>&1 || true
	@set -euo pipefail; \
	workspace_dir="$$(git -C "$(PROJECT_DIR)" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$(PROJECT_DIR)")"; \
	if [ -f "$(GITCONFIG_FILE)" ]; then \
		gitconfig_mount=(-v "$(GITCONFIG_FILE)":$(ANVIL_HOME)/.gitconfig:ro); \
	else \
		gitconfig_mount=(); \
	fi; \
	if [ -f "$(ENV_FILE)" ]; then \
		env_file_flag=(--env-file "$(ENV_FILE)"); \
	else \
		env_file_flag=(); \
	fi; \
	if [ "$(6)" = "repo-slug" ]; then \
		repo_slug="$(SWARMFORGE_REPO_SLUG)"; \
		if [ -z "$$repo_slug" ]; then \
			remote_url="$$(git -C "$$workspace_dir" remote get-url "$(SWARMFORGE_REMOTE_NAME)" 2>/dev/null || true)"; \
			if [ -n "$$remote_url" ]; then \
				remote_slug="$$remote_url"; \
				remote_slug="$${remote_slug%.git}"; \
				case "$$remote_slug" in \
					*://*) remote_slug="$${remote_slug#*://}" ;; \
				esac; \
				remote_slug="$${remote_slug#*@}"; \
				remote_slug="$${remote_slug/:/\/}"; \
				remote_slug="$${remote_slug#/}"; \
				case "$$remote_slug" in \
					github.com/*/*) repo_slug="$${remote_slug#github.com/}" ;; \
					*/*) repo_slug="$${remote_slug#*/}" ;; \
				esac; \
			fi; \
		fi; \
		if [ -z "$$repo_slug" ]; then \
			repo_slug="$$(basename "$$workspace_dir")"; \
		fi; \
		repo_slug="$$(printf '%s' "$$repo_slug" | tr '\\\\' '/' | tr -cs '[:alnum:]._/-' '-')"; \
		while [ "$${repo_slug#/}" != "$$repo_slug" ]; do repo_slug="$${repo_slug#/}"; done; \
		while [ "$${repo_slug%/}" != "$$repo_slug" ]; do repo_slug="$${repo_slug%/}"; done; \
		if [ -z "$$repo_slug" ]; then \
			repo_slug="$$(basename "$$workspace_dir")"; \
		fi; \
		repo_mount_path="/repos/$$repo_slug"; \
		workspace_path_mount=(-v "$$workspace_dir":"$$repo_mount_path"); \
		workdir_flag=(-w "$$repo_mount_path"); \
	elif [ -z "$(6)" ]; then \
		workspace_path_mount=(); \
		workdir_flag=(); \
	else \
		printf '%s\n' "Unsupported workdir mode: $(6)" >&2; \
		exit 2; \
	fi; \
	git_guard_flags=(--workspace "$$workspace_dir" --target "$(WORKSPACE_MOUNT)"); \
	if [ -n "$${repo_mount_path:-}" ]; then git_guard_flags+=(--target "$$repo_mount_path"); fi; \
	git_dir_mounts=(); \
	git_guard_specs="$$($(PYTHON) "$(SWARMFORGE_DIR)/bin/git-guard" "$${git_guard_flags[@]}")"; \
	if [ -n "$$git_guard_specs" ]; then \
		while IFS= read -r git_guard_spec; do \
			git_dir_mounts+=(-v "$$git_guard_spec"); \
		done <<< "$$git_guard_specs"; \
	fi; \
	set -x; \
	$(PYTHON) "$(SWARMFORGE_DIR)/bin/run-anvil" \
	  $(TONGS_LAYER_ARGS) \
	  --workspace-tongs "$$workspace_dir/.swarmforge/tongs" \
	  --workspace "$$workspace_dir" \
	  --approvals "$(SWARMFORGE_USER_ASSETS_DIR)/approvals.json" \
	  --providers "$(SWARMFORGE_USER_ASSETS_DIR)/secret-providers.yaml" \
	  --harness "$(7)" \
	  --anvil-image "$(4)" \
	  -- \
	  docker run -it --rm --name "$(1)" \
	  --network "$(NETWORK)" \
	  -e SWARMFORGE_UID="$(UID)" \
	  -e SWARMFORGE_GID="$(GID)" \
	  -e TZ="$(TIMEZONE)" \
	  -e TERM -e COLORTERM \
	  $(2) \
	  -v "$$workspace_dir":"$(WORKSPACE_MOUNT)" \
	  $${workspace_path_mount[@]+"$${workspace_path_mount[@]}"} \
	  $(3) \
	  $${git_dir_mounts[@]+"$${git_dir_mounts[@]}"} \
	  $${gitconfig_mount[@]+"$${gitconfig_mount[@]}"} \
	  $${env_file_flag[@]+"$${env_file_flag[@]}"} \
	  $${workdir_flag[@]+"$${workdir_flag[@]}"} \
	  $(4) $(5); \
	set +x
endef

# Generates one harness's build/update/run/stop/name targets. $(1) is the harness
# name (target names, --build-arg AGENT); $(2) is its harness.mk variable
# prefix (CLAUDE, OPENCODE, ...). Fragment knobs are referenced by name and
# expand when a recipe runs, so overrides behave as on a rule written out in
# full. $(2)_MKDIRS is the one knob spliced verbatim at eval time, so its
# entries must carry $$-escaped references.
define harness_rules
.PHONY: build_$(1) update_$(1) run_$(1) stop_$(1) name_$(1)

build_$(1):
	docker build --target harness-runtime --build-arg AGENT=$(1)$(if $($(2)_EXTRA_BUILD_ARGS), $$($(2)_EXTRA_BUILD_ARGS)) --build-arg DEBIAN_TAG=$$(DEBIAN_TAG) --build-arg SWARMFORGE_HARNESS_INSTALL_BUST=$$(SWARMFORGE_HARNESS_INSTALL_BUST) -f "$$(SWARMFORGE_DIR)/anvil/Dockerfile" -t $$($(2)_IMG) "$$(SWARMFORGE_DIR)"

# Rebuild only from the harness install step onward.
update_$(1):
	$$(MAKE) build_$(1) SWARMFORGE_HARNESS_INSTALL_BUST=$$(shell date +%s)

run_$(1): SWARMFORGE_USER_CONFIG_DIR ?= $$($(2)_USER_CONFIG_DIR)
run_$(1): SWARMFORGE_ORG_CONFIG_DIR ?= $$($(2)_ORG_CONFIG_DIR)
run_$(1): SWARMFORGE_REPO_CONFIG_DIR ?= $$($(2)_REPO_CONFIG_DIR)
run_$(1): SWARMFORGE_CONFIG_DEST ?= $$($(2)_CONFIG_DEST)
run_$(1): SWARMFORGE_CONFIG_RESET ?= $$($(2)_CONFIG_RESET)
run_$(1): opencode_network
	$(if $($(2)_MKDIRS),@mkdir -p $(foreach dir,$($(2)_MKDIRS),"$(dir)"))
	$$(call run_agent_container,$$($(2)_CTR),$$($(2)_RUN_ENV),$$($(2)_RUN_MOUNTS),$$($(2)_IMG),$$($(2)_RUN_ARGS),$$($(2)_WORKDIR_MODE),$(1))

stop_$(1):
	@docker rm -f $$($(2)_CTR) >/dev/null 2>&1 || true

# The name carries a path digest; this prints the one run_$(1) uses, and nothing else, so it composes.
name_$(1):
	@echo "$$($(2)_CTR)"

# No image depends on another, so `make -j build_harnesses` builds them in parallel.
build_harnesses: build_$(1)

clean: stop_$(1)
endef

opencode_network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK) >/dev/null
	@echo "Network ready: $(NETWORK)"

# Included after the macros the fragments use, and after opencode_network so it stays the default goal.
HARNESS_FRAGMENTS := $(wildcard $(SWARMFORGE_DIR)/swarmforge/harness/*/harness.mk)
# An empty glob would silently drop every harness target.
ifeq ($(strip $(HARNESS_FRAGMENTS)),)
$(error No harness fragments found under $(SWARMFORGE_DIR)/swarmforge/harness)
endif
include $(HARNESS_FRAGMENTS)

# The reference broker image, unused until a broker tong definition is enabled in a layer.
build_broker:
	docker build -t $(BROKER_IMG) "$(SWARMFORGE_DIR)/tongs/docker-broker"

run_ollama: opencode_network
	@docker rm -f $(OLLAMA_CTR) >/dev/null 2>&1 || true
	docker run -d --rm --name $(OLLAMA_CTR) \
	  --network $(NETWORK) \
	  -v $(SWARMFORGE_DIR)/ollama:/root/.ollama \
	  -e OLLAMA_HOST=0.0.0.0:11434 \
		-e OLLAMA_CONTEXT_LENGTH=$(OLLAMA_CTX) \
	  -p $(OLLAMA_PORT):11434 \
	  --gpus=all \
	  $(OLLAMA_IMG)
	@echo "Ollama: host http://localhost:$(OLLAMA_PORT) | containers http://$(OLLAMA_CTR):11434"

logs_ollama:
	docker logs -f $(OLLAMA_CTR)

stop_ollama:
	@docker rm -f $(OLLAMA_CTR) >/dev/null 2>&1 || true

gpu_stat:
	nvidia-smi

clean: stop_ollama
	@docker network rm $(NETWORK) >/dev/null 2>&1 || true

run_llama_3-1-8b:
	docker exec -it ollama ollama run llama3.1:8b

run_gpt-oss-20b:
	docker exec -it ollama ollama run gpt-oss:20b

run_gpt-oss-120b:
	docker exec -it ollama ollama run gpt-oss:120b

run_devstral2_small:
	docker exec -it ollama ollama run devstral-small-2:24b

run_qwen_3-5-27b:
	docker exec -it ollama ollama run qwen3.5:27b

run_qwen_3-5-35b:
	docker exec -it ollama ollama run qwen3.5:35b

run_gemma4_26b:
	docker exec -it ollama ollama run gemma4:26b

# PYTHONPATH makes swarmforge importable regardless of where make was invoked from.
test:
	PYTHONPATH="$(SWARMFORGE_DIR)" $(PYTHON) -m unittest discover -s "$(SWARMFORGE_DIR)/tests" -p 'test_*.py'

# `check` never edits a file, so this is safe over a dirty tree.
lint:
	$(RUFF) check "$(SWARMFORGE_DIR)"

# Drives a real model in the opencode image; OPENCODE_IMG comes from its harness.mk fragment.
test-skills: opencode_network
	@if [ -z "$(strip $(MODEL))" ]; then \
		printf '%s\n' "MODEL is required (example: make test-skills MODEL=ollama/llama3.1)"; \
		exit 2; \
	fi
	@mkdir -p "$(TEST_DATA_DIR)"
	docker run --rm \
	  --network $(NETWORK) \
	  -e HOME=$(ANVIL_HOME) \
	  -v "$(PROJECT_DIR)":/workspace \
	  -v "$(TEST_DATA_DIR)":$(ANVIL_HOME)/.local/share/opencode \
	  --entrypoint python \
	  $(OPENCODE_IMG) /workspace/scripts/skill_eval.py \
	    --model "$(MODEL)" \
	    --eval-model "$(EVAL_MODEL)" \
	    --timeout-s "$(TEST_TIMEOUT_S)" \
	    --color always \
	    --report-cost \
	    $(if $(TEST_ENABLE_JUDGE),--enable-judge,) \
	    $(if $(TEST_SKILL),--skill "$(TEST_SKILL)",)
