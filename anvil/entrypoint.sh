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
# the harness's destination and run the harness's config hooks, translate the
# unified agent definitions into the harness's native destination, install the
# portable skills and commands into the harness's native asset locations, then
# link the state the harness keeps across runs into its config destination.
# The SWARMFORGE_CONFIG_*, SWARMFORGE_ASSETS_*, and SWARMFORGE_DOTAGENTS_*
# layer variables, SWARMFORGE_SKILLS_DIR, SWARMFORGE_COMMAND_DIR, and
# SWARMFORGE_TONG_MCP_FILE are read from the environment. This runs as root,
# before the privilege drop, and a failure here stops the container.
PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.harness.init "${AGENT_BIN}" "${ANVIL_HOME}"

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
