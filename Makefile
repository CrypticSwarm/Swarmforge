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

BROKER_IMG  ?= swarmforge-docker-broker:latest

PROFILE      ?=
DATA_DIR     ?= $(HOME)/.local/share/opencode
OPENCODE_ARGS ?=
CLAUDE_DATA_DIR ?= $(HOME)/.local/share/claude
CLAUDE_HOME_DIR ?= $(CLAUDE_DATA_DIR)/home
CLAUDE_ARGS ?=
CLAUDE_REPO_SLUG ?=
CLAUDE_REMOTE_NAME ?= origin
GITCONFIG_FILE ?= $(HOME)/.gitconfig
ENV_FILE ?= $(PROJECT_DIR)/.swarmforge/env

# Set this to a changing value to refresh the `curl https://opencode.ai/install` layer.
OPENCODE_INSTALL_BUST ?= 0
# Optional OpenCode version pin (example: 1.4.14)
OPENCODE_VERSION ?=
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
# Default timezone passed at runtime (override with TIMEZONE=Region/City)
TIMEZONE     ?= Etc/UTC

# Ensure inner UID and GID are mapped correctly to avoid permission issues
UID          := $(shell id -u)
GID          := $(shell id -g)

SWARMFORGE_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
PROJECT_DIR  := $(CURDIR)
PROJECT_NAME := $(notdir $(abspath $(PROJECT_DIR)))
OPENCODE_CONFIG_DIR ?= $(SWARMFORGE_DIR)/opencode
SHARED_SKILLS_DIR ?= $(SWARMFORGE_DIR)/skills
SHARED_COMMAND_DIR ?= $(SWARMFORGE_DIR)/commands
SWARMFORGE_ORG_CONFIG_ROOT ?=
# Harness-neutral Swarmforge asset layers. User and org layers are .swarmforge
# roots (unified agents live in <dir>/agents); the repo layer points directly
# at this repo's top-level agents/ so the rest of the repo is never mounted.
SWARMFORGE_USER_ASSETS_DIR ?= $(HOME)/.swarmforge
SWARMFORGE_ORG_ASSETS_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge,)
SWARMFORGE_REPO_AGENTS_DIR ?= $(SWARMFORGE_DIR)/agents
# Repo-layer tong definitions, pointed at directly (like SWARMFORGE_REPO_AGENTS_DIR)
# so the rest of the checkout is never read. The tongs/ dir ships only the
# reference broker's source under a subdirectory, not a top-level *.yaml, so
# discovery (which reads top-level *.yaml only) finds nothing here until a
# definition is added; the wildcard guard below still skips the layer entirely if
# the dir is ever absent.
SWARMFORGE_REPO_TONGS_DIR ?= $(SWARMFORGE_DIR)/tongs

# Portable skills/commands overlay layers. These follow the harness-neutral
# .agents/{skills,commands} convention (a sibling of .swarmforge under the same
# user $HOME / org SWARMFORGE_ORG_CONFIG_ROOT roots). Named DOTAGENTS to keep
# them distinct from the unified-agent asset pipeline above (whose agents live
# in .swarmforge/agents and use SWARMFORGE_ASSETS_*). The repo layer keeps its
# own special shared skills/ and commands/ (SHARED_SKILLS_DIR/SHARED_COMMAND_DIR).
SWARMFORGE_USER_DOTAGENTS_DIR ?= $(HOME)/.agents
SWARMFORGE_ORG_DOTAGENTS_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.agents,)

# Host python used to run the anvil launcher (run_anvil.py).
PYTHON ?= python3

PROFILE_FLAG :=
ifneq ($(strip $(PROFILE)),)
PROFILE_FLAG := --profile $(PROFILE)
endif

SWARMFORGE_LAYER_MOUNTS = \
	-v "$(SWARMFORGE_USER_CONFIG_DIR)":/tmp/swarmforge-config/user:ro \
	$(if $(and $(strip $(SWARMFORGE_ORG_CONFIG_DIR)),$(wildcard $(SWARMFORGE_ORG_CONFIG_DIR))),-v "$(SWARMFORGE_ORG_CONFIG_DIR)":/tmp/swarmforge-config/org:ro,) \
	$(if $(and $(strip $(SWARMFORGE_REPO_CONFIG_DIR)),$(wildcard $(SWARMFORGE_REPO_CONFIG_DIR))),-v "$(SWARMFORGE_REPO_CONFIG_DIR)":/tmp/swarmforge-config/repo:ro,) \
	$(if $(and $(strip $(SWARMFORGE_USER_ASSETS_DIR)),$(wildcard $(SWARMFORGE_USER_ASSETS_DIR))),-v "$(SWARMFORGE_USER_ASSETS_DIR)":/tmp/swarmforge-assets/user:ro,) \
	$(if $(and $(strip $(SWARMFORGE_ORG_ASSETS_DIR)),$(wildcard $(SWARMFORGE_ORG_ASSETS_DIR))),-v "$(SWARMFORGE_ORG_ASSETS_DIR)":/tmp/swarmforge-assets/org:ro,) \
	$(if $(and $(strip $(SWARMFORGE_REPO_AGENTS_DIR)),$(wildcard $(SWARMFORGE_REPO_AGENTS_DIR))),-v "$(SWARMFORGE_REPO_AGENTS_DIR)":/tmp/swarmforge-assets/repo/agents:ro,) \
	$(if $(and $(strip $(SWARMFORGE_USER_DOTAGENTS_DIR)),$(wildcard $(SWARMFORGE_USER_DOTAGENTS_DIR))),-v "$(SWARMFORGE_USER_DOTAGENTS_DIR)":/tmp/swarmforge-dotagents/user:ro,) \
	$(if $(and $(strip $(SWARMFORGE_ORG_DOTAGENTS_DIR)),$(wildcard $(SWARMFORGE_ORG_DOTAGENTS_DIR))),-v "$(SWARMFORGE_ORG_DOTAGENTS_DIR)":/tmp/swarmforge-dotagents/org:ro,) \
	-v "$(SHARED_SKILLS_DIR)":/home/opencode/.swarmforge/skills:ro \
	-v "$(SHARED_COMMAND_DIR)":/home/opencode/.swarmforge/command:ro

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
	-e SWARMFORGE_SKILLS_DIR=/home/opencode/.swarmforge/skills \
	-e SWARMFORGE_COMMAND_DIR=/home/opencode/.swarmforge/command

# Host directories for the tong definition layers, passed to the launcher only
# when present (same wildcard guard as the asset mounts above). The launcher
# reads these on the host; they are not mounted into the anvil. The workspace
# layer depends on the resolved workspace dir and is appended at run time.
TONGS_LAYER_ARGS = \
	$(if $(and $(strip $(SWARMFORGE_USER_ASSETS_DIR)),$(wildcard $(SWARMFORGE_USER_ASSETS_DIR)/tongs)),--user-tongs "$(SWARMFORGE_USER_ASSETS_DIR)/tongs",) \
	$(if $(and $(strip $(SWARMFORGE_ORG_ASSETS_DIR)),$(wildcard $(SWARMFORGE_ORG_ASSETS_DIR)/tongs)),--org-tongs "$(SWARMFORGE_ORG_ASSETS_DIR)/tongs",) \
	$(if $(and $(strip $(SWARMFORGE_REPO_TONGS_DIR)),$(wildcard $(SWARMFORGE_REPO_TONGS_DIR))),--repo-tongs "$(SWARMFORGE_REPO_TONGS_DIR)",)

OPENCODE_RUN_MOUNTS = \
	$(SWARMFORGE_LAYER_MOUNTS) \
	-v "$(DATA_DIR)":/home/opencode/.local/share/opencode

OPENCODE_RUN_ENV = \
	$(SWARMFORGE_LAYER_ENV)

CLAUDE_RUN_ENV = \
	-e SWARMFORGE_AGENT_BIN=claude \
	$(SWARMFORGE_LAYER_ENV)

# skills/, commands/, and agents/ under ~/.claude are container-private tmpfs
# masks over the shared persistent home. The entrypoint repopulates them from
# this repo's sources on every run, so per-repo assets never accumulate in
# CLAUDE_HOME_DIR or leak into other repos' sessions. skills mounts with exec
# because skill packages may ship executable scripts.
CLAUDE_RUN_MOUNTS = \
	-v "$(CLAUDE_HOME_DIR)":/home/opencode \
	--tmpfs /home/opencode/.claude/skills:exec \
	--tmpfs /home/opencode/.claude/commands \
	--tmpfs /home/opencode/.claude/agents \
	$(SWARMFORGE_LAYER_MOUNTS)

.PHONY: opencode_network build_opencode update_opencode build_broker build_claude update_claude run_opencode stop_opencode run_claude stop_claude run_ollama logs_ollama stop_ollama gpu_stat clean \
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
	if [ "$(6)" = "repo-slug" ]; then \
		repo_slug="$(CLAUDE_REPO_SLUG)"; \
		if [ -z "$$repo_slug" ]; then \
			remote_url="$$(git -C "$$workspace_dir" remote get-url "$(CLAUDE_REMOTE_NAME)" 2>/dev/null || true)"; \
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
	set -x; \
	$(PYTHON) "$(SWARMFORGE_DIR)/scripts/run_anvil.py" \
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
	  -e OPENCODE_UID="$(UID)" \
	  -e OPENCODE_GID="$(GID)" \
	  -e TZ="$(TIMEZONE)" \
	  $(2) \
	  -v "$$workspace_dir":/workspace \
	  $${workspace_path_mount[@]+"$${workspace_path_mount[@]}"} \
	  $(3) \
	  $${git_common_mount[@]+"$${git_common_mount[@]}"} \
	  $${gitconfig_mount[@]+"$${gitconfig_mount[@]}"} \
	  $${env_file_flag[@]+"$${env_file_flag[@]}"} \
	  $${workdir_flag[@]+"$${workdir_flag[@]}"} \
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
	  --build-arg OPENCODE_VERSION=$(OPENCODE_VERSION) \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg OPENCODE_INSTALL_BUST=$(OPENCODE_INSTALL_BUST) \
	  -t $(OPENCODE_IMG) "$(SWARMFORGE_DIR)/anvil"

# Rebuild only from the OpenCode install step onward.
update_opencode:
	$(MAKE) build_opencode OPENCODE_INSTALL_BUST=$(shell date +%s)

# Build the reference docker-task broker image. It is not used until a broker tong
# definition is enabled in a layer (see tongs/docker-broker/docker-broker.tong.yaml).
build_broker:
	docker build -t $(BROKER_IMG) "$(SWARMFORGE_DIR)/tongs/docker-broker"

build_claude:
	docker build \
	  --target claude-runtime \
	  --build-arg AGENT=claude \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg CLAUDE_INSTALL_BUST=$(CLAUDE_INSTALL_BUST) \
	  -t $(CLAUDE_IMG) "$(SWARMFORGE_DIR)/anvil"

# Rebuild only from the Claude install step onward.
update_claude:
	$(MAKE) build_claude CLAUDE_INSTALL_BUST=$(shell date +%s)

run_opencode: SWARMFORGE_USER_CONFIG_DIR ?= $(HOME)/.config/opencode
run_opencode: SWARMFORGE_ORG_CONFIG_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode,)
run_opencode: SWARMFORGE_REPO_CONFIG_DIR ?= $(OPENCODE_CONFIG_DIR)
run_opencode: SWARMFORGE_CONFIG_DEST ?= /home/opencode/.config/opencode
run_opencode: SWARMFORGE_CONFIG_RESET ?= 1
run_opencode: opencode_network
	@mkdir -p "$(SWARMFORGE_USER_CONFIG_DIR)"
	@mkdir -p "$(SWARMFORGE_REPO_CONFIG_DIR)"
	@mkdir -p "$(DATA_DIR)"
	$(call run_agent_container,$(OPENCODE_CTR),$(OPENCODE_RUN_ENV),$(OPENCODE_RUN_MOUNTS),$(OPENCODE_IMG),$(PROFILE_FLAG) $(OPENCODE_ARGS),,opencode)

stop_opencode:
	@docker rm -f $(OPENCODE_CTR) >/dev/null 2>&1 || true

run_claude: SWARMFORGE_USER_CONFIG_DIR ?= $(HOME)/.claude
run_claude: SWARMFORGE_ORG_CONFIG_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude,)
run_claude: SWARMFORGE_REPO_CONFIG_DIR ?= $(SWARMFORGE_DIR)/claude
run_claude: SWARMFORGE_CONFIG_DEST ?= /home/opencode/.claude
run_claude: SWARMFORGE_CONFIG_RESET ?= 0
run_claude: opencode_network
	@mkdir -p "$(CLAUDE_HOME_DIR)"
	@mkdir -p "$(SWARMFORGE_USER_CONFIG_DIR)"
	@mkdir -p "$(CLAUDE_HOME_DIR)/.swarmforge"
	@mkdir -p "$(CLAUDE_HOME_DIR)/.swarmforge/skills"
	@mkdir -p "$(CLAUDE_HOME_DIR)/.swarmforge/command"
	@mkdir -p "$(CLAUDE_HOME_DIR)/.claude/skills"
	@mkdir -p "$(CLAUDE_HOME_DIR)/.claude/commands"
	@mkdir -p "$(CLAUDE_HOME_DIR)/.claude/agents"
	$(call run_agent_container,$(CLAUDE_CTR),$(CLAUDE_RUN_ENV),$(CLAUDE_RUN_MOUNTS),$(CLAUDE_IMG),$(CLAUDE_ARGS),repo-slug,claude)

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

run_qwen_3-5-27b:
	docker exec -it ollama ollama run qwen3.5:27b

run_qwen_3-5-35b:
	docker exec -it ollama ollama run qwen3.5:35b

run_gemma4_26b:
	docker exec -it ollama ollama run gemma4:26b

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
