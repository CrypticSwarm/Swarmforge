"""Registry of the harnesses Swarmforge can drive.

One module per harness, each declaring a `HarnessSpec` (see `spec`) plus the
functions the spec points at. The registry is a static dict rather than
directory discovery: greppable, import-order explicit, and closed at image
build time.
"""

from swarmforge.harness import claude, codex, grok, opencode
from swarmforge.harness.spec import provided

_REGISTRY = {
    "claude": claude,
    "codex": codex,
    "grok": grok,
    "opencode": opencode,
}


def get(name):
    """The module for the harness registered under `name`, or None."""
    return _REGISTRY.get(name)


def names():
    """Every registered harness name, sorted."""
    return sorted(_REGISTRY)


def agent_override_keys():
    """Frontmatter keys that name per-harness override blocks.

    A harness claims its own name as an override key exactly when it defines
    an agent emitter to consume the block.
    """
    return {
        name for name, module in _REGISTRY.items()
        if provided(module.SPEC.agent_emitter)
    }
