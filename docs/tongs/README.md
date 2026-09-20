# Tongs (sidecar processes)

A **tong** is a Swarmforge-managed sidecar container started alongside the anvil (the harness container you work in).
The name captures the primary use case: holding something hot — usually credentials — so the agent never touches it directly.
A credential-holding tong runs as a sibling container exposing an **MCP server** the agent calls over the session network; the secret material lives only in the tong's process space.
Tongs can also be plain network services (a throwaway Postgres, a fixture server), volume providers, or background side-effect processes.

Tongs are YAML files discovered across the same four [asset layers](../configuration.md#asset-layers).
The host-side launcher (`swarmforge/anvil/`, run through `bin/run-anvil`) discovers, approves, starts, and tears them down; `make run_opencode` / `make run_claude` already delegate to it.

## Quick start: run a tong

1. (Only if the tong needs secrets) configure the secret-provider table (see [Secret providers](secrets.md)).
2. Drop a tong definition into a layer directory, e.g. `~/.swarmforge/tongs/<name>.yaml` (personal) or `<workspace>/.swarmforge/tongs/<name>.yaml` (project).
3. Run the anvil as usual (`oc`, or `make run_claude PROJECT_DIR=$(pwd)`). A **workspace**-sourced tong prints a privilege summary and asks for approval on first run (see [First-run approval](approval.md)). The launcher resolves secrets (which may prompt your provider CLI to unlock), starts the tong, waits for readiness, injects reachability into the anvil, then runs the anvil in the foreground.
4. On exit (including Ctrl-C), `session` tongs and the per-session network are torn down; `shared` tongs are left running.

## Where definitions live

One YAML file per tong under `.swarmforge/tongs/`, merged **by name** (filename = identity) lowest to highest precedence:

- **user** — `~/.swarmforge/tongs/` (override the root with `SWARMFORGE_USER_ASSETS_DIR`)
- **org** — `$(SWARMFORGE_ORG_CONFIG_ROOT)/.swarmforge/tongs/` (override with `SWARMFORGE_ORG_ASSETS_DIR`)
- **repo** — `tongs/` in the checkout (override with `SWARMFORGE_REPO_TONGS_DIR`, which points directly at the directory)
- **workspace** — `<workspace>/.swarmforge/tongs/`

A higher layer replaces a same-named tong wholesale; `disable: true` switches off an inherited tong.
The user/org/repo layers are **trusted**; the workspace layer (any repo you happened to clone) is gated by first-run approval.

## Lifecycle

- **`session`** — started with the anvil, torn down when it exits. Per-session isolation; the default for credential tongs.
- **`shared`** — long-lived, survives across anvil sessions (ollama-style). Started on first use, connected to each session's network via a network alias, and left running on teardown (no refcounting). A running `shared` container whose config-hash docker label still matches the current definition is reused untouched; a missing, stopped, or stale one is recreated automatically. A rotated secret behind an unchanged reference does **not** churn it — force a restart with `docker rm -f <container>`. A `shared` tong may not mount the `workspace` (it would leak one session's workspace into the next); use a `session` tong for per-workspace mounts.

## Reference

- [Definition format](definitions.md) — every field a tong YAML accepts.
- [Secret providers](secrets.md) — how `${secret:...}` references are resolved and delivered.
- [First-run approval](approval.md) — the gate on workspace-sourced tongs.
- [Broker tongs](broker.md) — holding the docker socket so the anvil never does.
