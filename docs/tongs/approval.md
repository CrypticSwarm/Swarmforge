# First-run approval

The user, org, and repo layers are installed deliberately and are trusted; they skip the gate.
A **workspace**-sourced tong (from a repo you cloned) could otherwise request your secrets, host mounts, the docker socket, or your tmux server simply by being present, so the launcher gates it:

- Before starting, it prints exactly what the tong requests — image, secret references, mounts, networks, docker-socket access, and tmux-socket access — and asks you to approve. Both sockets get a line of their own, since each lets the tong run anything on your host; the tmux line says that this covers every tmux server in your socket directory, and that the mount being read-only does not limit it.
- Approval is keyed by workspace path + tong name + a hash of the merged definition, stored in `~/.swarmforge/approvals.json`. Any change to the definition re-prompts.
- The gate defaults to **No**, and a non-interactive stdin reads as No. A scripted `--no-prompt` run **fails closed** rather than auto-approving.
- Approving `image: foo:latest` approves a moving target; **pinned digests are the recommended convention** for workspace tongs.
