"""`python3 -m swarmforge.cli` -- the swarmforge command, as `bin/swarmforge` runs it."""

import sys

from .main import main

# Guarded so importing this module does not run the CLI and exit.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
