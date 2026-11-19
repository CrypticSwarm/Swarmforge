"""Run the external commands the `swarmforge` CLI drives, git and tmux.

A leaf: nothing in the package is below it.
"""

import subprocess


class CommandError(Exception):
    """A command failed; str(exc) names it and how it exited.

    `returncode` is its exit status, and None when it could not be run.
    """

    def __init__(self, message, returncode=None):
        super().__init__(message)
        self.returncode = returncode


def not_runnable(argv, exc):
    """The message for `argv` failing to start with the OSError `exc`."""
    return "could not run %s: %s" % (" ".join(argv), exc.strerror or exc)


def run(argv, error=CommandError, capture=False, quiet=False, env=None):
    """Run `argv`, returning its stdout when `capture` is set.

    Failure raises `error`, a `CommandError` subclass. Only a captured result
    comes back on stdout; everything else the command says reaches the
    terminal on stderr, so a caller's own stdout holds only what it prints.
    `quiet` discards stderr, for a probe whose failure the caller reports
    itself. stdin is inherited, for ssh and credential prompts.
    """
    try:
        # Raw fd 2: a caller's `sys.stderr` may be a stream with no fileno.
        completed = subprocess.run(
            argv, text=True, env=env,
            stdout=subprocess.PIPE if capture else 2,
            stderr=subprocess.DEVNULL if quiet else None)
    except OSError as exc:
        raise error(not_runnable(argv, exc))
    if completed.returncode != 0:
        raise error("%s exited %d" % (" ".join(argv), completed.returncode),
                    completed.returncode)
    return completed.stdout if capture else None
