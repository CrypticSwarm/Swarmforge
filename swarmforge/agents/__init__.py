"""Agent-definition tooling.

The unified agent format is the repo's own. `translate` drives one harness's
registered emitter over the unified definitions, and `emit` holds the
rendering and frontmatter helpers the emitters share; each harness's emitter
lives with its harness module under `swarmforge.harness`, so no hand-written
dialects are scattered across the tree.
"""
