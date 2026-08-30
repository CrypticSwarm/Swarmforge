# Grok Build's make interface: the user knobs, run env/mounts, and layer
# defaults for the targets harness_rules generates (build_grok,
# update_grok, run_grok, stop_grok).
GROK_IMG    ?= grok-build:local
GROK_CTR    ?= grok-$(PROJECT_NAME)
GROK_DATA_DIR ?= $(HOME)/.local/share/grok
GROK_HOME_DIR ?= $(GROK_DATA_DIR)/home
GROK_ARGS ?=

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

GROK_EXTRA_BUILD_ARGS =
GROK_RUN_ARGS = $(GROK_ARGS)
GROK_WORKDIR_MODE = repo-slug
GROK_USER_CONFIG_DIR = $(HOME)/.grok
GROK_ORG_CONFIG_DIR = $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.grok,)
GROK_REPO_CONFIG_DIR = $(SWARMFORGE_DIR)/grok
GROK_CONFIG_DEST = $(ANVIL_HOME)/.grok
GROK_CONFIG_RESET = 0
# $$-escaped so each path expands in the run_grok recipe, where the
# target-scoped SWARMFORGE_USER_CONFIG_DIR default is in effect.
GROK_MKDIRS = \
	$$(GROK_HOME_DIR) \
	$$(SWARMFORGE_USER_CONFIG_DIR) \
	$$(GROK_HOME_DIR)/.swarmforge \
	$$(GROK_HOME_DIR)/.swarmforge/skills \
	$$(GROK_HOME_DIR)/.swarmforge/command \
	$$(GROK_HOME_DIR)/.grok/skills \
	$$(GROK_HOME_DIR)/.grok/commands

$(eval $(call harness_rules,grok,GROK))
