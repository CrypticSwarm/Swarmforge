#!/bin/sh
set -eu

# Entrypoint: create a user matching host UID/GID at runtime, then drop privileges

OPENCODE_UID="${OPENCODE_UID:-1000}"
OPENCODE_GID="${OPENCODE_GID:-1000}"
OPENCODE_USER="opencode"
OPENCODE_GROUP="opencode"
OPENCODE_HOME="/home/${OPENCODE_USER}"

# If we're not root, just run. (We can't create users/groups without root.)
if [ "$(id -u)" -ne 0 ]; then
  exec /usr/local/bin/opencode "$@"
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

export HOME="${OPENCODE_HOME}"

exec gosu "${OPENCODE_UID}:${OPENCODE_GID}" /usr/local/bin/opencode "$@"
