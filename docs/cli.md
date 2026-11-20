# swarmforge CLI

`swarmforge`, the command [`install.sh`](../README.md) puts on your `PATH`, checks
setup, creates git repos laid out for worktrees, and opens them in tmux.

- `swarmforge doctor` reports readiness without changing the host; add `--json` for structured output.
- `swarmforge setup` guides installation with confirmation before each action.
- Both accept `--project`, `--checkout`, and repeated `--harness` options; details are in [Setup and doctor](installation.md#guided-setup-and-doctor).

The repository commands:

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

## Opening a repo in tmux

```bash
swarmforge tmux open                      # the repo holding the current directory
swarmforge tmux open ~/code/notes --name notes
swarmforge tmux open --detach ~/code/notes   # set it up, print the session name
```

`tmux open` gives a repo one tmux session with a window per worktree, then switches your terminal to it (`switch-client` inside tmux, `attach-session` outside).
`PATH` is any directory inside the repo and defaults to `.`, so an alias that `cd`s into a repo composes with it: `cdnotes && swarmforge tmux open`.
The session is named for the repo's root directory, so `repo/`, `repo/.git`, and every worktree under it open the same session; `--name` picks another.

The first window, `Repo`, is the root itself; the default branch's worktree comes next, then the rest in the order `git worktree list` gives them.
A window is named for its worktree's path under the root, title-cased: `repo/onboarding-tui` opens as `Onboarding Tui` and `repo/release/1.0` as `Release 1 0`; a name already taken gets a number.
Running it again only adds windows for worktrees that have none, and never types into an open window; each new window starts in its worktree.

## Running a harness from a worktree

The `run_*` harness targets auto-detect the git root from `PROJECT_DIR` and mount it at `/workspace`.
For a linked git worktree they also mount the shared git common directory so git operations keep working inside the container.
So `oc` works from repo roots, subdirectories, and linked worktrees such as `repo/main` with no extra flags, and the other `run_*` targets from the same places with their usual `PROJECT_DIR=$(pwd)`.
Each session sees the one branch you launched it from.
The layout is safe under the [git guard](git-guard.md).
