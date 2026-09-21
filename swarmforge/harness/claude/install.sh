#!/bin/sh
# Runs as root in anvil/Dockerfile's agent-runtime stage; the installer's own
# home is discarded so no login state is baked into the image.
set -eux
echo "Installing Claude Code (cache bust: ${SWARMFORGE_HARNESS_INSTALL_BUST})"
curl -fsSL https://claude.ai/install.sh | bash
claude_bin="$(readlink -f /root/.local/bin/claude)"
install -m 0755 "${claude_bin}" /usr/local/bin/claude
rm -rf /root/.claude /root/.claude.json /root/.local/share/claude
rm -f /root/.local/bin/claude
