"""`python3 -m swarmforge.tongs` -- the diagnostic CLI, as `bin/tongs` runs it."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
