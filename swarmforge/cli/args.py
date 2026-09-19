"""argparse `type` callables the subcommand modules share."""

import argparse


def nonempty(value):
    """An argparse `type` that refuses an empty argument.

    An empty URL, path or branch would otherwise reach git as a real argument
    and fail somewhere in the middle of the build.
    """
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value
