# Secret providers

Secret references are resolved **on the host** by shelling out to a provider CLI — Swarmforge knows nothing about any individual secret manager.
Declare your providers once in the user layer at `~/.swarmforge/secret-providers.yaml` (override the root with `SWARMFORGE_USER_ASSETS_DIR`):

```yaml
# ~/.swarmforge/secret-providers.yaml
providers:
  op:      ["op", "read", "{ref}"]
  pass:    ["pass", "show", "{ref}"]
  doppler: ["doppler", "secrets", "get", "{ref}", "--plain"]
  aws:     ["aws", "secretsmanager", "get-secret-value", "--secret-id", "{ref}",
            "--query", "SecretString", "--output", "text"]
```

Each value is an argv template; the literal token `{ref}` in any element is replaced with the reference. Command templates must be single-line flow lists.
A missing file means no providers are configured, so any secret reference fails loudly rather than resolving to an empty value.

Reference a secret from a tong's `env:` as `${secret:<provider>:<ref>}`, for example `${secret:op:op://Work/github/token}`.
Because the launcher runs in your terminal before the anvil starts, interactive unlocks (`op signin`, biometric prompts) work for free.

**Per-secret overrides.** A provider value may instead be a structured entry with a `default` command and per-secret `overrides`, so a *shared* tong (say in the org layer) can reference `${secret:<provider>:<ref>}` while each developer's personal table decides how each individual secret is fetched. One developer resolves a ref through `pass`, another through `1Password`, without touching the shared tong:

```yaml
# ~/.swarmforge/secret-providers.yaml
providers:
  shared:
    default: ["pass", "show", "{ref}"]        # used for any ref not overridden
    overrides:
      ci-token: ["doppler", "secrets", "get", "CI_TOKEN", "--plain"]
```

Resolving `${secret:shared:<ref>}` uses the argv in `overrides` for that ref, falling back to `default`. A ref with neither stops the launch with a clear message. `default` is optional (use `overrides` alone to require every ref be listed), and `{ref}` substitution still applies to whichever command is chosen. Because `default` and `overrides` are separate keys, a secret literally named `default` is just an entry under `overrides` — distinct from the fallback — and any other provider-level key is flagged as a typo at load.

**Delivery is leak-resistant by design.** A resolved secret is never passed as a docker `-e` value, a command-line argument, or a file on disk (anything holding the docker socket could read those back). Instead the launcher wraps the tong's entrypoint with a `/bin/sh` prologue that creates a FIFO on a tmpfs inside the container, reads it, exports the values, then execs the image's real entrypoint — so an unmodified off-the-shelf server that reads its credentials from `process.env` works as-is. The launcher streams the secret env into that FIFO over `docker exec` stdin, which docker carries on its API stream; because nothing crosses the host filesystem, delivery behaves the same on native Linux and under Docker Desktop's VM (macOS/Windows). A tong with secret env therefore needs `/bin/sh`, `mkfifo`, `cat`, and `rm` in its image; a tong without secrets runs its image entrypoint unchanged. Plain (non-secret) `env:` values still flow through `-e`.
