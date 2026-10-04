# First-run approval

The user, org, and repo layers are installed deliberately and are trusted; they skip the gate.
A **workspace**-sourced tong (from a repo you cloned) could otherwise request your secrets, host mounts, the docker socket, or the host's GPUs simply by being present, so the launcher gates it:

- Before starting, it prints exactly what the tong requests — image, secret references, mounts, networks, docker-socket access, and GPU access — and asks you to approve. Control and formatting characters in those values are shown as escapes such as `\x1b` or `\n`, so a value cannot move the cursor, restyle, or hide any part of the summary. A `volume:` mount is also listed by name on a `volumes:` line, which warns that its data outlives the tong and that any data already in it, left by an earlier definition or an earlier checkout at this path, is reused.
- Approval is keyed by workspace path + tong name + a hash of the merged definition, stored in `~/.swarmforge/approvals.json`. Any change to the definition re-prompts.
- The gate defaults to **No**, and a non-interactive stdin reads as No. A scripted `--no-prompt` run **fails closed** rather than auto-approving.
- Approving `image: foo:latest` approves a moving target; **pinned digests are the recommended convention** for workspace tongs.
