# Broker tongs

A **broker** is a tong that holds the docker socket and spawns its own short-lived worker containers on demand, so the anvil can compile, run tests, or do other sandboxed work without ever getting socket access itself.

`tongs/docker-broker/` ships a reference broker: an HTTP MCP server whose verbs are defined by a **declarative config**, not hand-written per project. Each command in `broker.config.yaml` describes the worker container to spawn — reusing the tong definition shape (`image`, `mounts`, `command`, `env`, `resources`, `networks`) — with an MCP surface (`name`, `description`, typed `params`) on top:

```yaml
allowed_images:
  - node:24-alpine            # the entire image allowlist; nothing else can run
commands:
  - name: test                # the MCP tool the agent calls
    description: Run the project's test suite.
    image: node:24-alpine
    mounts: [workspace:/work:ro]
    workdir: /work
    command: [npm, test, --]
    params:
      - name: suite            # exposed as a constrained MCP input
        type: enum             # boolean | enum
        values: [unit, integration, e2e]
        append_value: true     # the chosen value is appended as one command token
```

The config **is** the broker's allowlist. There is no verb that runs an arbitrary image or mounts an arbitrary host path: a worker may only mount the session `workspace`, and a parameter can only toggle a fixed effect (`boolean`) or pick a value from a fixed set (`enum`) — values are passed as whole argv words to a worker spawned without a shell, so nothing a caller sends can become a flag, path, or shell metacharacter. The launcher hands the broker the workspace's host path as `SWARMFORGE_WORKSPACE_HOST_PATH` so it can mount the workspace into the workers it spawns.

To enable it:

1. `make build_broker` — builds the `swarmforge-docker-broker` image.
2. Copy the example definition into a layer: `cp tongs/docker-broker/docker-broker.tong.yaml ~/.swarmforge/tongs/docker-broker.yaml`.

The example definition is **not** auto-discovered from the checkout (it lives a directory below the layer root, and discovery reads only top-level `*.yaml`), so the broker stays off until you opt in. Because it requests the docker socket, a workspace-sourced copy is always called out in the approval prompt.

[`tongs/docker-broker/README.md`](../../tongs/docker-broker/README.md) documents the config format and the image, beside the `broker.config.yaml` it describes.
