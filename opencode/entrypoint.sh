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

link_shared_claude_skills() {
  skills_src="${SWARMFORGE_SKILLS_DIR:-}"
  [ -n "${skills_src}" ] || return 0
  [ -d "${skills_src}" ] || return 0

  skills_dst="${OPENCODE_HOME}/.claude/skills"
  mkdir -p "${skills_dst}"

  for skill_dir in "${skills_src}"/*; do
    [ -d "${skill_dir}" ] || continue
    [ -f "${skill_dir}/SKILL.md" ] || continue

    skill_name="$(basename "${skill_dir}")"
    skill_target="${skills_dst}/${skill_name}"

    if [ -e "${skill_target}" ] || [ -L "${skill_target}" ]; then
      continue
    fi

    ln -s "${skill_dir}" "${skill_target}" || true
  done
}

link_shared_claude_commands() {
  commands_src="${SWARMFORGE_COMMAND_DIR:-}"
  [ -n "${commands_src}" ] || return 0
  [ -d "${commands_src}" ] || return 0

  commands_dst="${OPENCODE_HOME}/.claude/commands"
  mkdir -p "${commands_dst}"

  for command_file in "${commands_src}"/*.md; do
    [ -f "${command_file}" ] || continue

    command_name="$(basename "${command_file}")"
    command_target="${commands_dst}/${command_name}"

    if [ -e "${command_target}" ] || [ -L "${command_target}" ]; then
      continue
    fi

    ln -s "${command_file}" "${command_target}" || true
  done
}

merge_config_layer() {
  src_dir="${1}"
  dst_dir="${2}"

  [ -n "${src_dir}" ] || return 0
  [ -d "${src_dir}" ] || return 0

  # Use a tar stream to avoid bind-mount same-file copy errors.
  (
    cd "${src_dir}" && tar -cf - .
  ) | (
    cd "${dst_dir}" && tar -xf -
  )
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
  merge_config_layer "${org_config_src}" "${config_dst}"
  merge_config_layer "${repo_config_src}" "${config_dst}"
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

chown -R "${OPENCODE_UID}:${OPENCODE_GID}" "${OPENCODE_HOME}" 2>/dev/null || true
chown -R "${OPENCODE_UID}:${OPENCODE_GID}" /workspace 2>/dev/null || true

if [ "${AGENT_BIN}" = "claude" ]; then
  link_shared_claude_skills
  link_shared_claude_commands

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
