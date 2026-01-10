SHELL := /bin/bash

NETWORK      ?= opencode-net

OLLAMA_IMG   ?= ollama/ollama
OLLAMA_CTR   ?= ollama
OLLAMA_PORT  ?= 11434
OLLAMA_CTX   ?= 32768

OPENCODE_IMG ?= opencode:local
OPENCODE_CTR ?= opencode

# Allows overriding base debian image tag
DEBIAN_TAG   ?= trixie-slim

# Ensure inner UID and GID are mapped correctly to avoid permission issues
UID          := $(shell id -u)
GID          := $(shell id -g)

PROJECT_DIR  := $(CURDIR)

.PHONY: opencode_network build_opencode run_opencode stop_opencode run_ollama logs_ollama stop_ollama gpu_stat clean \
	run_llama_3-1-8b run_gpt-oss-20b run_gpt-oss-120b run_devstral2_small

opencode_network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK) >/dev/null
	@echo "Network ready: $(NETWORK)"

build_opencode:
	docker build \
	  --build-arg DEBIAN_TAG=$(DEBIAN_TAG) \
	  -t $(OPENCODE_IMG) opencode

run_opencode: opencode_network
	@mkdir -p "$(CURDIR)/opencode/config"
	@mkdir -p "$(HOME)/.local/share/opencode"
	@docker rm -f $(OPENCODE_CTR) >/dev/null 2>&1 || true
	docker run -it --rm --name $(OPENCODE_CTR) \
	  --network $(NETWORK) \
	  -e OPENCODE_UID=$(UID) \
	  -e OPENCODE_GID=$(GID) \
	  -v "$(PROJECT_DIR)":/workspace \
		-v "$(CURDIR)/opencode/config":/home/opencode/.config/opencode \
		-v "$(HOME)/.local/share/opencode":/home/opencode/.local/share/opencode \
	  $(OPENCODE_IMG)

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
