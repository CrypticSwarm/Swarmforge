# Harness lifecycle

A harness is one directory, `swarmforge/harness/<name>/`, holding everything Swarmforge knows about it:

- `__init__.py` — the spec module: a `HarnessSpec` and the hook functions it points at.
- `harness.mk` — the fragment the Makefile includes to generate `build_<name>`, `update_<name>`, `run_<name>`, `stop_<name>`, and `name_<name>`, and to add that build to `build_harnesses`.
- `install.sh` — run by the image build, leaving the harness binary under `/usr/local/bin/`.
- `image.sh` — optional, run next, installing any build-time assets the harness ships (Claude's `statusline.sh` and `claude-settings.json`).

`swarmforge/harness/__init__.py` maps each name to its module in a static dict, so the set of harnesses is greppable and closed at image build time.

Every run walks the same phases against that spec:

- **initialize** — merge the three config layers into the harness's config destination, build its config, merge the tong MCP servers the way its `mcp_merge` names, then finalize and publish.
- **translate-agents** — write the unified agent definitions into the harness's native format and destination.
- **install-assets** — install the portable skills and commands into its native asset locations, layer by layer.
- **link-state** — link the state that has to outlive the container back into the config destination.
- **root-setup** — whatever container preparation the harness needs root for (Claude's git worktree wrapper).
- **handover** — chown the home, the paths the harness builds outside it, and the workspace to the anvil uid.
- **pre-exec** — after privileges drop, the harness's `pre_exec` hook has the last word on the argv and the environment the binary is exec'd with.

`anvil/entrypoint.sh` carries no harness-specific logic: it configures the timezone, creates the user, and invokes the two drivers — `swarmforge.harness.init` as root, then `swarmforge.harness.execute` as the anvil user.

Adding a harness is one new directory and one line in the registry dict. `tests/test_harness_conformance.py` runs over the registry, so the new spec's completeness, its behavior in each phase, and its recorded run argv are checked with no test to write.
