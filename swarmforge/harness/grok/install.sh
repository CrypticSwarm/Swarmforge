#!/bin/sh
# Runs as root in anvil/Dockerfile's agent-runtime stage; the installer's own
# home is discarded so no login state is baked into the image.
set -eux
echo "Installing Grok Build CLI (cache bust: ${SWARMFORGE_HARNESS_INSTALL_BUST})"
curl -fsSL https://x.ai/cli/install.sh | bash
grok_bin="$(readlink -f /root/.grok/bin/grok)"
install -m 0755 "${grok_bin}" /usr/local/bin/grok
rm -rf /root/.grok
