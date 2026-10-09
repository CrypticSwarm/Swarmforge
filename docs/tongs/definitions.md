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
# - volume:cache:/var/cache   # a persistent named volume, kept between containers
resources:
  memory: 512m                # string or number
  # gpus: all                 # optional host GPU access (see Resources)
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
- **`volume`** — a shared named volume, no network. Requires `volume` and `mountpoint`; readiness must be declared. The schema accepts it, but **the launcher does not wire it up yet** and refuses to start such a tong with a clear message. (A tong that only needs to keep its own state uses a `volume:` mount, described under Mounts below.)
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

Mounts are opt-in **magic words**, never raw host paths. Only three are recognized, each spelled as shown below with the optional access mode (`ro`/`rw`) last:

- `workspace[:/target][:mode]` — bind-mounts the session workspace, at `/workspace` unless an absolute `target` says otherwise (e.g. `workspace:ro`, `workspace:/code`, `workspace:/code:ro`). A custom target lets an image that expects its sources elsewhere be used unmodified (it does not set the working directory — the process still starts in the image's own `WORKDIR`). A target is refused unless it is an absolute path free of whitespace, and refused if it resolves to `/`, overlaps another of the tong's mounts, or overlaps a path the tong's own wiring occupies — the secret-delivery tmpfs at `/run/swarmforge` and the `/bin/sh` its wrapper execs (for a tong with secret references), or the docker socket (for a tong that mounts it). The workspace bind is paired with the same git-dir mounts the anvil gets from `swarmforge/gitguard.py`: read-only guards over the config and hooks the host's git obeys, and — when the workspace is a linked worktree or another checkout whose git dir lives outside it — that git dir at its own absolute path, which is where the checkout's `.git` pointer file says to look (without it, git inside the tong fails with "not a git repository"). The one mount the anvil gets and a tong does not is the restated worktree registration — a tong keeps the host's copy, so `git worktree list` inside one names the checkout by its host path (the [git guard](../git-guard.md) has the rest). When every `workspace` mount is `ro`, the ride-along git-dir mounts are forced read-only too.
- `docker-socket[:mode]` — bind-mounts the host docker socket onto the same path inside the container (so it takes no target). This is full host docker control and is always called out explicitly in the workspace approval prompt; it is the grant a broker tong needs.
- `volume:<name>:/target[:mode]` — mounts a docker **named volume** at `target`, for state the tong keeps between containers (a model store, a cache). The name comes first and is at most 64 letters, digits, `.` and `-`, starting with a letter or digit; the target is required and held to the same rules as a `workspace` target, may not overlap the git dir that a `workspace` mount brings along, and a tong mounts each volume only once. Docker creates the volume on first use and seeds a new, empty one with whatever the image has at `target`. Only that first seeding happens: a later image's files at `target` never replace what the volume holds, so point `target` at a data directory, not at files the image ships. The launcher never removes a volume — not on teardown, and not when a `shared` tong is recreated because its definition changed — so the state outlives every container that mounts it. Remove one by hand with `docker volume rm <volume>` (`docker volume ls --filter name=swarmforge-volume-` lists them). A `volume:` mount is private to its tong; it is unrelated to `interface.kind: volume`, which would share a volume with the anvil and is not wired up yet.

### Volume naming and scope

The docker volume is named `swarmforge-volume-[<hint>-]<digest>_<name>`. The hint is the tong's filename reduced to the characters docker allows and cut to 32 of them, and is there only to be read in `docker volume ls`. The digest is the volume's identity: the first 32 hex digits of a SHA-256 over the tong's exact filename and the scope it came from. A volume name never contains `_` and the digest has a fixed width, so the last `_` always separates the volume name. The scope depends on the tong's layer:

- **org** — the org's tongs directory. This applies to both lifecycles.
- **workspace** — the checkout's top-level path, with symlinks resolved. The path is the key: every session started anywhere in that checkout uses the volume, and a clone or worktree at a different path gets its own. A later checkout at the same path inherits it, even if it is a different repository, just as it inherits the approvals keyed by that path. Deleting a checkout leaves its volumes behind; remove them with `docker volume rm`. A workspace tong that mounts a volume may not be `shared`: its container would be reused by sessions in other checkouts, still mounting this checkout's volume.
- **user** and **repo** — this machine, across every checkout. A repo tong and the same-named user tong it replaces use one volume. Anything such a tong copies from one workspace into its volume is visible to its sessions in every other checkout.

Two tongs therefore share a volume only when they have the same filename and the same scope. A tong never gets the volume of another org, a checkout at another path, or a differently named tong, and at 128 bits a definition cannot search its way onto another scope's name. Within its scope a volume belongs to the tong's name, not to one version of its definition or to one session: an edited definition, or a later checkout at the same path, reuses whatever data is already there, and concurrent sessions of a `session` tong all mount it read-write at once. Keep state there that tolerates that, or that the program locks itself; a second database server on one data directory, for instance, fails to start.

## Resources

```yaml
resources:
  memory: 8g                  # docker --memory: a string or number
  gpus: all                   # docker --gpus: all, a count, or devices
```

Both are optional and passed to `docker run` as written. `gpus` grants the tong the host's GPUs and accepts exactly these forms:

- `all` — every GPU on the host.
- A positive count, e.g. `2` (at most 2147483647).
- `device=<id>` for one device, or `"device=<id>,<id>,..."` — double quotes included — for several, where an id is an index or a `GPU-`/`MIG-` UUID: a letter or digit, then letters, digits, and `._:-`. Docker reads the `--gpus` value as CSV, so a list needs those literal double quotes inside the value, which in YAML means wrapping it in single quotes: `gpus: '"device=0,1"'`. An unquoted `device=0,1` is refused with that hint.

Docker's other `--gpus` keys are deliberately refused. `driver=`, `capabilities=`, and `options=` can grant more than GPU access — `driver=cdi`, for one, injects any device registered on the host — and `count=` is redundant with a plain count.
GPU access needs a docker host set up for GPU containers — the NVIDIA Container Toolkit on Linux; Docker Desktop's WSL 2 backend provides GPU support itself. Without it `docker run` fails and the launch stops.
A workspace tong's GPU request is called out in its [approval prompt](approval.md).

## Example: a GPU model server

A `shared` `port` tong that serves models from the host's GPUs and keeps its multi-GB model store in a volume, so a recreated container does not download it again:

```yaml
# ~/.swarmforge/tongs/models.yaml
lifecycle: shared
image: ollama/ollama@sha256:...
interface:
  kind: port
  port: 11434
mounts:
  - volume:store:/root/.ollama
resources:
  gpus: all
```

The anvil gets `SWARMFORGE_TONG_MODELS_HOST=models` and `SWARMFORGE_TONG_MODELS_PORT=11434`, and the store lives in the volume `swarmforge-volume-models-<digest>_store`. The tong is named `models` rather than `ollama` because `make run_ollama` already answers to `ollama` on the same network; a tong of that name would clash with it.
