"""`python3 -m swarmforge.cli` -- the swarmforge command, as `bin/swarmforge` runs it."""

import sys

from .main import main

# Guarded, so importing this module (a collector walking the package, say) does
# not run the CLI and raise SystemExit at its import.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
