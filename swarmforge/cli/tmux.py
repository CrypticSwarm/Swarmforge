"""Open repositories as tmux sessions, with a window per worktree."""

import argparse

from swarmforge import tmux

from .args import nonempty

OPEN_DESCRIPTION = """\
Open the repository holding PATH as a tmux session, one window per worktree.

The session is named for the repository's root directory. Running it again
only adds windows for worktrees that have none.
"""


def configure(parser):
    actions = parser.add_subparsers(dest="tmux_action", metavar="ACTION",
                                    required=True)
    opener = actions.add_parser(
        "open", help=OPEN_DESCRIPTION.splitlines()[0],
        description=OPEN_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    opener.add_argument(
        "path", metavar="PATH", nargs="?", default=".", type=nonempty,
        help="any directory inside the repository; defaults to the current "
             "one")
    opener.add_argument(
        "--name", metavar="NAME", type=nonempty,
        help="session name, instead of the repository root's directory name")
    opener.add_argument(
        "-d", "--detach", action="store_true",
        help="set the session up without switching to it, and print its name")


def open_action(options):
    session = tmux.open_session(options.path, name=options.name)
    if options.detach:
        print(session)
    else:
        tmux.attach(session)
    return 0


ACTIONS = {
    "open": open_action,
}


def run(options):
    return ACTIONS[options.tmux_action](options)
