# Git repos and worktrees

`.git/config` and `.git/hooks` are mounted read-only wherever the git dir is visible in the container.
Both execute on the *host* — hooks run on your next commit or checkout, and config carries `core.hooksPath`, `core.pager`, `core.sshCommand` and aliases — so the agent gets no write access to them.
`swarmforge/gitguard.py` builds those mounts, covering every git dir reachable from the workspace: the repo's own, a linked worktree's shared common dir, and the git dirs of initialized submodules — each with their own submodules and worktrees, including a submodule initialized inside a worktree, whose git dir git keeps under that worktree rather than the repository.
`remotes/` and `branches/`, the pre-config way to define a remote, are read-only for the same reason as `config`.
It also guards the pointers that say where config and hooks live (`commondir`, a `.git` that is a `gitdir:` file, and `config.worktree` where `extensions.worktreeConfig` is on), and binds every directory on the way down onto itself, since a plain directory containing a read-only mount can still be renamed aside and recreated writable.
A guarded path that is absent is created on the host first so there is no gap to slip through — a repo with no `config` works fine, which makes its absence room to write one rather than a sign there is nothing to guard.
The placeholders are inert, though a repo that gains a `commondir` starts answering `git rev-parse --git-common-dir` with an absolute path instead of `.git`.
In a bare git dir the `commondir` placeholder is not inert: git takes any git dir holding one for a linked worktree's and ignores `core.bare`, so a bare `repo/.git` with sibling worktrees reads as a checkout of `repo/`, where `git status` lists the worktrees as untracked and `git clean` would remove them.
The guard warns and prints the fix, which moves `core.bare` into `config.worktree` under `extensions.worktreeConfig`; git reads that file after deciding, so the repo stays bare and its worktrees stay checkouts.
`swarmforge clone` and `swarmforge init` apply it at creation, so a layout they build stays bare under the guard.
Only git dirs that exist when the session starts are covered — a repo the agent clones or `git init`s inside the workspace, or an unrelated checkout vendored there, is not.
A `.git` written into an existing subdirectory shadows the guarded repo for anything run from inside that directory: `git status` at the root neither reports it nor executes it, and a git-aware shell prompt or editor entering the directory is enough to run what its config says. `safe.directory`, git's gate for this, keys on ownership, and the container runs as your own uid.

When the workspace is a linked worktree, one mount goes the other way.
The worktree's git dir records the checkout it belongs to in `gitdir`, by the path that checkout has on the host — and the container gets the git dirs at their host paths but never a checkout there.
Git still finds the worktree root by walking up from the working directory, so `git rev-parse --show-toplevel` is unaffected; what goes wrong is everything that reads the record.
`git worktree list` names that worktree by its host path and calls it `prunable`, and a harness that resolves its project root from the record — folder trust, in particular, is usually keyed on it — answers with a directory the container does not have.
A read-only mount over the record spells the checkout the way the session reaches it, `/workspace` or `/repos/<slug>`, so those answers match the directory the agent starts in.
The host's record is neither written nor moved: `git worktree list` and `git worktree prune` read it on the host, where a container path reads as a worktree that has gone away.
Only the workspace's own record is restated; the repository's other linked worktrees still read `prunable` inside the container, and a `git worktree prune` there — including one an automatic `gc` reaches — unlinks their registration files on the host until the read-only mounts stop it partway.

The rest of the git dir stays writable, so committing, branching, fetching, and `git worktree add` work as usual.
Commands that write config do not, by design: `git config --local`, `git remote add`, `git submodule update --init`, and `git sparse-checkout` fail with `could not write config file ...: Device or resource busy`, and hook installers like `pre-commit install` or husky fail on the read-only `.git/hooks`.
`git worktree repair` and `git worktree move` fail the same way on the read-only registration above, which `status`, `commit` and `worktree list` never write.
Branch tracking is the sharp edge: `git push -u` and `git switch <remote-branch>` exit 0 and still report "set up to track", but the tracking config is silently not recorded — git treats that write failing as non-fatal. Use `git push origin HEAD:<branch>` and `git switch -c <name> --no-track origin/<branch>`, and set a repo up on the host when it needs to stick.
This narrows the git-specific surface; it does not make the workspace a trust boundary. Hooks that config already points *outside* the git dir (`core.hooksPath = .githooks`, husky) and attribute-driven filter commands live in the workspace, as do `package.json` scripts and `Makefile`s — anything you run on the host from a directory an agent could write is still yours to trust.
