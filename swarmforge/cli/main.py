"""Argument parsing and dispatch for the `swarmforge` command.

`COMMANDS` maps a subcommand name to the module that implements it, which
exposes `configure(parser)` and `run(options)` returning an exit code. Adding a
subcommand is adding a module and an entry here.

Exit codes: 2 for a usage error (argparse's own), 1 for a layout, git or tmux
failure reported as one line rather than a traceback, 130 for an interrupt, 0
otherwise.
"""

import argparse
import contextlib
import os
import sys

from swarmforge import command, worktrees

from . import clone, doctor, init, setup, tmux

COMMANDS = {
    "clone": clone,
    "init": init,
    "doctor": doctor,
    "setup": setup,
    "tmux": tmux,
}


def build_parser(checkout=None):
    """The `swarmforge` parser, with one subparser per entry in `COMMANDS`.

    A subcommand's module docstring is its help text, so the two cannot drift;
    it is already wrapped into paragraphs, which the raw formatter prints as
    written.
    """
    parser = argparse.ArgumentParser(
        prog="swarmforge",
        description="Set up Swarmforge, check readiness, manage worktree "
                    "repositories, and open them in tmux.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND",
                                       required=True)
    for name in sorted(COMMANDS):
        module = COMMANDS[name]
        summary = module.__doc__.strip().splitlines()[0]
        subparser = subparsers.add_parser(
            name, help=summary, description=module.__doc__.strip(),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        module.configure(subparser)
        subparser.set_defaults(checkout=checkout or os.getcwd())
    return parser


def main(argv, out=sys.stdout, err=sys.stderr, checkout=None):
    """Parse `argv`, run the subcommand it names, and return its exit code.

    argparse writes help and usage to the process streams and exits, so both
    are redirected onto `out`/`err` for the whole run; that carries a
    subcommand's plain `print` too.
    """
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            options = build_parser(checkout).parse_args(argv)
        except SystemExit as exc:
            return exc.code or 0

        try:
            return COMMANDS[options.command].run(options)
        except (command.CommandError, worktrees.LayoutError) as exc:
            err.write("swarmforge: %s\n" % exc)
            return 1
        except KeyboardInterrupt:
            err.write("swarmforge: interrupted\n")
            return 130
