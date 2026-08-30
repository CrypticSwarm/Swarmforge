#!/bin/sh
# Runs as root during the image build, from anvil/Dockerfile's agent-runtime
# stage. Leaves the codex binary at /usr/local/bin/codex: the installer reads
# the exported CODEX_INSTALL_DIR to place it there, and CODEX_HOME to keep its
# own state out of root's home.
set -eux
echo "Installing Codex CLI (cache bust: ${SWARMFORGE_HARNESS_INSTALL_BUST})"
export CODEX_HOME=/opt/codex CODEX_INSTALL_DIR=/usr/local/bin
curl -fsSL https://chatgpt.com/codex/install.sh | sh
