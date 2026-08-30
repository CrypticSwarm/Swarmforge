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
CLAUDE_SHARED_HOME="${ANVIL_HOME}/.claude"
CODEX_AGENTS_HOME="/run/swarmforge/codex-agents"

# State only: nothing claude loads as configuration or code belongs here.
CLAUDE_STATE_DIRS="projects sessions file-history session-env shell-snapshots
plans tasks todos backups cache paste-cache plugins"
CLAUDE_STATE_FILES="history.jsonl stats-cache.json keybindings.json
.last-cleanup scheduled_tasks.lock"

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
# next container. A link holds only what claude writes in place -- an entry
# rewritten by rename replaces it. A directory must exist before it is linked,
# or claude's own mkdir fails on the link. Runs after the config merge, which
# wipes this destination under SWARMFORGE_CONFIG_RESET.
link_claude_state() {
  mkdir -p "${CLAUDE_CONFIG_HOME}"

  # The one piece of state claude keeps beside its config dir, not inside it.
  link_claude_entry "${ANVIL_HOME}/.claude.json" ".claude.json"

  for entry in ${CLAUDE_STATE_DIRS}; do
    mkdir -p "${CLAUDE_SHARED_HOME}/${entry}" 2>/dev/null || true
    link_claude_entry "${CLAUDE_SHARED_HOME}/${entry}" "${entry}"
  done

  for entry in ${CLAUDE_STATE_FILES}; do
    link_claude_entry "${CLAUDE_SHARED_HOME}/${entry}" "${entry}"
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

# The root phases: merge the layered config (repo, then user, then org) into
# the harness's destination and run the harness's config hooks, then translate
# the unified agent definitions into the harness's native destination. The
# SWARMFORGE_CONFIG_* and SWARMFORGE_ASSETS_* layer variables and
# SWARMFORGE_TONG_MCP_FILE are read from the environment. This runs as root,
# before the privilege drop, and a failure here stops the container.
PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.harness.init "${AGENT_BIN}" "${ANVIL_HOME}"
copy_shared_assets

if [ "${AGENT_BIN}" = "claude" ]; then
  link_claude_state
fi

chown -R "${ANVIL_UID}:${ANVIL_GID}" "${ANVIL_HOME}" 2>/dev/null || true
chown -Rh "${ANVIL_UID}:${ANVIL_GID}" "${CLAUDE_CONFIG_HOME}" 2>/dev/null || true
chown -Rh "${ANVIL_UID}:${ANVIL_GID}" "${CODEX_AGENTS_HOME}" 2>/dev/null || true
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
  # Credentials are written by rename, which replaces a link, so the store is
  # named rather than linked. Claude's token-refresh lock sits in the same
  # directory, so concurrent containers rotate the shared token one at a time.
  export CLAUDE_SECURESTORAGE_CONFIG_DIR="${CLAUDE_SHARED_HOME}"
fi

export HOME="${ANVIL_HOME}"

exec gosu "${ANVIL_UID}:${ANVIL_GID}" "${AGENT_BIN_PATH}" "$@"
