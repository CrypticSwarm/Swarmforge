# swarmforge CLI

`install.sh` links `bin/swarmforge` into `~/.local/bin`, which gives you a `swarmforge` command that creates git repos laid out for worktrees.

```bash
swarmforge clone git@github.com:owner/repo.git             # -> ./repo
swarmforge clone https://host/owner/repo.git checkouts/repo
swarmforge clone git@github.com:owner/repo.git --branch release
swarmforge init newproject --branch main
```

`clone` fetches a remote and checks out its default branch; `init` starts an empty repo whose default branch has no commits yet.
Both print the worktree they created and nothing else on stdout, so `cd "$(swarmforge clone URL)"` works, and both refuse a `PATH` that already exists.
`PATH` defaults to the name git would pick for the URL, so a one-argument `swarmforge clone` lands in the same directory `git clone` would.
`--branch` names the branch to check out instead of the remote's default, and for `init` the name to use instead of `init.defaultBranch`.

The result is the repository with its branches checked out beside it:

```
repo/
  .git/    the repository, shared by every worktree
  main/    a checkout of main
```

`repo/main` is an ordinary working copy: it tracks `origin/main`, and `git fetch`, `git pull`, and `git push` work there as they do in a clone.
Add a branch from inside it with `git worktree add -b feature ../feature`, which puts the checkout at `repo/feature`; a slashed name nests, so `release/1.0` lands at `repo/release/1.0`.
Removing one is `git worktree remove ../feature`.

## Running a harness from a worktree

The `run_*` harness targets auto-detect the git root from `PROJECT_DIR` and mount it at `/workspace`.
For a linked git worktree they also mount the shared git common directory so git operations keep working inside the container.
This means `oc` works from repo roots, subdirectories, and linked worktrees without extra flags.

Run the harnesses from `repo/main`, or any other branch directory, as you would from a clone: `oc`, `make run_claude PROJECT_DIR=$(pwd)`, and the other `run_*` targets need nothing extra.
Each session sees the one branch you launched it from.
The layout is safe under the git guard: both subcommands configure the repo for it at creation (see [Git repos and worktrees](git-guard.md)).
