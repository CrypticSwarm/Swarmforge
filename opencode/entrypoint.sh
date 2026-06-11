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

# Populate ~/.claude/skills and ~/.claude/commands for the Claude agent.
#
# Sources are applied lowest- to highest-precedence:
#   1. Harness shared assets (mounted via SWARMFORGE_SKILLS_DIR /
#      SWARMFORGE_COMMAND_DIR).
#   2. Workspace overlay: <workspace>/.agents/{skills,commands} preferred;
#      falls back to <workspace>/.opencode/{skills,command}.
#
# Entries are copied (not symlinked) so subsequent runs against a persistent
# CLAUDE_HOME_DIR can replace them idempotently without colliding with the
# layered tar merge.
copy_claude_shared_assets() {
  workspace_dir="${1:-/workspace}"

  skills_dst="${OPENCODE_HOME}/.claude/skills"
  commands_dst="${OPENCODE_HOME}/.claude/commands"

  copy_dir_entries "${SWARMFORGE_SKILLS_DIR:-}" "${skills_dst}"
  copy_dir_entries "${SWARMFORGE_COMMAND_DIR:-}" "${commands_dst}"

  for candidate in \
    "${workspace_dir}/.agents/skills" \
    "${workspace_dir}/.opencode/skills"; do
    if [ -d "${candidate}" ]; then
      copy_dir_entries "${candidate}" "${skills_dst}"
      break
    fi
  done

  for candidate in \
    "${workspace_dir}/.agents/commands" \
    "${workspace_dir}/.opencode/command" \
    "${workspace_dir}/.opencode/commands"; do
    if [ -d "${candidate}" ]; then
      copy_dir_entries "${candidate}" "${commands_dst}"
      break
    fi
  done
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
# Sources are applied lowest- to highest-precedence (later files win by name):
#   1. Claude: harness shared agents (mounted via SWARMFORGE_AGENTS_DIR).
#      OpenCode: the layered config merge already landed unified agents at
#      <config dest>/agents, so they are translated in place.
#   2. Workspace overlay: <workspace>/.agents/agents (for Claude, falling
#      back to <workspace>/.opencode/agents; OpenCode reads that natively).
prepare_unified_agents() {
  workspace_dir="${1:-/workspace}"
  translator="/usr/local/lib/swarmforge/translate_agents.py"

  [ -f "${translator}" ] || return 0

  overlay_src="${workspace_dir}/.agents/agents"
  case "${AGENT_BIN}" in
    claude)
      agents_dst="${OPENCODE_HOME}/.claude/agents"
      shared_src="${SWARMFORGE_AGENTS_DIR:-}"
      if [ ! -d "${overlay_src}" ]; then
        overlay_src="${workspace_dir}/.opencode/agents"
      fi
      ;;
    opencode)
      agents_dst="${SWARMFORGE_CONFIG_DEST:-${OPENCODE_HOME}/.config/opencode}/agents"
      shared_src="${agents_dst}"
      ;;
    *)
      return 0
      ;;
  esac

  python3 "${translator}" "${AGENT_BIN}" "${agents_dst}" "${shared_src}" "${overlay_src}" \
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

  # For Claude, skills/ and commands/ are managed by copy_claude_shared_assets
  # and may already exist (as directories or as stale symlinks left over from
  # older entrypoint logic) at the destination. Excluding them here avoids
  # `tar: ./skills: Cannot open: File exists` when the layered merge runs on
  # a persistent CLAUDE_HOME_DIR that already contains those entries.
  exclude_args="--exclude=./opencode.json"
  if [ "${AGENT_BIN:-}" = "claude" ]; then
    exclude_args="${exclude_args} --exclude=./skills --exclude=./commands"
  fi

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

  [ -n "${src_file}" ] || return 0
  [ -f "${src_file}" ] || return 0

  if [ ! -f "${dst_file}" ]; then
    cp -f "${src_file}" "${dst_file}"
    return 0
  fi

  python3 - "${dst_file}" "${src_file}" <<'PY'
import json
import sys

dst_path, src_path = sys.argv[1], sys.argv[2]

def merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            if key in out:
                out[key] = merge(out[key], value)
            else:
                out[key] = value
        return out
    return override

with open(dst_path, "r", encoding="utf-8") as f:
    dst = json.load(f)
with open(src_path, "r", encoding="utf-8") as f:
    src = json.load(f)

with open(dst_path, "w", encoding="utf-8") as f:
    json.dump(merge(dst, src), f, indent=2)
    f.write("\n")
PY
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

chown -R "${OPENCODE_UID}:${OPENCODE_GID}" "${OPENCODE_HOME}" 2>/dev/null || true
chown -R "${OPENCODE_UID}:${OPENCODE_GID}" /workspace 2>/dev/null || true

if [ "${AGENT_BIN}" = "claude" ]; then
  copy_claude_shared_assets

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
