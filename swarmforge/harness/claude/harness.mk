# Claude Code's make interface for the targets harness_rules generates.
CLAUDE_IMG  ?= claude-code:local
CLAUDE_CTR  ?= claude-$(PROJECT_NAME)
CLAUDE_DATA_DIR ?= $(HOME)/.local/share/claude
CLAUDE_HOME_DIR ?= $(CLAUDE_DATA_DIR)/home
CLAUDE_ARGS ?=

CLAUDE_RUN_ENV = \
	-e SWARMFORGE_AGENT_BIN=claude \
	$(SWARMFORGE_LAYER_ENV)

# Nothing under .claude is loaded as config: the pinned dir is container-local.
# plugins/ remounts read-only so a session cannot rewrite the next one's code.
CLAUDE_RUN_MOUNTS = \
	-v "$(CLAUDE_HOME_DIR)":$(ANVIL_HOME) \
	-v "$(CLAUDE_HOME_DIR)/.claude/plugins":$(ANVIL_HOME)/.claude/plugins:ro \
	$(SWARMFORGE_LAYER_MOUNTS)

CLAUDE_EXTRA_BUILD_ARGS =
CLAUDE_RUN_ARGS = $(CLAUDE_ARGS)
CLAUDE_WORKDIR_MODE = repo-slug
CLAUDE_USER_CONFIG_DIR = $(HOME)/.claude
CLAUDE_ORG_CONFIG_DIR = $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude,)
CLAUDE_REPO_CONFIG_DIR = $(SWARMFORGE_DIR)/claude
CLAUDE_CONFIG_DEST =
CLAUDE_CONFIG_RESET = 0
# $$-escaped: these expand in the run_claude recipe, not here.
CLAUDE_MKDIRS = \
	$$(CLAUDE_HOME_DIR) \
	$$(SWARMFORGE_USER_CONFIG_DIR) \
	$$(CLAUDE_HOME_DIR)/.swarmforge \
	$$(CLAUDE_HOME_DIR)/.swarmforge/skills \
	$$(CLAUDE_HOME_DIR)/.swarmforge/command \
	$$(CLAUDE_HOME_DIR)/.claude/plugins

$(eval $(call harness_rules,claude,CLAUDE))
