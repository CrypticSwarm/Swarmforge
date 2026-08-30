# OpenCode's make interface: the user knobs, run env/mounts, and layer
# defaults for the targets harness_rules generates (build_opencode,
# update_opencode, run_opencode, stop_opencode). Its data dir and profile
# knobs are the user-facing names DATA_DIR and PROFILE, unprefixed.
OPENCODE_IMG ?= opencode:local
OPENCODE_CTR ?= opencode-$(PROJECT_NAME)
PROFILE      ?=
DATA_DIR     ?= $(HOME)/.local/share/opencode
OPENCODE_ARGS ?=
# Optional OpenCode version pin (example: 1.4.14)
OPENCODE_VERSION ?=
OPENCODE_CONFIG_DIR ?= $(SWARMFORGE_DIR)/opencode

PROFILE_FLAG :=
ifneq ($(strip $(PROFILE)),)
PROFILE_FLAG := --profile $(PROFILE)
endif

OPENCODE_RUN_MOUNTS = \
	$(SWARMFORGE_LAYER_MOUNTS) \
	-v "$(DATA_DIR)":$(ANVIL_HOME)/.local/share/opencode

OPENCODE_RUN_ENV = \
	$(SWARMFORGE_LAYER_ENV)

OPENCODE_EXTRA_BUILD_ARGS = --build-arg SWARMFORGE_HARNESS_VERSION=$(OPENCODE_VERSION)
OPENCODE_RUN_ARGS = $(PROFILE_FLAG) $(OPENCODE_ARGS)
OPENCODE_WORKDIR_MODE =
OPENCODE_USER_CONFIG_DIR = $(HOME)/.config/opencode
OPENCODE_ORG_CONFIG_DIR = $(if $(strip $(SWARMFORGE_ORG_CONFIG_ROOT)),$(SWARMFORGE_ORG_CONFIG_ROOT)/.opencode,)
OPENCODE_REPO_CONFIG_DIR = $(OPENCODE_CONFIG_DIR)
OPENCODE_CONFIG_DEST = $(ANVIL_HOME)/.config/opencode
OPENCODE_CONFIG_RESET = 1
# $$-escaped so each path expands in the run_opencode recipe, where the
# target-scoped config-layer defaults are in effect.
OPENCODE_MKDIRS = \
	$$(SWARMFORGE_USER_CONFIG_DIR) \
	$$(SWARMFORGE_REPO_CONFIG_DIR) \
	$$(DATA_DIR)

$(eval $(call harness_rules,opencode,OPENCODE))
