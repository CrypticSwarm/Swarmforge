#!/bin/sh
set -eu

# Entrypoint: create a user matching host UID/GID at runtime, then drop privileges.

OPENCODE_UID="${OPENCODE_UID:-${SWARMFORGE_UID:-1000}}"
OPENCODE_GID="${OPENCODE_GID:-${SWARMFORGE_GID:-1000}}"
OPENCODE_USER="opencode"
OPENCODE_GROUP="opencode"
OPENCODE_HOME="/home/${OPENCODE_USER}"
AGENT_BIN="${SWARMFORGE_AGENT_BIN:-opencode}"
AGENT_BIN_PATH="/usr/local/bin/${AGENT_BIN}"

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
# For Claude the destinations are container-private tmpfs mounts masking the
# shared persistent home, so each container starts empty and sees only the
# layers for this run; nothing persists across runs or leaks between repos.
copy_shared_assets() {
  workspace_dir="${1:-/workspace}"

  case "${AGENT_BIN}" in
    claude)
      skills_dst="${OPENCODE_HOME}/.claude/skills"
      commands_dst="${OPENCODE_HOME}/.claude/commands"
      ;;
    opencode)
      config_dest="${SWARMFORGE_CONFIG_DEST:-${OPENCODE_HOME}/.config/opencode}"
      skills_dst="${config_dest}/skills"
      commands_dst="${config_dest}/command"
      ;;
    *)
      return 0
      ;;
  esac

  for layer_src in "${SWARMFORGE_DOTAGENTS_USER_DIR:-}" "${SWARMFORGE_DOTAGENTS_ORG_DIR:-}"; do
    [ -n "${layer_src}" ] || continue
    copy_dir_entries "${layer_src}/skills" "${skills_dst}"
    copy_dir_entries "${layer_src}/commands" "${commands_dst}"
  done

  copy_dir_entries "${SWARMFORGE_SKILLS_DIR:-}" "${skills_dst}"
  copy_dir_entries "${SWARMFORGE_COMMAND_DIR:-}" "${commands_dst}"

  copy_dir_entries "${workspace_dir}/.agents/skills" "${skills_dst}"
  copy_dir_entries "${workspace_dir}/.agents/commands" "${commands_dst}"
}

# Translate unified Swarmforge agent definitions into the running harness's
# native subagent format.
#
# Unified definitions are markdown files whose YAML frontmatter is a superset
# of the OpenCode agent schema (description, mode, model, temperature, tools)
# plus optional per-harness override blocks (claude:, opencode:). One shared
# translator (translate_agents.py) emits each harness's dialect, so adding a
# new harness means adding an emitter there plus a case arm here.
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
  translator="/usr/local/lib/swarmforge/translate_agents.py"

  [ -f "${translator}" ] || return 0

  case "${AGENT_BIN}" in
    claude)
      agents_dst="${OPENCODE_HOME}/.claude/agents"
      ;;
    opencode)
      agents_dst="${SWARMFORGE_CONFIG_DEST:-${OPENCODE_HOME}/.config/opencode}/agents"
      ;;
    *)
      return 0
      ;;
  esac

  python3 "${translator}" "${AGENT_BIN}" "${agents_dst}" \
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
  # agents/ is additionally excluded for Claude only: there the destination
  # is a container-private tmpfs mask over the shared persistent home,
  # populated solely by prepare_unified_agents, and excluding it keeps
  # host-side strays (for example user-layer agents accumulated by older
  # entrypoint logic) from reappearing. For OpenCode the merged config dir
  # is the harness's own native discovery, so native agents/ in user/org
  # layers still merge through.
  exclude_args="--exclude=./opencode.json --exclude=./.swarmforge"
  case "${AGENT_BIN:-}" in
    claude)
      exclude_args="${exclude_args} --exclude=./skills --exclude=./commands --exclude=./agents"
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

merge_opencode_json() {
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
    python3 /usr/local/lib/swarmforge/merge_opencode_json.py \
      "${dst_file}" "${src_file}" --replace-mcp-entries
  else
    python3 /usr/local/lib/swarmforge/merge_opencode_json.py \
      "${dst_file}" "${src_file}"
  fi
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

  # Merge order (lowest to highest precedence): user -> org -> repo
  merge_config_layer "${user_config_src}" "${config_dst}"
  merge_opencode_json "${user_config_src}/opencode.json" "${config_dst}/opencode.json"
  merge_config_layer "${org_config_src}" "${config_dst}"
  merge_opencode_json "${org_config_src}/opencode.json" "${config_dst}/opencode.json"
  merge_config_layer "${repo_config_src}" "${config_dst}"
  merge_opencode_json "${repo_config_src}/opencode.json" "${config_dst}/opencode.json"

  # Sidecar (tong) MCP servers, generated by the host launcher and bind-mounted
  # in read-only, merge last so they take precedence. The variable is only set
  # for OpenCode sessions that discovered an MCP-interface tong; otherwise this
  # is a no-op (merge_opencode_json ignores an empty or missing source).
  merge_opencode_json "${SWARMFORGE_TONG_MCP_FILE:-}" "${config_dst}/opencode.json" 1
}

prepare_agent_config() {
  config_dest="${SWARMFORGE_CONFIG_DEST:-}"
  [ -n "${config_dest}" ] || return 0

  prepare_layered_config \
    "${config_dest}" \
    "${SWARMFORGE_CONFIG_USER_DIR:-}" \
    "${SWARMFORGE_CONFIG_ORG_DIR:-}" \
    "${SWARMFORGE_CONFIG_REPO_DIR:-}" \
    "${SWARMFORGE_CONFIG_RESET:-0}"
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
if ! getent group "${OPENCODE_GID}" >/dev/null 2>&1; then
  addgroup --gid "${OPENCODE_GID}" "${OPENCODE_GROUP}" >/dev/null 2>&1 || true
fi

# Ensure user exists for the target UID
if ! getent passwd "${OPENCODE_UID}" >/dev/null 2>&1; then
  adduser --disabled-password --comment "" \
    --uid "${OPENCODE_UID}" \
    --gid "${OPENCODE_GID}" \
    --home "${OPENCODE_HOME}" \
    "${OPENCODE_USER}" >/dev/null 2>&1 || true
fi

prepare_agent_config
prepare_unified_agents
copy_shared_assets

chown -R "${OPENCODE_UID}:${OPENCODE_GID}" "${OPENCODE_HOME}" 2>/dev/null || true
chown -R "${OPENCODE_UID}:${OPENCODE_GID}" /workspace 2>/dev/null || true

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

export HOME="${OPENCODE_HOME}"

exec gosu "${OPENCODE_UID}:${OPENCODE_GID}" "${AGENT_BIN_PATH}" "$@"
