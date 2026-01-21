SHELL := /bin/bash

NETWORK      ?= opencode-net

OLLAMA_IMG   ?= ollama/ollama
OLLAMA_CTR   ?= ollama
OLLAMA_PORT  ?= 11434
OLLAMA_CTX   ?= 32768

OPENCODE_IMG ?= opencode:local
OPENCODE_CTR ?= opencode-$(PROJECT_NAME)

PROFILE      ?=
DATA_DIR     ?= $(HOME)/.local/share/opencode
OPENCODE_ARGS ?=
GITCONFIG_FILE ?= $(HOME)/.gitconfig

# Set this to a changing value to refresh the `curl https://opencode.ai/install` layer.
OPENCODE_INSTALL_BUST ?= 0

MODEL        ?=
EVAL_MODEL   ?= $(MODEL)
TEST_SKILL   ?=
TEST_DATA_DIR ?= $(DATA_DIR)
TEST_ENABLE_JUDGE ?=
TEST_TIMEOUT_S ?= 600
GITCONFIG_FLAG := $(strip $(if $(wildcard $(GITCONFIG_FILE)),-v "$(GITCONFIG_FILE)":/home/opencode/.gitconfig:ro,))

# Allows overriding base debian image tag
DEBIAN_TAG   ?= trixie-slim

# Ensure inner UID and GID are mapped correctly to avoid permission issues
UID          := $(shell id -u)
GID          := $(shell id -g)

PROJECT_DIR  := $(CURDIR)
PROJECT_NAME := $(notdir $(abspath $(PROJECT_DIR)))

PROFILE_FLAG :=
ifneq ($(strip $(PROFILE)),)
PROFILE_FLAG := --profile $(PROFILE)
endif

.PHONY: opencode_network build_opencode update_opencode run_opencode stop_opencode run_ollama logs_ollama stop_ollama gpu_stat clean \
	run_llama_3-1-8b run_gpt-oss-20b run_gpt-oss-120b run_devstral2_small test

opencode_network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK) >/dev/null
	@echo "Network ready: $(NETWORK)"

build_opencode:
	docker build \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  --build-arg OPENCODE_INSTALL_BUST=$(OPENCODE_INSTALL_BUST) \
	  -t $(OPENCODE_IMG) opencode

# Rebuild only from the OpenCode install step onward.
update_opencode:
	$(MAKE) build_opencode OPENCODE_INSTALL_BUST=$(shell date +%s)

run_opencode: opencode_network
	@mkdir -p "$(CURDIR)/opencode/config"
	@mkdir -p "$(DATA_DIR)"
	@docker rm -f $(OPENCODE_CTR) >/dev/null 2>&1 || true
	docker run -it --rm --name $(OPENCODE_CTR) \
	  --network $(NETWORK) \
	  -e OPENCODE_UID=$(UID) \
	  -e OPENCODE_GID=$(GID) \
	  -v "$(PROJECT_DIR)":/workspace \
		-v "$(CURDIR)/opencode/config":/home/opencode/.config/opencode \
		-v "$(DATA_DIR)":/home/opencode/.local/share/opencode$(if $(GITCONFIG_FLAG), $(GITCONFIG_FLAG)) \
	  $(OPENCODE_IMG) $(PROFILE_FLAG) $(OPENCODE_ARGS)

stop_opencode:
	@docker rm -f $(OPENCODE_CTR) >/dev/null 2>&1 || true

run_ollama: opencode_network
	@docker rm -f $(OLLAMA_CTR) >/dev/null 2>&1 || true
	docker run -d --rm --name $(OLLAMA_CTR) \
	  --network $(NETWORK) \
	  -v $(CURDIR)/ollama:/root/.ollama \
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

clean: stop_opencode stop_ollama
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
	  -v "$(CURDIR)/opencode/config":/home/opencode/.config/opencode \
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
