"""Argument parsing and dispatch for the `swarmforge` command.

`COMMANDS` maps a subcommand name to the module that implements it, which
exposes `configure(parser)` and `run(options)` returning an exit code. Adding a
subcommand is adding a module and an entry here.

Exit codes: 2 for a usage error (argparse's own), 1 for a layout or git failure
reported as one line rather than a traceback, 130 for an interrupt, 0
otherwise.
"""

import argparse
import contextlib
import sys

from swarmforge import worktrees

from . import clone, init

COMMANDS = {
    "clone": clone,
    "init": init,
}


def build_parser():
    """The `swarmforge` parser, with one subparser per entry in `COMMANDS`.

    A subcommand's module docstring is its help text, so the two cannot drift;
    it is already wrapped into paragraphs, which the raw formatter prints as
    written.
    """
    parser = argparse.ArgumentParser(
        prog="swarmforge",
        description="Create git repositories laid out as a bare repo with "
                    "sibling worktrees.",
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
    return parser


def main(argv, out=sys.stdout, err=sys.stderr):
    """Parse `argv`, run the subcommand it names, and return its exit code.

    argparse writes help and usage to the process streams and exits, so both
    are redirected onto `out`/`err` for the whole run; that carries a
    subcommand's plain `print` too.
    """
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            options = build_parser().parse_args(argv)
        except SystemExit as exc:
            return exc.code or 0

        try:
            return COMMANDS[options.command].run(options)
        except (worktrees.GitError, worktrees.LayoutError) as exc:
            err.write("swarmforge: %s\n" % exc)
            return 1
        except KeyboardInterrupt:
            err.write("swarmforge: interrupted\n")
            return 130
