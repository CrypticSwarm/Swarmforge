#!/bin/sh
# Runs as root in anvil/Dockerfile's agent-runtime stage.
set -eux
echo "Installing OpenCode (cache bust: ${SWARMFORGE_HARNESS_INSTALL_BUST})"
if [ -n "${SWARMFORGE_HARNESS_VERSION}" ]; then
  curl -fsSL https://opencode.ai/install | bash -s -- --version "${SWARMFORGE_HARNESS_VERSION}"
else
  curl -fsSL https://opencode.ai/install | bash
fi
install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode
