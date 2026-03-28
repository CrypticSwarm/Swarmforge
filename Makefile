SHELL := /bin/bash

NETWORK      ?= opencode-net

OLLAMA_IMG   ?= ollama/ollama
OLLAMA_CTR   ?= ollama
OLLAMA_PORT  ?= 11434
OLLAMA_CTX   ?= 32768

OPENCODE_IMG ?= opencode:local
OPENCODE_CTR ?= opencode-$(PROJECT_NAME)
CLAUDE_IMG  ?= claude-code:local
CLAUDE_CTR  ?= claude-$(PROJECT_NAME)

PROFILE      ?=
DATA_DIR     ?= $(HOME)/.local/share/opencode
OPENCODE_ARGS ?=
CLAUDE_DATA_DIR ?= $(HOME)/.local/share/claude
CLAUDE_ARGS ?=
GITCONFIG_FILE ?= $(HOME)/.gitconfig
ENV_FILE ?= $(PROJECT_DIR)/.swarmforge/env

# Set this to a changing value to refresh the `curl https://opencode.ai/install` layer.
OPENCODE_INSTALL_BUST ?= 0
# Set this to a changing value to refresh the `curl https://claude.ai/install.sh` layer.
CLAUDE_INSTALL_BUST ?= 0

MODEL        ?=
EVAL_MODEL   ?= $(MODEL)
TEST_SKILL   ?=
TEST_DATA_DIR ?= $(DATA_DIR)
TEST_ENABLE_JUDGE ?=
TEST_TIMEOUT_S ?= 600
# Allows overriding base debian image tag
DEBIAN_TAG   ?= trixie-slim

# Ensure inner UID and GID are mapped correctly to avoid permission issues
UID          := $(shell id -u)
GID          := $(shell id -g)

SWARMFORGE_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
PROJECT_DIR  := $(CURDIR)
PROJECT_NAME := $(notdir $(abspath $(PROJECT_DIR)))
OPENCODE_CONFIG_DIR ?= $(SWARMFORGE_DIR)/opencode/config
SHARED_SKILLS_DIR ?= $(OPENCODE_CONFIG_DIR)/skills
SHARED_COMMAND_DIR ?= $(OPENCODE_CONFIG_DIR)/command

PROFILE_FLAG :=
ifneq ($(strip $(PROFILE)),)
PROFILE_FLAG := --profile $(PROFILE)
endif

OPENCODE_RUN_MOUNTS := \
	-v "$(OPENCODE_CONFIG_DIR)":/home/opencode/.config/opencode \
	-v "$(DATA_DIR)":/home/opencode/.local/share/opencode

CLAUDE_RUN_ENV := \
	-e SWARMFORGE_AGENT_BIN=claude \
	-e SWARMFORGE_SKILLS_DIR=/home/opencode/.swarmforge/skills \
	-e SWARMFORGE_COMMAND_DIR=/home/opencode/.swarmforge/command

CLAUDE_RUN_MOUNTS := \
	-v "$(CLAUDE_DATA_DIR)/home":/home/opencode \
	-v "$(SHARED_SKILLS_DIR)":/home/opencode/.swarmforge/skills:ro \
	-v "$(SHARED_COMMAND_DIR)":/home/opencode/.swarmforge/command:ro

.PHONY: opencode_network build_opencode update_opencode build_claude update_claude run_opencode stop_opencode run_claude stop_claude run_ollama logs_ollama stop_ollama gpu_stat clean \
	run_llama_3-1-8b run_gpt-oss-20b run_gpt-oss-120b run_devstral2_small test

define run_agent_container
	@docker rm -f "$(1)" >/dev/null 2>&1 || true
	@set -euo pipefail; \
	workspace_dir="$$(git -C "$(PROJECT_DIR)" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$(PROJECT_DIR)")"; \
	git_common_dir="$$(git -C "$$workspace_dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"; \
	if [ -n "$$git_common_dir" ] && [ "$$git_common_dir" != "$$workspace_dir/.git" ]; then \
		printf '%s\n' "Detected git worktree; mounting common git dir: $$git_common_dir"; \
		git_common_mount=(-v "$$git_common_dir":"$$git_common_dir"); \
	else \
		git_common_mount=(); \
	fi; \
	if [ -f "$(GITCONFIG_FILE)" ]; then \
		gitconfig_mount=(-v "$(GITCONFIG_FILE)":/home/opencode/.gitconfig:ro); \
	else \
		gitconfig_mount=(); \
	fi; \
	if [ -f "$(ENV_FILE)" ]; then \
		env_file_flag=(--env-file "$(ENV_FILE)"); \
	else \
		env_file_flag=(); \
	fi; \
	set -x; \
	docker run -it --rm --name "$(1)" \
	  --network "$(NETWORK)" \
	  -e OPENCODE_UID="$(UID)" \
	  -e OPENCODE_GID="$(GID)" \
	  $(2) \
	  -v "$$workspace_dir":/workspace \
	  $(3) \
	  $${git_common_mount[@]+"$${git_common_mount[@]}"} \
	  $${gitconfig_mount[@]+"$${gitconfig_mount[@]}"} \
	  $${env_file_flag[@]+"$${env_file_flag[@]}"} \
	  $(4) $(5); \
	set +x
endef

opencode_network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK) >/dev/null
	@echo "Network ready: $(NETWORK)"

build_opencode:
	docker build \
	  --target opencode-runtime \
	  --build-arg AGENT=opencode \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg OPENCODE_INSTALL_BUST=$(OPENCODE_INSTALL_BUST) \
	  -t $(OPENCODE_IMG) "$(SWARMFORGE_DIR)/opencode"

# Rebuild only from the OpenCode install step onward.
update_opencode:
	$(MAKE) build_opencode OPENCODE_INSTALL_BUST=$(shell date +%s)

build_claude:
	docker build \
	  --target claude-runtime \
	  --build-arg AGENT=claude \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg CLAUDE_INSTALL_BUST=$(CLAUDE_INSTALL_BUST) \
	  -t $(CLAUDE_IMG) "$(SWARMFORGE_DIR)/opencode"

# Rebuild only from the Claude install step onward.
update_claude:
	$(MAKE) build_claude CLAUDE_INSTALL_BUST=$(shell date +%s)

run_opencode: opencode_network
	@mkdir -p "$(OPENCODE_CONFIG_DIR)"
	@mkdir -p "$(DATA_DIR)"
	$(call run_agent_container,$(OPENCODE_CTR),,$(OPENCODE_RUN_MOUNTS),$(OPENCODE_IMG),$(PROFILE_FLAG) $(OPENCODE_ARGS))

stop_opencode:
	@docker rm -f $(OPENCODE_CTR) >/dev/null 2>&1 || true

run_claude: opencode_network
	@mkdir -p "$(CLAUDE_DATA_DIR)/home"
	@mkdir -p "$(CLAUDE_DATA_DIR)/home/.swarmforge"
	$(call run_agent_container,$(CLAUDE_CTR),$(CLAUDE_RUN_ENV),$(CLAUDE_RUN_MOUNTS),$(CLAUDE_IMG),$(CLAUDE_ARGS))

stop_claude:
	@docker rm -f $(CLAUDE_CTR) >/dev/null 2>&1 || true

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

clean: stop_opencode stop_claude stop_ollama
	@docker network rm $(NETWORK) >/dev/null 2>&1 || true

run_llama_3-1-8b:
	docker exec -it ollama ollama run llama3.1:8b

run_gpt-oss-20b:
	docker exec -it ollama ollama run gpt-oss:20b

run_gpt-oss-120b:
	docker exec -it ollama ollama run gpt-oss:120b

run_devstral2_small:
	docker exec -it ollama ollama run devstral-small-2:24b

test: opencode_network
	@if [ -z "$(strip $(MODEL))" ]; then \
		printf '%s\n' "MODEL is required (example: make test MODEL=ollama/llama3.1)"; \
		exit 2; \
	fi
	@mkdir -p "$(TEST_DATA_DIR)"
	docker run --rm \
	  --network $(NETWORK) \
	  -e HOME=/home/opencode \
	  -v "$(PROJECT_DIR)":/workspace \
	  -v "$(OPENCODE_CONFIG_DIR)":/home/opencode/.config/opencode \
	  -v "$(TEST_DATA_DIR)":/home/opencode/.local/share/opencode \
	  --entrypoint python \
	  $(OPENCODE_IMG) /workspace/scripts/test_skills.py \
	    --model "$(MODEL)" \
	    --eval-model "$(EVAL_MODEL)" \
	    --timeout-s "$(TEST_TIMEOUT_S)" \
	    --color always \
	    --report-cost \
	    $(if $(TEST_ENABLE_JUDGE),--enable-judge,) \
	    $(if $(TEST_SKILL),--skill "$(TEST_SKILL)",)
