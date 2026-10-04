"""The launch failures and interruptions raised from more than one launcher module.

Every other error the launcher raises is defined by the module that raises it.
`OrchestrationError` has two sources -- the secret channel, when a tong never
takes delivery of its secrets, and the orchestrator, when a tong cannot be
started or made ready -- and `TerminationSignal` is raised by the entry point's
signal handler but caught by the docker seam as well, so both sit on their own
and none of those modules has to import another.
"""


class OrchestrationError(Exception):
    """A tong could not be started/made ready; the launch stops."""


class TerminationSignal(BaseException):
    """SIGHUP or SIGTERM arrived mid-launch; unwinds through teardown like Ctrl-C.

    A `BaseException`, as `KeyboardInterrupt` is, so no `except Exception` on the
    way out swallows it. `signum` is the signal that was received.
    """

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum
