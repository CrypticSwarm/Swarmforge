"""The launch failures and interruptions more than one launcher module needs.

Every other error the launcher raises is defined by the module that raises it.
These two cross modules: `OrchestrationError` is raised by both the secret
channel and the orchestrator, and `TerminationSignal` is raised by the entry
point but caught by the docker seam. Keeping them here means none of those
modules imports another.
"""


class OrchestrationError(Exception):
    """A tong could not be started/made ready; the launch stops."""


class TerminationSignal(BaseException):
    """SIGHUP or SIGTERM `signum` arrived mid-launch; unwinds through teardown.

    A `BaseException`, like `KeyboardInterrupt`, so no `except Exception` swallows it.
    """

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum
