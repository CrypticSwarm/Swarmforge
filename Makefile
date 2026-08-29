SHELL := /bin/bash

NETWORK      ?= opencode-net

OLLAMA_IMG   ?= ollama/ollama
OLLAMA_CTR   ?= ollama
OLLAMA_PORT  ?= 11434
OLLAMA_CTX   ?= 32768

OPENCODE_IMG ?= opencode:local
OPENCODE_CTR ?= opencode-$(PROJECT_NAME)
GROK_IMG    ?= grok-build:local
GROK_CTR    ?= grok-$(PROJECT_NAME)
CODEX_IMG   ?= codex-cli:local
CODEX_CTR   ?= codex-$(PROJECT_NAME)

BROKER_IMG  ?= swarmforge-docker-broker:latest

PROFILE      ?=
DATA_DIR     ?= $(HOME)/.local/share/opencode
OPENCODE_ARGS ?=
GROK_DATA_DIR ?= $(HOME)/.local/share/grok
GROK_HOME_DIR ?= $(GROK_DATA_DIR)/home
GROK_ARGS ?=
CODEX_DATA_DIR ?= $(HOME)/.local/share/codex
CODEX_HOME_DIR ?= $(CODEX_DATA_DIR)/home
CODEX_ARGS ?=
# Stable per-repo mount path knobs, shared by every persistent-home harness.
SWARMFORGE_REPO_SLUG ?=
SWARMFORGE_REMOTE_NAME ?= origin
GITCONFIG_FILE ?= $(HOME)/.gitconfig
ENV_FILE ?= $(PROJECT_DIR)/.swarmforge/env

# Set this to a changing value to refresh the agent install layer. A build
# target names the one agent it installs, so this busts only that image.
SWARMFORGE_HARNESS_INSTALL_BUST ?= 0
# Optional OpenCode version pin (example: 1.4.14)
OPENCODE_VERSION ?=

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
# Container path the working repo is mounted at. The entrypoint, the config
# layer env, and the docs all name it, so it is not meant to be overridden --
# it is a variable so the workspace mount and the git-dir guard that overlays
# paths inside it cannot drift apart.
WORKSPACE_MOUNT := /workspace
# The entrypoint hardcodes this same path, so no overrides.
ANVIL_HOME := /home/anvil
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

# Host python used to run the anvil launcher (bin/run-anvil) and the unit tests.
PYTHON ?= python3

# Ruff is the one tool outside the stdlib this repo asks a contributor to have,
# and only for `make lint` -- nothing under swarmforge/ imports it and no image
# installs it. CI pins a version; locally whatever is on PATH will do.
RUFF ?= ruff

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
	-v "$(DATA_DIR)":$(ANVIL_HOME)/.local/share/opencode

OPENCODE_RUN_ENV = \
	$(SWARMFORGE_LAYER_ENV)

GROK_RUN_ENV = \
	-e SWARMFORGE_AGENT_BIN=grok \
	$(SWARMFORGE_LAYER_ENV)

# Grok reads its skills from ~/.grok/skills natively. Masking that dir and
# ~/.grok/commands with tmpfs keeps them container-private, so per-repo assets
# never accumulate in the persistent home. exec: skill packages ship scripts.
GROK_RUN_MOUNTS = \
	-v "$(GROK_HOME_DIR)":$(ANVIL_HOME) \
	--tmpfs $(ANVIL_HOME)/.grok/skills:exec \
	--tmpfs $(ANVIL_HOME)/.grok/commands \
	$(SWARMFORGE_LAYER_MOUNTS)

CODEX_RUN_ENV = \
	-e SWARMFORGE_AGENT_BIN=codex \
	$(SWARMFORGE_LAYER_ENV)

# Codex's native skills dir is ~/.agents/skills, masked for the reason above.
CODEX_RUN_MOUNTS = \
	-v "$(CODEX_HOME_DIR)":$(ANVIL_HOME) \
	--tmpfs $(ANVIL_HOME)/.agents/skills:exec \
	$(SWARMFORGE_LAYER_MOUNTS)

.PHONY: opencode_network build_opencode update_opencode build_broker build_grok update_grok build_codex update_codex run_opencode stop_opencode run_grok stop_grok run_codex stop_codex run_ollama logs_ollama stop_ollama gpu_stat clean \
	run_llama_3-1-8b run_gpt-oss-20b run_gpt-oss-120b run_devstral2_small test test-skills lint

# The workspace is mounted read-write, but the paths inside its git dir that
# the *host's* git later obeys -- `config`, `hooks/`, and the pointers naming
# where those two live -- are not the agent's to write. bin/git-guard
# works out which git dirs are reachable from the workspace and prints the
# read-only mounts that cover them, one docker `-v` value per line, for each
# container path the workspace is mounted at. Its module docstring has the
# reasoning.
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

# A literal newline, a lone backslash, and a single space, for assembling
# recipe text inside $(eval). Reading a define body collapses literal
# backslash-newlines, so harness_rules splices harness_bs where a recipe
# needs a line continuation to survive into the generated rule.
define harness_nl


endef
harness_blank :=
harness_bs := \$(harness_blank)
harness_space := $(harness_blank) $(harness_blank)

# One recipe line per directory. $(1) holds $$-escaped paths, so each
# expands in the run_<name> recipe where the target-scoped config-layer
# defaults are in effect. foreach joins iterations with a space; strip
# the space that would otherwise trail each generated line.
harness_mkdir_lines = $(subst $(harness_space)$(harness_nl),$(harness_nl),$(foreach dir,$(1),$(harness_nl)	@mkdir -p "$(dir)"))

# Generates one harness's build/update/run/stop targets from the knobs its
# harness.mk fragment declares. $(1) is the harness name as it appears in
# target names, --build-arg AGENT, and the Dockerfile stage; $(2) is the
# fragment's variable prefix (CLAUDE, OPENCODE, ...). Fragment knobs stay
# $$-deferred in the generated recipes, so command-line and environment
# overrides behave exactly as they would on a hand-written rule. .PHONY
# and clean accumulate across evals, one contribution per harness.
define harness_rules
.PHONY: build_$(1) update_$(1) run_$(1) stop_$(1)

build_$(1):
	docker build $(harness_bs)
	  --target $(1)-runtime $(harness_bs)
	  --build-arg AGENT=$(1) $(harness_bs)
$(if $($(2)_EXTRA_BUILD_ARGS),	  $($(2)_EXTRA_BUILD_ARGS) $(harness_bs)$(harness_nl))	  --build-arg DEBIAN_TAG=$$(DEBIAN_TAG) $(harness_bs)
	  --build-arg SWARMFORGE_HARNESS_INSTALL_BUST=$$(SWARMFORGE_HARNESS_INSTALL_BUST) $(harness_bs)
	  -f "$$(SWARMFORGE_DIR)/anvil/Dockerfile" $(harness_bs)
	  -t $$($(2)_IMG) "$$(SWARMFORGE_DIR)"

# Rebuild only from the harness install step onward.
update_$(1):
	$$(MAKE) build_$(1) SWARMFORGE_HARNESS_INSTALL_BUST=$$(shell date +%s)

run_$(1): SWARMFORGE_USER_CONFIG_DIR ?= $$($(2)_USER_CONFIG_DIR)
run_$(1): SWARMFORGE_ORG_CONFIG_DIR ?= $$($(2)_ORG_CONFIG_DIR)
run_$(1): SWARMFORGE_REPO_CONFIG_DIR ?= $$($(2)_REPO_CONFIG_DIR)
run_$(1): SWARMFORGE_CONFIG_DEST ?= $$($(2)_CONFIG_DEST)
run_$(1): SWARMFORGE_CONFIG_RESET ?= $$($(2)_CONFIG_RESET)
run_$(1): opencode_network$(call harness_mkdir_lines,$($(2)_MKDIRS))
	$$(call run_agent_container,$$($(2)_CTR),$$($(2)_RUN_ENV),$$($(2)_RUN_MOUNTS),$$($(2)_IMG),$$($(2)_RUN_ARGS),$$($(2)_WORKDIR_MODE),$(1))

stop_$(1):
	@docker rm -f $$($(2)_CTR) >/dev/null 2>&1 || true

clean: stop_$(1)
endef

opencode_network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK) >/dev/null
	@echo "Network ready: $(NETWORK)"

# Per-harness fragments declare that harness's knobs and eval harness_rules to
# generate its targets. They are read after the shared variables and macros they
# reference, and after opencode_network so it stays the default goal.
include $(wildcard $(SWARMFORGE_DIR)/swarmforge/harness/*/harness.mk)

build_opencode:
	docker build \
	  --target opencode-runtime \
	  --build-arg AGENT=opencode \
	  --build-arg OPENCODE_VERSION=$(OPENCODE_VERSION) \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg SWARMFORGE_HARNESS_INSTALL_BUST=$(SWARMFORGE_HARNESS_INSTALL_BUST) \
	  -f "$(SWARMFORGE_DIR)/anvil/Dockerfile" \
	  -t $(OPENCODE_IMG) "$(SWARMFORGE_DIR)"

# Rebuild only from the OpenCode install step onward.
update_opencode:
	$(MAKE) build_opencode SWARMFORGE_HARNESS_INSTALL_BUST=$(shell date +%s)

# Build the reference docker-task broker image. It is not used until a broker tong
# definition is enabled in a layer (see tongs/docker-broker/docker-broker.tong.yaml).
build_broker:
	docker build -t $(BROKER_IMG) "$(SWARMFORGE_DIR)/tongs/docker-broker"

build_grok:
	docker build \
	  --target grok-runtime \
	  --build-arg AGENT=grok \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg SWARMFORGE_HARNESS_INSTALL_BUST=$(SWARMFORGE_HARNESS_INSTALL_BUST) \
	  -f "$(SWARMFORGE_DIR)/anvil/Dockerfile" \
	  -t $(GROK_IMG) "$(SWARMFORGE_DIR)"

# Rebuild only from the Grok install step onward.
update_grok:
	$(MAKE) build_grok SWARMFORGE_HARNESS_INSTALL_BUST=$(shell date +%s)

build_codex:
	docker build \
	  --target codex-runtime \
	  --build-arg AGENT=codex \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg SWARMFORGE_HARNESS_INSTALL_BUST=$(SWARMFORGE_HARNESS_INSTALL_BUST) \
	  -f "$(SWARMFORGE_DIR)/anvil/Dockerfile" \
	  -t $(CODEX_IMG) "$(SWARMFORGE_DIR)"

# Rebuild only from the Codex install step onward.
update_codex:
	$(MAKE) build_codex SWARMFORGE_HARNESS_INSTALL_BUST=$(shell date +%s)

run_opencode: SWARMFORGE_USER_CONFIG_DIR ?= $(HOME)/.config/opencode
run_opencode: SWARMFORGE_ORG_CONFIG_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode,)
run_opencode: SWARMFORGE_REPO_CONFIG_DIR ?= $(OPENCODE_CONFIG_DIR)
run_opencode: SWARMFORGE_CONFIG_DEST ?= $(ANVIL_HOME)/.config/opencode
run_opencode: SWARMFORGE_CONFIG_RESET ?= 1
run_opencode: opencode_network
	@mkdir -p "$(SWARMFORGE_USER_CONFIG_DIR)"
	@mkdir -p "$(SWARMFORGE_REPO_CONFIG_DIR)"
	@mkdir -p "$(DATA_DIR)"
	$(call run_agent_container,$(OPENCODE_CTR),$(OPENCODE_RUN_ENV),$(OPENCODE_RUN_MOUNTS),$(OPENCODE_IMG),$(PROFILE_FLAG) $(OPENCODE_ARGS),,opencode)

stop_opencode:
	@docker rm -f $(OPENCODE_CTR) >/dev/null 2>&1 || true

run_grok: SWARMFORGE_USER_CONFIG_DIR ?= $(HOME)/.grok
run_grok: SWARMFORGE_ORG_CONFIG_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.grok,)
run_grok: SWARMFORGE_REPO_CONFIG_DIR ?= $(SWARMFORGE_DIR)/grok
run_grok: SWARMFORGE_CONFIG_DEST ?= $(ANVIL_HOME)/.grok
run_grok: SWARMFORGE_CONFIG_RESET ?= 0
run_grok: opencode_network
	@mkdir -p "$(GROK_HOME_DIR)"
	@mkdir -p "$(SWARMFORGE_USER_CONFIG_DIR)"
	@mkdir -p "$(GROK_HOME_DIR)/.swarmforge"
	@mkdir -p "$(GROK_HOME_DIR)/.swarmforge/skills"
	@mkdir -p "$(GROK_HOME_DIR)/.swarmforge/command"
	@mkdir -p "$(GROK_HOME_DIR)/.grok/skills"
	@mkdir -p "$(GROK_HOME_DIR)/.grok/commands"
	$(call run_agent_container,$(GROK_CTR),$(GROK_RUN_ENV),$(GROK_RUN_MOUNTS),$(GROK_IMG),$(GROK_ARGS),repo-slug,grok)

stop_grok:
	@docker rm -f $(GROK_CTR) >/dev/null 2>&1 || true

run_codex: SWARMFORGE_USER_CONFIG_DIR ?= $(HOME)/.codex
run_codex: SWARMFORGE_ORG_CONFIG_DIR ?= $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.codex,)
run_codex: SWARMFORGE_REPO_CONFIG_DIR ?= $(SWARMFORGE_DIR)/codex
run_codex: SWARMFORGE_CONFIG_RESET ?= 0
run_codex: opencode_network
	@mkdir -p "$(CODEX_HOME_DIR)"
	@mkdir -p "$(SWARMFORGE_USER_CONFIG_DIR)"
	@mkdir -p "$(CODEX_HOME_DIR)/.swarmforge"
	@mkdir -p "$(CODEX_HOME_DIR)/.swarmforge/skills"
	@mkdir -p "$(CODEX_HOME_DIR)/.swarmforge/command"
	@mkdir -p "$(CODEX_HOME_DIR)/.agents/skills"
	@mkdir -p "$(CODEX_HOME_DIR)/.codex"
	$(call run_agent_container,$(CODEX_CTR),$(CODEX_RUN_ENV),$(CODEX_RUN_MOUNTS),$(CODEX_IMG),$(CODEX_ARGS),repo-slug,codex)

stop_codex:
	@docker rm -f $(CODEX_CTR) >/dev/null 2>&1 || true

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

clean: stop_opencode stop_claude stop_grok stop_codex stop_ollama
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

# The unit suite. Needs nothing but a host python -- no network, no image,
# no model. PYTHONPATH makes the swarmforge package importable regardless of
# where make was invoked from; the container-side modules under test import it
# the same way the image does.
test:
	PYTHONPATH="$(SWARMFORGE_DIR)" $(PYTHON) -m unittest discover -s "$(SWARMFORGE_DIR)/tests" -p 'test_*.py'

# Lint every python file in the repo. Rules and exemptions live in
# pyproject.toml; `check` never edits a file, so this is safe to run over a
# dirty tree and never reflows code the change did not touch.
lint:
	$(RUFF) check "$(SWARMFORGE_DIR)"

# Skill evaluation: runs scenario prompts from skills/<name>/tests/*.json
# against a real model inside the opencode image and checks what came
# back, so it needs a model and a running network.
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
