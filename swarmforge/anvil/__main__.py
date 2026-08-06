"""`python3 -m swarmforge.anvil` -- the host-side launcher, as its module form."""

import sys

from .cli import main

# Guarded, so importing this module (a collector walking the package, say) does
# not run the launcher and raise SystemExit at its import.
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
