#!/usr/bin/env python3
"""Translate portable slash commands into Codex skill packages.

Usage: python3 -m swarmforge.commands.translate <dest_dir> <src_dir>

The implementation lives in swarmforge.harness.codex.commands; this module
is the stable `python3 -m` invocation path for it.
"""

import sys

from swarmforge.harness.codex.commands import main

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
