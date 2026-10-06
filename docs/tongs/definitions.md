# Definition format

```yaml
# ~/.swarmforge/tongs/github-creds.yaml
description: Holds GitHub credentials, exposes push/PR operations as MCP
lifecycle: session            # session | shared (required)
image: ghcr.io/example/github-tong@sha256:...   # required; pinned digest recommended
env:
  GITHUB_TOKEN: ${secret:op:op://Work/github/token}  # resolved on the host launcher
  LOG_LEVEL: info             # plain values pass through as ordinary -e env
interface:                    # required; how (or whether) the anvil reaches the tong
  kind: mcp                   # mcp | port | volume | none
  transport: http             # http only in v1
  port: 8080                  # the port the server listens on inside the container
  name: github                # canonical MCP server name the agent sees
# aliases: [gh, git.example]  # optional extra DNS names the tong also answers to
mounts:                       # opt-in magic words only, never raw host paths
  - workspace:ro              # or workspace:/code:ro to bind it somewhere else
resources:
  memory: 512m                # string or number
networks:                     # optional extra pre-existing networks to also join
  - some-existing-net
# entrypoint: [...]           # optional argv override of the image ENTRYPOINT
# command: [...]              # optional argv override of the image CMD
```

Required fields: `lifecycle`, `image`, and `interface` (with a valid `kind`). Unknown keys are tolerated for forward compatibility.

## Interface kinds

The `interface:` block drives what gets injected into the anvil, how readiness is checked, and what plumbing is wired up:

- **`mcp`** — an HTTP MCP server (the common case). Requires `port` and `name`; `transport` defaults to `http`. Injects per-harness MCP config pointing at `http://<name>:<port>/mcp` on the session network (`interface.path` overrides the `/mcp` suffix). TCP readiness probe by default.
- **`port`** — a non-MCP network service. Requires `port`; optional `protocol` is informational. Injects `SWARMFORGE_TONG_<NAME>_HOST` (the canonical alias) and `SWARMFORGE_TONG_<NAME>_PORT` so the anvil composes its own connection string. TCP readiness probe by default.
- **`volume`** — a shared named volume, no network. Requires `volume` and `mountpoint`; readiness must be declared. The schema accepts it, but **the launcher does not wire it up yet** and refuses to start such a tong with a clear message.
- **`none`** — a background side-effect with no anvil-facing surface. Injects nothing. Readiness must be declared.

`<NAME>` is the filename uppercased with hyphens turned into underscores (`github-creds` → `SWARMFORGE_TONG_GITHUB_CREDS_*`).
The MCP server name and the `port` alias are docker network aliases (not container names), so generated config is identical regardless of where the workspace is mounted.

### Extra aliases

A network-facing tong (`mcp` or `port`) may declare additional DNS names it answers to on the session network:

```yaml
interface:
  kind: port
  port: 3000
  aliases: [api, console, local.example.test]
```

Use this when something dialing the tong hardcodes a hostname of its own — a vhost another container expects, or the CN on a TLS certificate a client must match. Each entry must be a valid DNS name (letters, digits, hyphens and dots) and is registered as a further `--network-alias`; the canonical alias is unaffected and stays the name injected into the anvil (`SWARMFORGE_TONG_<NAME>_HOST`, the MCP URL). Extra aliases participate in the same collision check as canonical ones — two tongs on the session network may not claim the same name, whether canonical or extra. `volume` and `none` tongs have no listener and reject the field.

## Readiness

```yaml
readiness:
  mode: healthcheck           # tcp | healthcheck | none
  command: ["test", "-S", "/run/agent.sock"]   # for mode: healthcheck (docker exec)
  timeout: 30s                # 30s / 500ms / 2m, or a bare number of seconds (default 30s)
```

`tcp` is the implicit default for `mcp` and `port`. `volume` and `none` have no port to probe, so `mode` is **required** for them; use `mode: none` to deliberately skip the gate.

## Mounts

Mounts are opt-in **magic words**, never raw host paths. Only three are recognized, spelled `<word>[:/target][:mode]` with the access mode (`ro`/`rw`) last:

- `workspace[:/target][:mode]` — bind-mounts the session workspace, at `/workspace` unless an absolute `target` says otherwise (e.g. `workspace:ro`, `workspace:/code`, `workspace:/code:ro`). A custom target lets an image that expects its sources elsewhere be used unmodified (it does not set the working directory — the process still starts in the image's own `WORKDIR`). A target is refused unless it is an absolute path free of whitespace, and refused if it resolves to `/`, overlaps another of the tong's mounts, or overlaps a path the tong's own wiring occupies — the secret-delivery tmpfs at `/run/swarmforge` and the `/bin/sh` its wrapper execs (for a tong with secret references), the docker socket (for a tong that mounts it), or `/run/swarmforge-tmux` (for a tong that mounts the tmux socket). The workspace bind is paired with the same git-dir mounts the anvil gets from `swarmforge/gitguard.py`: read-only guards over the config and hooks the host's git obeys, and — when the workspace is a linked worktree or another checkout whose git dir lives outside it — that git dir at its own absolute path, which is where the checkout's `.git` pointer file says to look (without it, git inside the tong fails with "not a git repository"). The one mount the anvil gets and a tong does not is the restated worktree registration — a tong keeps the host's copy, so `git worktree list` inside one names the checkout by its host path (the [git guard](../git-guard.md) has the rest). When every `workspace` mount is `ro`, the ride-along git-dir mounts are forced read-only too.
- `docker-socket[:mode]` — bind-mounts the host docker socket onto the same path inside the container (so it takes no target). This is full host docker control and is always called out explicitly in the workspace approval prompt; it is the grant a broker tong needs.
- `tmux-socket[:ro]` — binds the directory holding the launcher's tmux socket at `/run/swarmforge-tmux`, so the tong reaches every tmux server you run in that directory — the one the launcher was started from and any other you started with `-L` — and through any of them can run any command on the host as you. That is as strong a grant as the docker socket, and the approval prompt calls it out the same way. It is always read-only (`tmux-socket:rw` is refused), but read-only only stops the tong deleting or replacing the sockets in that directory: a tmux client needs nothing more than to connect, so it limits nothing the tong can do through a server. It takes no target, and because the directory rather than the socket is bound, a socket tmux recreates on a server restart is still found. It is `session`-only — a `shared` tong is refused, since its container outlives the session and would carry one session's tmux server into the next. The launcher must itself run in a tmux pane, and the socket must sit directly in tmux's default per-user directory (`tmux-<uid>` under `$TMUX_TMPDIR` or `/tmp`); a socket at a custom `-S` path is refused, because binding its directory could expose an arbitrary host directory. The launcher also checks that directory on disk: no symlink on the way to it (docker would bind whatever one points at), owned by you, no group or other permission bits (tmux creates it `0700`), and holding a socket at the path `$TMUX` names. Every directory above it, up to `/`, must be owned by root or you and not writable by group or others unless it has the sticky bit (as `/tmp` does), so no other user can swap the directory between the check and docker's bind. The check assumes the docker daemon resolves the bind on the launcher's own filesystem, as the workspace and docker-socket binds already do; a remote `DOCKER_HOST` is not supported for this mount. Any of these failures stops the launch before anything starts. The tong gets `TMUX`, re-pointed at the socket under `/run/swarmforge-tmux` so a stock `tmux` client reaches the launcher's server with no flags; `TMUX_PANE`, the launcher's pane when it has one, which tmux uses as the default target — a default, not a restriction, since the tong can still name any session, window, or pane; `SWARMFORGE_SESSION_HANDLE`, the anvil's container name; and `SWARMFORGE_WORKSPACE_HOST_PATH`. For the first three the launcher's values replace a plain value the definition's `env:` sets (and one the launcher has no value for is dropped), while a secret reference on any of them is refused at validation, since a delivered secret would override the launcher's value. The socket directory's `0700` mode means the tong's process must run as your host uid or as root to connect.
