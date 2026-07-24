# docker-broker

A reference **broker tong**: an HTTP MCP server that exposes a *configured* set of
narrow docker-task verbs. It holds the docker socket and spawns short-lived worker
containers on demand, so the anvil can compile and test without socket access of
its own.

The verbs are not hand-written per project — they come from a declarative config
(`broker.config.yaml`, baked into the image at `/etc/swarmforge/broker.config.yaml`).
Each command describes the worker container to spawn, reusing the Swarmforge tong
definition shape, with an MCP surface on top.

## Layout

- `broker.config.yaml` — the baked reference command set. Replace it with the
  verbs your project needs (or mount your own at a trusted path and point
  `BROKER_CONFIG` at it).
- `src/config.ts` — parses and validates the command config, fail-closed.
- `src/commands.ts` — turns a validated command + caller params into a `docker run`
  argv and runs the worker. This is the security boundary.
- `src/server.ts` / `src/index.ts` — the MCP tool registration and the HTTP server.
- `docker-broker.tong.yaml` — example tong definition wiring this image into a
  Swarmforge layer (not auto-discovered; copy it into a layer to enable).

## Config model

```yaml
name: docker-broker
allowed_images:                 # the entire image allowlist
  - node:24-alpine
commands:
  - name: build                 # MCP tool name
    description: ...             # shown to the agent
    image: node:24-alpine       # must be in allowed_images
    mounts: [workspace:/work]   # only the `workspace` magic word (never the socket)
    workdir: /work
    command: [npm, run, build]  # base argv inside the container
    env: { CI: "1" }
    resources: { memory: 1g }
    params:
      - name: production        # exposed as an MCP input
        type: boolean           # toggles a fixed effect when true
        when_true:
          env: { NODE_ENV: production }
      - name: target
        type: enum              # picks a value from a fixed set
        values: [app, lib]
        env_var: TARGET         # or `append_value: true` to append it as a token
```

Safety rules enforced at load time (the server refuses to start otherwise):

- Every command image must be listed in `allowed_images`; there is no
  arbitrary-image verb.
- A worker may only mount `workspace[:<target>][:<mode>]` — never the docker
  socket and never a raw host path.
- Parameters are limited to `boolean` (apply a fixed `append_command`/`env`
  effect — a boolean param must declare a non-empty `when_true`, so it can never
  be a silent no-op) and `enum` (insert a value drawn from a fixed `values` set
  via `append_value`/`env_var`). Values reach the worker as whole argv words; the
  worker is spawned without a shell.

Notes:

- There is deliberately **no free-form `string` parameter** in v1: an
  unconstrained value is exactly the injection surface the broker exists to
  avoid. Enumerate the values you want to allow with an `enum` param instead.
- The `workspace:<target>` mount form (a custom mountpoint) is **broker-config
  only** — it describes where a *worker* sees the workspace. A Swarmforge tong
  definition's own `mounts:` accepts only `workspace[:mode]` and always mounts at
  `/workspace`.

## Develop

```sh
npm install
npm test        # tsc + node:test over the config + argv-builder suites
npm run build   # emit dist/
```

## Build the image

From the repo root: `make build_broker` (tags `swarmforge-docker-broker:latest`).
