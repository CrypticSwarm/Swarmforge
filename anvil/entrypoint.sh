#!/bin/sh
set -eu

# Entrypoint: create a user matching host UID/GID at runtime, then drop privileges.

ANVIL_UID="${SWARMFORGE_UID:-1000}"
ANVIL_GID="${SWARMFORGE_GID:-1000}"
ANVIL_USER="anvil"
ANVIL_GROUP="anvil"
ANVIL_HOME="/home/${ANVIL_USER}"
AGENT_BIN="${SWARMFORGE_AGENT_BIN:-opencode}"
AGENT_BIN_PATH="/usr/local/bin/${AGENT_BIN}"
CLAUDE_SETTINGS_FILE="/run/swarmforge/claude-settings.json"
CLAUDE_CONFIG_HOME="/run/swarmforge/claude-config"
CODEX_CONFIG_HOME="/run/swarmforge/codex-config"
CODEX_CONFIG_FILE="${ANVIL_HOME}/.codex/config.toml"

# State only: nothing claude loads as configuration or code belongs here.
CLAUDE_STATE_DIRS="projects sessions file-history session-env shell-snapshots
plans tasks todos backups cache paste-cache plugins"
CLAUDE_STATE_FILES="history.jsonl stats-cache.json keybindings.json
.credentials.json .last-cleanup .last-update-result.json scheduled_tasks.lock"

configure_timezone() {
  timezone="${TZ:-}"

  [ -n "${timezone}" ] || return 0

  zoneinfo_path="/usr/share/zoneinfo/${timezone}"
  if [ ! -f "${zoneinfo_path}" ]; then
    printf '%s\n' "Warning: TZ '${timezone}' not found under /usr/share/zoneinfo; keeping image default timezone" >&2
    return 0
  fi

  ln -snf "${zoneinfo_path}" /etc/localtime
  printf '%s\n' "${timezone}" >/etc/timezone
}

# Copy each top-level entry from src_dir into dst_dir, replacing whatever is
# at the destination (file, directory, or stale symlink). Top-level entries
# are replaced wholesale; this is intentionally not a deep merge so that
# stale per-entry symlinks left behind by earlier versions of this entrypoint
# get cleaned up on the next run.
copy_dir_entries() {
  src_dir="${1}"
  dst_dir="${2}"

  [ -n "${src_dir}" ] || return 0
  [ -d "${src_dir}" ] || return 0
  [ -n "${dst_dir}" ] || return 0

  mkdir -p "${dst_dir}"

  for entry in "${src_dir}"/*; do
    # Guard against a literal pattern when the directory is empty.
    [ -e "${entry}" ] || [ -L "${entry}" ] || continue

    name="$(basename "${entry}")"
    target="${dst_dir}/${name}"

    rm -rf "${target}"
    cp -a "${entry}" "${target}"
  done
}

translate_codex_commands() {
  src_dir="${1}"
  skills_dst="${2}"

  [ -n "${src_dir}" ] || return 0
  [ -d "${src_dir}" ] || return 0

  PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.commands.translate \
    "${skills_dst}" "${src_dir}" \
    || printf '%s\n' "Warning: command translation failed for Codex; continuing" >&2
}

# Only the allowlisted state outlives the run: claude loads configuration and
# code out of this dir, and a shared one would hand a session's writes to the
# next container. Symlinks rather than bind mounts survive the atomic rename
# some entries are written with; a directory must exist before it is linked,
# or claude's own mkdir fails on the link. Runs after the config merge, which
# wipes this destination under SWARMFORGE_CONFIG_RESET.
link_claude_state() {
  shared="${ANVIL_HOME}/.claude"

  mkdir -p "${CLAUDE_CONFIG_HOME}"

  # The one piece of state claude keeps beside its config dir, not inside it.
  link_claude_entry "${ANVIL_HOME}/.claude.json" ".claude.json"

  for entry in ${CLAUDE_STATE_DIRS}; do
    mkdir -p "${shared}/${entry}" 2>/dev/null || true
    link_claude_entry "${shared}/${entry}" "${entry}"
  done

  for entry in ${CLAUDE_STATE_FILES}; do
    link_claude_entry "${shared}/${entry}" "${entry}"
  done
}

link_claude_entry() {
  rm -rf "${CLAUDE_CONFIG_HOME}/${2}"
  ln -s "${1}" "${CLAUDE_CONFIG_HOME}/${2}"
}

# Populate the harness's native skills and commands locations from the
# shared Swarmforge assets (skills and commands are portable across
# harnesses, so copying is the whole translation).
#
# Sources are applied lowest- to highest-precedence, identically for every
# harness; the config merge excludes skills/commands so this is their only
# transport:
#   1. Portable .agents layers: user, then org. These follow the harness-neutral
#      .agents/{skills,commands} convention (mounted via SWARMFORGE_DOTAGENTS_USER_DIR
#      / SWARMFORGE_DOTAGENTS_ORG_DIR), so the source dir names are the same for
#      every harness.
#   2. Harness shared assets (mounted via SWARMFORGE_SKILLS_DIR /
#      SWARMFORGE_COMMAND_DIR): the Swarmforge repo's own skills/ and commands/.
#   3. Workspace overlay: <workspace>/.agents/{skills,commands}.
#
# Harness-native config dirs (such as <layer>/.claude or <layer>/.opencode) are
# never consumed for skills/commands; those formats are portable and live under
# the .agents convention instead.
#
# Claude's destinations live in the container-local config dir: each run
# starts empty, and per-repo assets never leak between repos.
copy_shared_assets() {
  workspace_dir="${1:-/workspace}"

  case "${AGENT_BIN}" in
    claude)
      skills_dst="${CLAUDE_CONFIG_HOME}/skills"
      commands_dst="${CLAUDE_CONFIG_HOME}/commands"
      ;;
    grok)
      skills_dst="${ANVIL_HOME}/.grok/skills"
      commands_dst="${ANVIL_HOME}/.grok/commands"
      ;;
    codex)
      # Codex uses skills as its extension point; portable commands are
      # translated into skill packages below.
      skills_dst="${ANVIL_HOME}/.agents/skills"
      commands_dst="codex-skills"
      ;;
    opencode)
      config_dest="${SWARMFORGE_CONFIG_DEST:-${ANVIL_HOME}/.config/opencode}"
      skills_dst="${config_dest}/skills"
      commands_dst="${config_dest}/command"
      ;;
    *)
      return 0
      ;;
  esac

  for layer_src in "${SWARMFORGE_DOTAGENTS_USER_DIR:-}" "${SWARMFORGE_DOTAGENTS_ORG_DIR:-}"; do
    [ -n "${layer_src}" ] || continue
    if [ "${commands_dst}" = "codex-skills" ]; then
      translate_codex_commands "${layer_src}/commands" "${skills_dst}"
      copy_dir_entries "${layer_src}/skills" "${skills_dst}"
    else
      copy_dir_entries "${layer_src}/skills" "${skills_dst}"
      copy_dir_entries "${layer_src}/commands" "${commands_dst}"
    fi
  done

  if [ "${commands_dst}" = "codex-skills" ]; then
    translate_codex_commands "${SWARMFORGE_COMMAND_DIR:-}" "${skills_dst}"
    copy_dir_entries "${SWARMFORGE_SKILLS_DIR:-}" "${skills_dst}"
    translate_codex_commands "${workspace_dir}/.agents/commands" "${skills_dst}"
    copy_dir_entries "${workspace_dir}/.agents/skills" "${skills_dst}"
  else
    copy_dir_entries "${SWARMFORGE_SKILLS_DIR:-}" "${skills_dst}"
    copy_dir_entries "${SWARMFORGE_COMMAND_DIR:-}" "${commands_dst}"
    copy_dir_entries "${workspace_dir}/.agents/skills" "${skills_dst}"
    copy_dir_entries "${workspace_dir}/.agents/commands" "${commands_dst}"
  fi
}

# Translate unified Swarmforge agent definitions into the running harness's
# native subagent format.
#
# Unified definitions are markdown files whose YAML frontmatter is a superset
# of the OpenCode agent schema (description, mode, model, temperature, tools)
# plus optional per-harness override blocks (claude:, opencode:). One shared
# translator (swarmforge.agents.translate) emits each harness's dialect, so
# adding a new harness means adding an emitter there plus a case arm here.
#
# Unified Swarmforge agent definitions live under <dir>/agents in the
# harness-neutral .swarmforge asset layers, mounted read-only via
# SWARMFORGE_ASSETS_{USER,ORG,REPO}_DIR, plus <workspace>/.swarmforge/agents.
# One definition serves every harness; native agents/ directories inside
# harness config dirs are never transported by this asset pipeline. For
# OpenCode they still reach the harness through the layered config merge
# (the merged config dir is OpenCode's own discovery; see
# merge_config_layer), while for Claude they are excluded from the merge
# as well -- Claude-native definitions belong to Claude's own discovery
# (for example <workspace>/.claude/agents).
#
# Sources are identical for every harness and applied lowest- to
# highest-precedence (later files win by name): user, org, repo asset
# layers, then the workspace overlay. Only the destination differs.
prepare_unified_agents() {
  workspace_dir="${1:-/workspace}"
  translator="/usr/local/lib/swarmforge/swarmforge/agents/translate.py"

  [ -f "${translator}" ] || return 0

  case "${AGENT_BIN}" in
    claude)
      agents_dst="${CLAUDE_CONFIG_HOME}/agents"
      ;;
    opencode)
      agents_dst="${SWARMFORGE_CONFIG_DEST:-${ANVIL_HOME}/.config/opencode}/agents"
      ;;
    *)
      return 0
      ;;
  esac

  # The container-side python is the swarmforge package the Dockerfile copies
  # to /usr/local/lib/swarmforge; -P keeps the working directory off sys.path,
  # so a workspace that happens to contain a swarmforge/ directory cannot
  # shadow it. These run as root, before the drop to the invoking user.
  PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.agents.translate \
    "${AGENT_BIN}" "${agents_dst}" \
    "${SWARMFORGE_ASSETS_USER_DIR:-}/agents" \
    "${SWARMFORGE_ASSETS_ORG_DIR:-}/agents" \
    "${SWARMFORGE_ASSETS_REPO_DIR:-}/agents" \
    "${workspace_dir}/.swarmforge/agents" \
    || printf '%s\n' "Warning: unified agent translation failed for ${AGENT_BIN}; continuing" >&2
}

merge_config_layer() {
  src_dir="${1}"
  dst_dir="${2}"

  [ -n "${src_dir}" ] || return 0
  [ -d "${src_dir}" ] || return 0

  # Skip when src and dst resolve to the same underlying directory (for
  # example when CLAUDE_HOME_DIR=$HOME makes both layer paths bind-mounts of
  # the host's ~/.claude). Otherwise tar would try to extract entries on top
  # of themselves and abort.
  src_id="$(stat -c '%d:%i' "${src_dir}" 2>/dev/null || true)"
  dst_id="$(stat -c '%d:%i' "${dst_dir}" 2>/dev/null || true)"
  if [ -n "${src_id}" ] && [ "${src_id}" = "${dst_id}" ]; then
    return 0
  fi

  # .swarmforge/ asset dirs are read via their own mounts, never through the
  # config merge, so transporting them here would only litter the dest (or,
  # for Claude, accumulate junk in the persistent home).
  #
  # Skills and commands are excluded for every harness: they are populated
  # exclusively by copy_shared_assets so all layers get the same per-entry
  # replacement semantics (a higher layer's skill package replaces the whole
  # package, never file-merges into it). The tar merge would instead union
  # layers file-by-file.
  #
  # agents/ is excluded for Claude only -- prepare_unified_agents is its
  # sole source. OpenCode's merged config dir is native discovery, so layer
  # agents/ still merge through.
  #
  # settings.json is excluded for Claude for the same reason opencode.json
  # is excluded everywhere: it merges by key, through build_claude_settings.
  exclude_args="--exclude=./opencode.json --exclude=./.swarmforge"
  case "${AGENT_BIN:-}" in
    claude)
      exclude_args="${exclude_args} --exclude=./skills --exclude=./commands --exclude=./agents"
      exclude_args="${exclude_args} --exclude=./settings.json"
      ;;
    grok)
      # bin/downloads/completions are the host installer's own artifacts and
      # this dest is a persistent home: the container has its own
      # /usr/local/bin/grok, so copying them in would only leave them there.
      exclude_args="${exclude_args} --exclude=./skills --exclude=./commands --exclude=./bin --exclude=./downloads --exclude=./completions"
      ;;
    codex)
      exclude_args="${exclude_args} --exclude=./skills --exclude=./packages"
      exclude_args="${exclude_args} --exclude=./sessions --exclude=./history.jsonl --exclude=./log"
      exclude_args="${exclude_args} --exclude=./config.toml"
      ;;
    opencode)
      exclude_args="${exclude_args} --exclude=./skills --exclude=./command"
      ;;
  esac

  # Use a tar stream to avoid bind-mount same-file copy errors.
  # shellcheck disable=SC2086 # exclude_args intentionally word-split
  (
    cd "${src_dir}" && tar ${exclude_args} -cf - .
  ) | (
    cd "${dst_dir}" && tar -xf -
  )
}

merge_config_file() {
  src_file="${1}"
  dst_file="${2}"
  replace_mcp_entries="${3:-0}"

  [ -n "${src_file}" ] || return 0
  [ -f "${src_file}" ] || return 0

  if [ ! -f "${dst_file}" ]; then
    cp -f "${src_file}" "${dst_file}"
    return 0
  fi

  if [ "${replace_mcp_entries}" = "1" ]; then
    PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.config.merge_json \
      "${dst_file}" "${src_file}" --replace-mcp-entries
  else
    PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.config.merge_json \
      "${dst_file}" "${src_file}"
  fi
}

# Derived from the layers on every run; the destination is never read. The
# result stays off the persistent home -- one directory shared by every
# container for this user, where it would carry an org layer's permissions,
# hooks, and env into later runs that do not mount that layer -- and rides
# claude's command line instead (see the exec at the bottom).
build_claude_settings() {
  settings_dst="${1}"
  settings_repo_src="${2:-}"
  settings_user_src="${3:-}"
  settings_org_src="${4:-}"

  [ "${AGENT_BIN}" = "claude" ] || return 0

  image_defaults="/usr/local/share/swarmforge/claude-settings.json"

  mkdir -p "$(dirname "${settings_dst}")"

  # A failed build must still leave valid JSON at the path the exec hands
  # claude. An empty object is the safe reading of "no layer could be
  # applied".
  if ! PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.config.merge_json \
    --build "${settings_dst}" \
    "${image_defaults}" \
    "${settings_repo_src}/settings.json" \
    "${settings_user_src}/settings.json" \
    "${settings_org_src}/settings.json"
  then
    printf '%s\n' "Warning: could not build Claude settings.json; continuing" >&2
    printf '%s\n' '{}' > "${settings_dst}" || true
  fi
}

build_codex_config() {
  config_dst="${1}"
  config_repo_src="${2:-}"
  config_user_src="${3:-}"
  config_org_src="${4:-}"

  [ "${AGENT_BIN}" = "codex" ] || return 0

  PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.config.merge_toml \
    --build "${config_dst}/config.toml" \
    "${config_repo_src:+${config_repo_src}/config.toml}" \
    "${config_user_src:+${config_user_src}/config.toml}" \
    "${config_org_src:+${config_org_src}/config.toml}"
}

prepare_layered_config() {
  config_dst="${1}"
  user_config_src="${2:-}"
  org_config_src="${3:-}"
  repo_config_src="${4:-}"
  reset_config="${5:-0}"

  if [ "${reset_config}" = "1" ]; then
    rm -rf "${config_dst}"
  fi

  mkdir -p "${config_dst}"

  # Merge order (lowest to highest precedence): repo -> user -> org.
  #
  # Ordered by trust, not by specificity, because these files carry
  # permissions, hooks, and env: a checkout is whatever repo you cloned, and
  # the org layer is installed deliberately. That inverts the order the asset
  # pipelines use for skills, commands, and agents, where a repo's own
  # definitions are the most specific thing available and rightly win.
  merge_config_layer "${repo_config_src}" "${config_dst}"
  merge_config_file "${repo_config_src}/opencode.json" "${config_dst}/opencode.json"
  merge_config_layer "${user_config_src}" "${config_dst}"
  merge_config_file "${user_config_src}/opencode.json" "${config_dst}/opencode.json"
  merge_config_layer "${org_config_src}" "${config_dst}"
  merge_config_file "${org_config_src}/opencode.json" "${config_dst}/opencode.json"

  build_codex_config \
    "${config_dst}" \
    "${repo_config_src}" \
    "${user_config_src}" \
    "${org_config_src}"

  # Sidecar (tong) MCP servers, generated by the host launcher and bind-mounted
  # in read-only, merge last while yielding to same-named layer entries. The
  # variable is set only
  # for a harness that reads the fragment here, and each merges into its own
  # config file, so the fragment never lands in another harness's.
  case "${AGENT_BIN:-}" in
    grok|codex)
      # Servers go in a managed block the module rewrites each run rather than
      # being appended; this also removes stale entries when no tongs are set.
      PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.config.merge_toml_mcp \
        "${config_dst}/config.toml" ${SWARMFORGE_TONG_MCP_FILE:+"${SWARMFORGE_TONG_MCP_FILE}"}
      ;;
    *)
      # A no-op without the variable: merge_config_file ignores an empty or
      # missing source.
      merge_config_file "${SWARMFORGE_TONG_MCP_FILE:-}" "${config_dst}/opencode.json" 1
      ;;
  esac

  build_claude_settings \
    "${CLAUDE_SETTINGS_FILE}" \
    "${repo_config_src}" \
    "${user_config_src}" \
    "${org_config_src}"
}

prepare_agent_config() {
  config_dest="${SWARMFORGE_CONFIG_DEST:-}"
  reset_config="${SWARMFORGE_CONFIG_RESET:-0}"

  # Not the caller's to choose: a merged layer landing in the shared home
  # would outlive the container.
  [ "${AGENT_BIN}" != "claude" ] || config_dest="${CLAUDE_CONFIG_HOME}"

  # Codex keeps credentials and sessions beside config.toml, so its native
  # home stays persistent while configuration is rebuilt somewhere that dies
  # with the container. Publishing by copy does not change how Codex atomically
  # replaces credential files in the persistent directory.
  if [ "${AGENT_BIN}" = "codex" ]; then
    config_dest="${CODEX_CONFIG_HOME}"
    reset_config=1
  fi

  [ -n "${config_dest}" ] || return 0

  prepare_layered_config \
    "${config_dest}" \
    "${SWARMFORGE_CONFIG_USER_DIR:-}" \
    "${SWARMFORGE_CONFIG_ORG_DIR:-}" \
    "${SWARMFORGE_CONFIG_REPO_DIR:-}" \
    "${reset_config}"

  if [ "${AGENT_BIN}" = "codex" ]; then
    # Truncation clears the prior run even when no layer supplies config.toml.
    : > "${CODEX_CONFIG_FILE}"
    if [ -f "${CODEX_CONFIG_HOME}/config.toml" ]; then
      cp "${CODEX_CONFIG_HOME}/config.toml" "${CODEX_CONFIG_FILE}"
    fi
  fi
}

if [ ! -x "${AGENT_BIN_PATH}" ]; then
  printf '%s\n' "Agent binary not found: ${AGENT_BIN_PATH}" >&2
  exit 127
fi

# If we're not root, just run. (We can't create users/groups without root.)
if [ "$(id -u)" -ne 0 ]; then
  exec "${AGENT_BIN_PATH}" "$@"
fi

configure_timezone

# Ensure group exists for the target GID
if ! getent group "${ANVIL_GID}" >/dev/null 2>&1; then
  addgroup --gid "${ANVIL_GID}" "${ANVIL_GROUP}" >/dev/null 2>&1 || true
fi

# Ensure user exists for the target UID
if ! getent passwd "${ANVIL_UID}" >/dev/null 2>&1; then
  adduser --disabled-password --comment "" \
    --uid "${ANVIL_UID}" \
    --gid "${ANVIL_GID}" \
    --home "${ANVIL_HOME}" \
    "${ANVIL_USER}" >/dev/null 2>&1 || true
fi

prepare_agent_config
prepare_unified_agents
copy_shared_assets

if [ "${AGENT_BIN}" = "claude" ]; then
  link_claude_state
fi

chown -R "${ANVIL_UID}:${ANVIL_GID}" "${ANVIL_HOME}" 2>/dev/null || true
chown -Rh "${ANVIL_UID}:${ANVIL_GID}" "${CLAUDE_CONFIG_HOME}" 2>/dev/null || true
chown -R "${ANVIL_UID}:${ANVIL_GID}" /workspace 2>/dev/null || true

if [ "${AGENT_BIN}" = "claude" ]; then
  # Fix git worktree path resolution for bare-repo + worktree setups.
  #
  # Claude Code's /resume discovers sessions by running
  # `git worktree list --porcelain` and matching the output paths against
  # project directories in ~/.claude/projects/.  When the workspace is a
  # git worktree checked out from a bare repo, the worktree metadata stores
  # HOST paths.  Inside the container these paths don't exist, so Claude's
  # CWD-match fails and /resume reports "No conversations found to resume."
  #
  # Fix: install a thin git wrapper that rewrites the current worktree's
  # host path to the container CWD in `worktree list --porcelain` output.
  install_git_worktree_wrapper() {
    workspace="$(pwd)"
    dotgit="${workspace}/.git"

    # Only needed when .git is a file (i.e. a linked worktree).
    [ -f "${dotgit}" ] || return 0

    gitdir_ptr="$(sed -n 's/^gitdir: *//p' "${dotgit}")"
    [ -n "${gitdir_ptr}" ] || return 0

    # Read the reverse pointer to find the host-side worktree path.
    reverse_file="${gitdir_ptr}/gitdir"
    [ -f "${reverse_file}" ] || return 0

    host_dotgit="$(cat "${reverse_file}")"
    host_worktree="$(dirname "${host_dotgit}")"

    # Nothing to fix if paths already match.
    [ "${host_worktree}" != "${workspace}" ] || return 0

    real_git="$(command -v git)"
    wrapper_dir="/usr/local/libexec/swarmforge"
    mkdir -p "${wrapper_dir}"

    cat > "${wrapper_dir}/git" <<WRAPPER_EOF
#!/bin/sh
# Swarmforge git wrapper: rewrite worktree paths for container compatibility.
case "\$*" in
  *worktree*list*--porcelain*)
    "${real_git}" "\$@" | sed "s|^worktree ${host_worktree}\$|worktree ${workspace}|"
    ;;
  *)
    exec "${real_git}" "\$@"
    ;;
esac
WRAPPER_EOF
    chmod +x "${wrapper_dir}/git"
    export PATH="${wrapper_dir}:${PATH}"
  }
  install_git_worktree_wrapper
fi

# Command-line settings outrank every file, so the org layer beats even the
# checkout's own .claude/settings.json. `user` stays in the sources: that
# scope carries skills, commands, and agents discovery.
if [ "${AGENT_BIN}" = "claude" ] && [ -f "${CLAUDE_SETTINGS_FILE}" ]; then
  set -- --settings "${CLAUDE_SETTINGS_FILE}" --setting-sources user,project,local "$@"
fi

if [ "${AGENT_BIN}" = "claude" ]; then
  export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_HOME}"
fi

export HOME="${ANVIL_HOME}"

exec gosu "${ANVIL_UID}:${ANVIL_GID}" "${AGENT_BIN_PATH}" "$@"
