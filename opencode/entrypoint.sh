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

if [ ! -x "${AGENT_BIN_PATH}" ]; then
  printf '%s\n' "Agent binary not found: ${AGENT_BIN_PATH}" >&2
  exit 127
fi

# If we're not root, just run. (We can't create users/groups without root.)
if [ "$(id -u)" -ne 0 ]; then
  exec "${AGENT_BIN_PATH}" "$@"
fi

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

chown -R "${OPENCODE_UID}:${OPENCODE_GID}" "${OPENCODE_HOME}" 2>/dev/null || true
chown -R "${OPENCODE_UID}:${OPENCODE_GID}" /workspace 2>/dev/null || true

if [ "${AGENT_BIN}" = "claude" ]; then
  link_shared_claude_skills
  link_shared_claude_commands
fi

export HOME="${OPENCODE_HOME}"

exec gosu "${OPENCODE_UID}:${OPENCODE_GID}" "${AGENT_BIN_PATH}" "$@"
