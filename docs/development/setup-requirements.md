# Setup requirements

The setup wizard lives in `swarmforge/cli/setup.py`; the read-only report lives
in `swarmforge/doctor.py`. Both use the existing Make targets rather than
maintaining a second set of image tags, data roots, or config defaults.
Tong definitions and providers are checked once per distinct set of asset-layer
paths, even when several harnesses use those paths.

## Source map

| Source | Setup requirement and resulting behavior |
| --- | --- |
| `pyproject.toml`, `bin/swarmforge` | Python 3.9+ with only the standard library; run directly from a checkout before shell installation |
| `Makefile` | GNU Make and Bash; Linux Docker images, host bind mounts, host UID/GID, project-root resolution, optional Git config and environment-file mounts |
| `swarmforge/harness/__init__.py` | Registered harness names drive CLI choices; no independent list in the wizard |
| `swarmforge/harness/*/harness.mk` | Per-harness image, persistent data/home, and config-layer defaults; `doctor-config_<harness>` exports their resolved values |
| `install.sh` | Installs `~/.local/bin/swarmforge` and the OpenCode `oc` alias; the wizard delegates to this installer after confirmation |
| `anvil/Dockerfile` | Host Node/Ruby/harness installs are unnecessary; build the selected image through `build_<harness>` |
| `swarmforge/harness/spec.py`, `swarmforge/harness/init.py` | Container startup merges config, copies assets, translates agents, and fixes ownership; the smoke test deliberately bypasses this lifecycle |
| `swarmforge/tongs/discovery.py`, `validate.py` | Four asset layers merge by name; trusted definitions cannot be redefined by the workspace; doctor uses those same discovery and schema functions |
| `swarmforge/tongs/secrets.py` | Providers execute on the host; doctor checks mappings and executable availability without fetching values |
| `swarmforge/anvil/approval.py` | Workspace tong approval belongs to normal launch; setup cannot grant or bypass it |
| `docs/windows.md` | Native Windows shells are unsupported; run the tool and its prerequisites inside WSL 2 |

## Interaction and failure rules

- Start with harness selection and a read-only report.
- Stop before changes when a host or configuration prerequisite fails.
- Offer shell integration, each missing image build, and each temporary smoke test separately.
- Default each mutation to no; require a terminal and never interpret EOF as consent.
- Keep diagnostics bounded; image builds keep their normal terminal output and may take time.
- Keep secrets and provider output out of reports. Do not execute provider commands or persist approvals.
- Recheck after actions. Retain failed requirements in the exit status even if the user declines a repair.
- Print a launch command that uses the same checkout and project.

## Deferred runtime validation

The initial tool checks setup prerequisites, not every behavior of a session.
A real launch is still required for config parsing/merging, credentials, model
access, MCP connectivity, tong startup/readiness, secret-manager unlocks, GPU
availability, Git signing, and host filesystem sharing on all mounted paths.

The optional smoke test exercises only the selected image, project mount, and
harness executable. It cannot establish image freshness. It does not mount
personal storage/config or execute project code. No default readiness check
starts containers, creates networks, builds images, or downloads dependencies.

Potential follow-up work includes opt-in full lifecycle checks, validation of
config contents through per-harness declarations, sidecar image/build checks,
and explicit resource/GPU checks. Those checks must preserve the boundary
between diagnosis, secret resolution, and approval.

See [Installation](../installation.md#guided-setup-and-doctor) for commands,
report semantics, and limitations.
