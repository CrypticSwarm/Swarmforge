"""Argument types and option groups the subcommand modules share."""

import argparse

from swarmforge import harness


def nonempty(value):
    """An argparse `type` that refuses an empty argument.

    An empty URL, path or branch would otherwise reach git as a real argument
    and fail somewhere in the middle of the build.
    """
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def configure_readiness(parser, harness_help):
    """Register checkout, project, and harness options for setup checks."""
    parser.add_argument(
        "--checkout", type=nonempty,
        help="Swarmforge checkout (default: entry point's checkout)")
    parser.add_argument(
        "--project", type=nonempty, default=".",
        help="Project directory (default: current directory)")
    parser.add_argument("--harness", action="append", choices=harness.names(),
                        help=harness_help)
