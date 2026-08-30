#!/bin/sh
# Runs as root during the image build, from anvil/Dockerfile's harness-runtime
# stage. The status line and the settings that turn it on ship only in this
# image: no other harness reads Claude's settings.json. The defaults are the
# lowest config layer, and they are baked in rather than shipped from the
# checkout because they name a path only the image has.
set -eux
harness_dir="/usr/local/lib/swarmforge/swarmforge/harness/claude"
install -m 0755 "${harness_dir}/statusline.sh" /usr/local/bin/swarmforge-statusline
install -D -m 0644 "${harness_dir}/claude-settings.json" /usr/local/share/swarmforge/claude-settings.json
