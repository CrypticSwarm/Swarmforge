#!/usr/bin/env python3
"""The container's user-phase pre-exec driver.

Runs as the anvil user once the root phases are done, sets HOME to the anvil
home, drops the variables its own launch added, hands the argv and the
environment to the harness's `pre_exec` hook, and replaces itself with the
harness binary through execve. A harness that keeps the default hook is exec'd
byte-identically to a direct exec of the binary. Invoked as
`HARNESS HOME -- [ARG...]`, where everything after the separator is the
session's own arguments.

The launch sets PYTHONCOERCECLOCALE=0 because the image sets no LANG or LC_*:
CPython's C-locale coercion would otherwise put LC_CTYPE in this process's
environment at interpreter startup, and the exec below would pass it on to the
harness as though the container had been given it.
"""

import os
import signal
import sys

from swarmforge import harness
from swarmforge.harness import init

USAGE = "usage: python3 -m swarmforge.harness.execute HARNESS HOME -- [ARG...]"

# Where the image installs every harness binary.
BIN_DIR = "/usr/local/bin"

# The variables this driver's own launch adds, which the exec it performs must
# not pass on. A value the run itself carried is dropped with them: by the
# time this runs, the two are indistinguishable.
LAUNCH_VARS = ("PYTHONPATH", "PYTHONCOERCECLOCALE")

# Signals the interpreter sets to "ignore" at startup. An ignored disposition
# survives exec, so left alone the binary -- and every process it spawns --
# would start with signal handling a direct exec never gave it.
IGNORED_SIGNALS = (signal.SIGPIPE, signal.SIGXFSZ)


def run(name, home, args, environ, execv=os.execve):
    """Exec the harness registered as `name` with the environment it expects.

    The file exec'd is always the binary the spec names; the hook has the last
    word on the argv and the environment it starts with.
    """
    module = harness.get(name)
    if module is None:
        print("unknown harness: %s" % name, file=sys.stderr)
        return 2
    spec = module.SPEC

    env = dict(environ)
    for var in LAUNCH_VARS:
        env.pop(var, None)
    env["HOME"] = home

    binary = BIN_DIR + "/" + spec.binary
    ctx = init.asset_context(spec, home, environ, cwd=os.getcwd())
    argv, env = spec.pre_exec(ctx, [binary] + list(args), env)
    for sig in IGNORED_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)
    execv(binary, argv, env)
    return 0


def main(argv):
    if len(argv) < 3 or argv[2] != "--":
        print(USAGE, file=sys.stderr)
        return 2
    return run(argv[0], argv[1], argv[3:], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
