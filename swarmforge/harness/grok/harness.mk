# Grok Build's make interface for the targets harness_rules generates.
GROK_IMG    ?= grok-build:local
GROK_CTR    ?= grok-$(PROJECT_NAME)
GROK_DATA_DIR ?= $(HOME)/.local/share/grok
GROK_HOME_DIR ?= $(GROK_DATA_DIR)/home
GROK_ARGS ?=

GROK_RUN_ENV = \
	-e SWARMFORGE_AGENT_BIN=grok \
	$(SWARMFORGE_LAYER_ENV)

# tmpfs masks grok's native asset dirs so per-repo assets never accumulate in
# the persistent home; exec, because skill packages ship scripts.
GROK_RUN_MOUNTS = \
	-v "$(GROK_HOME_DIR)":$(ANVIL_HOME) \
	--tmpfs $(ANVIL_HOME)/.grok/skills:exec \
	--tmpfs $(ANVIL_HOME)/.grok/commands \
	$(SWARMFORGE_LAYER_MOUNTS)

GROK_EXTRA_BUILD_ARGS =
GROK_RUN_ARGS = $(GROK_ARGS)
GROK_WORKDIR_MODE = repo-slug
GROK_USER_CONFIG_DIR = $(HOME)/.grok
GROK_ORG_CONFIG_DIR = $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.grok,)
GROK_REPO_CONFIG_DIR = $(SWARMFORGE_DIR)/grok
GROK_CONFIG_DEST = $(ANVIL_HOME)/.grok
GROK_CONFIG_RESET = 0
# $$-escaped: these expand in the run_grok recipe, not here.
GROK_MKDIRS = \
	$$(GROK_HOME_DIR) \
	$$(SWARMFORGE_USER_CONFIG_DIR) \
	$$(GROK_HOME_DIR)/.swarmforge \
	$$(GROK_HOME_DIR)/.swarmforge/skills \
	$$(GROK_HOME_DIR)/.swarmforge/command \
	$$(GROK_HOME_DIR)/.grok/skills \
	$$(GROK_HOME_DIR)/.grok/commands

$(eval $(call harness_rules,grok,GROK))
