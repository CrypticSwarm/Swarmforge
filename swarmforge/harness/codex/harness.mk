# Codex CLI's make interface: the user knobs, run env/mounts, and layer
# defaults for the targets harness_rules generates (build_codex,
# update_codex, run_codex, stop_codex).
CODEX_IMG   ?= codex-cli:local
CODEX_CTR   ?= codex-$(PROJECT_NAME)
CODEX_DATA_DIR ?= $(HOME)/.local/share/codex
CODEX_HOME_DIR ?= $(CODEX_DATA_DIR)/home
CODEX_ARGS ?=

CODEX_RUN_ENV = \
	-e SWARMFORGE_AGENT_BIN=codex \
	$(SWARMFORGE_LAYER_ENV)

# Codex reads its skills from ~/.agents/skills natively. Masking that dir
# with tmpfs keeps it container-private, so per-repo assets never
# accumulate in the persistent home. exec: skill packages ship scripts.
CODEX_RUN_MOUNTS = \
	-v "$(CODEX_HOME_DIR)":$(ANVIL_HOME) \
	--tmpfs $(ANVIL_HOME)/.agents/skills:exec \
	$(SWARMFORGE_LAYER_MOUNTS)

CODEX_RUN_ARGS = $(CODEX_ARGS)
CODEX_WORKDIR_MODE = repo-slug
CODEX_USER_CONFIG_DIR = $(HOME)/.codex
CODEX_ORG_CONFIG_DIR = $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.codex,)
CODEX_REPO_CONFIG_DIR = $(SWARMFORGE_DIR)/codex
CODEX_CONFIG_DEST =
CODEX_CONFIG_RESET = 0
# $$-escaped so each path expands in the run_codex recipe, where the
# target-scoped SWARMFORGE_USER_CONFIG_DIR default is in effect.
CODEX_MKDIRS = \
	$$(CODEX_HOME_DIR) \
	$$(SWARMFORGE_USER_CONFIG_DIR) \
	$$(CODEX_HOME_DIR)/.swarmforge \
	$$(CODEX_HOME_DIR)/.swarmforge/skills \
	$$(CODEX_HOME_DIR)/.swarmforge/command \
	$$(CODEX_HOME_DIR)/.agents/skills \
	$$(CODEX_HOME_DIR)/.codex

$(eval $(call harness_rules,codex,CODEX))
