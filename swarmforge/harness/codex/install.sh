#!/bin/sh
# Runs as root in anvil/Dockerfile's agent-runtime stage; CODEX_HOME keeps the
# installer's own state out of root's home.
set -eux
echo "Installing Codex CLI (cache bust: ${SWARMFORGE_HARNESS_INSTALL_BUST})"
export CODEX_HOME=/opt/codex CODEX_INSTALL_DIR=/usr/local/bin
curl -fsSL https://chatgpt.com/codex/install.sh | sh
