#!/bin/sh
# Runs as root in anvil/Dockerfile's harness-runtime stage. The settings are
# baked in rather than shipped from the checkout: they name an image-only path.
set -eux
harness_dir="/usr/local/lib/swarmforge/swarmforge/harness/claude"
install -m 0755 "${harness_dir}/statusline.sh" /usr/local/bin/swarmforge-statusline
install -D -m 0644 "${harness_dir}/claude-settings.json" /usr/local/share/swarmforge/claude-settings.json
