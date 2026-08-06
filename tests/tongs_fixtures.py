#!/usr/bin/env python3
"""Tong definitions shared by more than one swarmforge.tongs test module."""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The launcher's entry-point shim puts the repo root on the path; standing in
# for it here keeps this file runnable on its own, not just under a discovery
# run that already set it.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from swarmforge import tongs


GITHUB_TONG = """\
description: Holds GitHub credentials, exposes push/PR operations as MCP
lifecycle: session
image: ghcr.io/crypticswarm/github-tong@sha256:abc123
env:
  GITHUB_TOKEN: ${secret:op:op://Work/github/token}
interface:
  kind: mcp
  transport: http
  port: 8080
  name: github
mounts:
  - workspace:ro
networks:
  - some-existing-net
"""


PORT_TONG = """\
lifecycle: session
image: postgres:16
interface:
  kind: port
  port: 5432
  protocol: postgres
readiness:
  mode: tcp
"""


VOLUME_TONG = """\
lifecycle: session
image: cache-builder
interface:
  kind: volume
  volume: build-cache
  mountpoint: /cache
readiness:
  mode: healthcheck
  command: ["test", "-d", "/cache"]
"""


NONE_TONG = """\
lifecycle: session
image: log-shipper
interface:
  kind: none
readiness:
  mode: none
"""


def def_of(text):
    return tongs.load_yaml(text)
