"""The `swarmforge` command: git repositories laid out for worktrees.

`bin/swarmforge` -- linked onto `PATH` by `install.sh` -- runs this package,
and `python3 -m swarmforge.cli` is the same entry point. Two subcommands build
the same layout, one from a remote and one empty:

    swarmforge clone URL [PATH] [--branch NAME]
    swarmforge init PATH [--branch NAME]

Both produce a bare repository in `PATH/.git` with the default branch checked
out as the linked worktree `PATH/<branch>`, and print that worktree path.

One module per concern:

    main    argument parsing, the COMMANDS registry, and exit codes
    args    argparse `type` callables the subcommands share
    clone   the `clone` subcommand
    init    the `init` subcommand

A subcommand module exposes `configure(parser)` and `run(options)`; the layout
work itself lives in `swarmforge.worktrees`, which these call.

The package's public names are re-exported below, so a caller imports
`swarmforge.cli` without knowing which module a name sits in. Each re-export is
a fresh binding rather than an alias, so anything that redirects a name -- a
test replacing a function with a fake -- has to redirect it on the module that
owns it, not on this one.
"""

from .args import nonempty
from .main import COMMANDS, build_parser, main

__all__ = [
    "nonempty",
    "COMMANDS",
    "build_parser",
    "main",
]
