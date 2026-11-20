# Installation

The three install steps are in the [repo README](../README.md); this page carries the detail behind them.
Every variable below has a row in [Environment variables](reference/environment.md), and every target one in [Make targets](reference/make-targets.md).

## Prerequisites

The host needs Docker with BuildKit, git, GNU make, bash, and Python 3.9 or newer; the launcher uses only the standard library.
On Windows these live inside WSL: see [Windows (WSL)](windows.md).

## Guided setup and doctor

No Python package install is needed. From a fresh checkout:

```bash
python3 bin/swarmforge setup
```

1. Select one or more harnesses, or `all`.
2. Review the readiness report. Fix failed host prerequisites before continuing.
3. Optionally run the existing shell installer. It links the CLI and appends the `oc` alias.
4. Optionally build each missing image through its existing Make target.
5. Optionally start a temporary container to check the image, harness executable, and read-only project mount.
6. Review the final report and launch command. Complete the harness's login in that session.

Each action defaults to **no**. Setup requires an interactive terminal; EOF never
consents to an action. It does not run a package manager, use `sudo`, edit Git
identity, obtain credentials, or approve workspace tongs. Host tools must be
installed using your operating system's process. Node, Ruby, and the harness
binaries belong to the images; they are not host prerequisites.

Once the CLI is on PATH:

```bash
swarmforge setup --harness codex --harness claude --project /path/to/project
swarmforge doctor --harness codex --project /path/to/project
swarmforge doctor --harness codex --json
```

Doctor defaults to OpenCode; repeat `--harness` to check several. Without the
installed command, use `python3 /path/to/Swarmforge/bin/swarmforge doctor`.
The shim finds its own checkout even when called through the installed symlink
from a project directory. `python3 -m swarmforge.cli doctor` defaults to the
current directory as its checkout; pass `--checkout /path/to/Swarmforge` when needed.

The Make targets accept `DOCTOR_ARGS` and `SETUP_ARGS`:

```bash
make doctor DOCTOR_ARGS='--harness codex --json'
make setup SETUP_ARGS='--harness claude'
```

Checks resolve settings through `doctor-config_<harness>`, which shares the
run target's config defaults. Export overrides when invoking the CLI directly;
Make command-line overrides also reach `make doctor` and `make setup`:

```bash
CODEX_IMG=team-codex:local CODEX_HOME_DIR=/srv/codex/home swarmforge doctor --harness codex
make doctor CODEX_IMG=team-codex:local DOCTOR_ARGS='--harness codex'
```

A build offered by setup uses the same overrides. These commands do not save
configuration: keep overrides in your shell environment or launch command for
future sessions. Paths follow GNU Make's existing variable-expansion rules;
literal dollar signs in variable values require Make escaping (`$$`).

### Report scope

| Requirement | What is checked |
| --- | --- |
| Host | Linux/macOS (including WSL), current Python 3.9+, Git, GNU Make, Bash, Docker CLI |
| Docker | Reachable Linux daemon and available Buildx plugin; the optional smoke test exercises a project bind mount |
| Harness images | Selected image tags exist on the active daemon; missing images include a build command |
| Storage | Data, home, user-assets, and user-config directories are writable, or their nearest existing parent allows creation |
| Config layers | Config directory paths are reported; absent optional org/repo layers warn |
| Shared assets | Shared skills and commands directories exist and are readable |
| Git | The mounted `GITCONFIG_FILE` contains `user.name` and `user.email`; values are not printed |
| Environment file | An existing `ENV_FILE` is a readable file; contents are not printed |
| Tongs | Effective layered definitions match the existing schema; skipped definitions fail and workspace approval is left to launch |
| Secret providers | Referenced provider commands are mapped and their executables are available; commands are never executed |

`PASS` applies only to the named check. `WARN` identifies an optional or untested
condition. `FAIL` means a requirement is missing. `SKIP` means a prerequisite
prevented a check. Exit status is **1** if any check fails and **0** otherwise;
usage errors return **2**, and Ctrl-C returns **130**. Warnings alone return 0.
JSON output contains `checks` and `exit_code`.

The smoke test overrides the image entrypoint, disables network access, never
pulls an image, and mounts the resolved project root read-only. It checks that
`/bin/sh` starts, the mount exists, and the harness executable is present. It does
not run the normal startup lifecycle, log in, or prove model/MCP connectivity.
Docker must allow access to the host paths; a remote context needs the same
paths on its daemon host. A successful image check does not prove the image is
up to date with this checkout.

Config contents, provider authentication, sidecar images, GPU support, network
access, Git signing/remote access, and full harness runtime readiness are outside
the initial report. A normal launch remains the final validation. Checks run
with the invoking process's permissions; a restricted sandbox can report paths
or the Docker socket inaccessible even when your normal terminal can access them.

The [setup requirements map](development/setup-requirements.md) records the
source files behind these checks and the deferred checks.

## The install script

Run it with `bash` (it uses Bash arrays) even if your login shell is Zsh.
On macOS it prefers `~/.zshrc` and falls back to `~/.bash_profile`, so you don't need to create `~/.bashrc` manually.

## Building the images

To pin OpenCode to a specific release instead of latest:

```bash
make build_opencode OPENCODE_VERSION=1.4.14
make update_opencode OPENCODE_VERSION=1.4.14
```

The four harness images share the same Debian base and toolchain (Node.js, Python, and Ruby; see `anvil/Dockerfile`).

## Multiple aliases (work/personal)

Define multiple aliases that point at the same Swarmforge checkout but use different storage roots and git identities (for example: work keys vs personal keys):

```bash
alias ocd='make -C PATH_TO_SWARMFORGE run_opencode PROJECT_DIR=$(pwd) DATA_DIR=$HOME/.local/share/opencode-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
alias ccd='make -C PATH_TO_SWARMFORGE run_claude PROJECT_DIR=$(pwd) CLAUDE_DATA_DIR=$HOME/.local/share/claude-work GITCONFIG_FILE=$HOME/.gitconfig-agent'
```

- `GITCONFIG_FILE` points at an agent-specific git config instead of `~/.gitconfig`.
- Separate `CLAUDE_DATA_DIR`, `GROK_DATA_DIR`, and `CODEX_DATA_DIR` roots isolate work and personal logins and session state; `DATA_DIR` does the same for OpenCode.
- Config layering uses `SWARMFORGE_USER_CONFIG_DIR`, `SWARMFORGE_ORG_CONFIG_DIR`, and `SWARMFORGE_REPO_CONFIG_DIR` (their defaults differ per harness — see [Config layers](configuration.md#config-layers)). Set `SWARMFORGE_ORG_CONFIG_ROOT=/path/to/org-repo` to resolve org defaults to each harness's own directory under that root.
