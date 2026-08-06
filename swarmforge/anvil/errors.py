"""The one launch failure raised from more than one launcher module.

Every other error the launcher raises is defined by the module that raises it.
This one has two sources -- the secret channel, when a tong never takes delivery
of its secrets, and the orchestrator, when a tong cannot be started or made
ready -- so it sits on its own and neither has to import the other.
"""


class OrchestrationError(Exception):
    """A tong could not be started/made ready; the launch stops."""
