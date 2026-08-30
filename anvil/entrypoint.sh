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
# portable skills and commands into the harness's native asset locations, link
# the state the harness keeps across runs into its config destination, then run
# whatever container preparation the harness needs root for. After those
# phases the driver hands the home, the harness's own root-built paths, and the
# workspace to the anvil uid, so the session owns what root prepared.
# The SWARMFORGE_CONFIG_*, SWARMFORGE_ASSETS_*, and SWARMFORGE_DOTAGENTS_*
# layer variables, SWARMFORGE_SKILLS_DIR, SWARMFORGE_COMMAND_DIR, and
# SWARMFORGE_TONG_MCP_FILE are read from the environment. This runs as root,
# before the privilege drop, and a failure here stops the container.
PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.harness.init "${AGENT_BIN}" "${ANVIL_HOME}" "${ANVIL_UID}" "${ANVIL_GID}"

# The user phase: privileges drop and the pre-exec driver replaces itself with
# the harness binary, with HOME set to the anvil home, the two variables this
# launch sets scrubbed back out, and the harness's pre_exec hook given the last
# word on the argv and the environment it starts with. PYTHONCOERCECLOCALE=0
# keeps interpreter startup from editing LC_CTYPE into the environment the
# binary inherits.
exec gosu "${ANVIL_UID}:${ANVIL_GID}" env PYTHONCOERCECLOCALE=0 PYTHONPATH=/usr/local/lib/swarmforge python3 -P -m swarmforge.harness.execute "${AGENT_BIN}" "${ANVIL_HOME}" -- "$@"
