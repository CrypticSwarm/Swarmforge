"""Build a repository as a bare git dir with its branches checked out beside it.

`clone` and `init` produce the same layout:

    PATH/
      .git/       the repository, bare
      <default>/  a linked worktree of the default branch

Further branches are sibling directories from `git worktree add`, sharing the
objects and refs in `PATH/.git`.

`core.bare` lives in `PATH/.git/config.worktree` rather than `PATH/.git/config`
because the anvil's git guard (`swarmforge.gitguard`) plants a read-only
`commondir` in every git dir it mounts, and git ignores `core.bare` in a git
dir holding one -- a guarded `PATH/.git` would read as a checkout of `PATH/`.
Git reads `config.worktree` after that decision.

`git clone --bare` is not what builds the clone: it records no
`remote.origin.fetch` refspec, so a later `git fetch` updates nothing, and it
copies every remote branch into `refs/heads/*`, taking the names the worktrees
want. `git init --bare` plus `remote add`, `fetch` and `remote set-head`
avoids both.

`init` needs git >= 2.42 for `git worktree add --orphan`.
"""

import os
import shutil
import subprocess

from swarmforge import gitguard


class GitError(Exception):
    """A git command failed; str(exc) names the command and how it exited.

    `returncode` is git's exit status, and None when git could not be run.
    """

    def __init__(self, message, returncode=None):
        super().__init__(message)
        self.returncode = returncode


class LayoutError(ValueError):
    """The destination cannot hold the layout; str(exc) names the path."""


# Variables that name the repository git works on. `-C` does not override them,
# so a build run from a hook or a `git rebase --exec` would write into the
# caller's repository.
REPO_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)


def git(cwd, *args, capture=False):
    """Run git in `cwd`, returning its stdout stripped when `capture` is set.

    Only a captured result comes back on stdout; everything git says reaches
    the terminal on stderr, so a caller's own stdout holds only what it prints.
    stdin is inherited too, for ssh and credential prompts. `REPO_ENVIRONMENT`
    is dropped from the environment git is handed, so `cwd` alone decides which
    repository the command lands in.
    """
    argv = ["git", "-C", cwd] + list(args)
    environment = {key: value for key, value in os.environ.items()
                   if key not in REPO_ENVIRONMENT}
    try:
        # The raw fd, not `sys.stderr`, which a caller may have replaced with a
        # stream that has no fileno.
        completed = subprocess.run(
            argv, text=True, env=environment,
            stdout=subprocess.PIPE if capture else 2)
    except OSError as exc:
        raise GitError("could not run %s: %s"
                       % (" ".join(argv), exc.strerror or exc))
    if completed.returncode != 0:
        raise GitError("%s exited %d" % (" ".join(argv), completed.returncode),
                       completed.returncode)
    return completed.stdout.strip() if capture else None


def guess_clone_dir(url):
    """The directory name git would clone `url` into.

    Git's own rule: drop trailing blanks and slashes, then a trailing `/.git`
    or `.git`, then take what follows the last `/` or `:`. Splitting on the
    colon too is what makes the scp-like `git@host:r.git` give `r`.
    """
    name = url.rstrip(" \t/")
    if name.endswith("/.git"):
        name = name[: -len("/.git")]
    elif name.endswith(".git"):
        name = name[: -len(".git")]
    for separator in ("/", ":"):
        name = name.rsplit(separator, 1)[-1]
    if not name:
        raise LayoutError("cannot work out a directory name from %r" % url)
    return name


def init_bare(git_dir, initial_branch=None):
    """Create `git_dir` as a bare repository.

    Without `initial_branch`, HEAD follows `init.defaultBranch`, which
    `local_default_branch` reads back.
    """
    git_dir = os.path.abspath(git_dir)
    args = ["init", "-q", "--bare"]
    if initial_branch:
        args += ["--initial-branch", initial_branch]
    git(os.path.dirname(git_dir), *(args + [git_dir]))


def fetch_origin(git_dir, url):
    """Point `git_dir` at `url` as origin and fetch it.

    `remote set-head -a` records which remote branch is the default. A remote
    with no branches has none, so that step is allowed to fail and
    `remote_default_branch` is where the caller finds out.
    """
    git(git_dir, "remote", "add", "origin", url)
    git(git_dir, "fetch", "origin")
    try:
        git(git_dir, "remote", "set-head", "origin", "-a")
    except GitError:
        pass


def remote_default_branch(git_dir):
    """The branch name origin calls its default, from `refs/remotes/origin/HEAD`.

    Only the remote-tracking prefix is removed, so `release/1.0` comes back
    whole.
    """
    prefix = "refs/remotes/origin/"
    try:
        ref = git(git_dir, "symbolic-ref", "refs/remotes/origin/HEAD",
                  capture=True)
    except GitError:
        raise GitError("could not determine origin's default branch (it may "
                       "have no branches); pass --branch")
    if not ref.startswith(prefix):
        raise GitError("origin/HEAD does not name a branch: %s" % ref)
    return ref[len(prefix):]


def local_default_branch(git_dir):
    """The branch `git_dir`'s HEAD names, born or not."""
    return git(git_dir, "symbolic-ref", "--short", "HEAD", capture=True)


def set_head(git_dir, branch):
    """Point HEAD at `branch`: the default branch a clone of this repo gets."""
    git(git_dir, "symbolic-ref", "HEAD", "refs/heads/%s" % branch)


def make_bare_by_worktree_config(git_dir):
    """Move `core.bare` into `git_dir`'s worktree config, keeping it bare.

    Running it over a git dir that already carries the config changes nothing.
    """
    for step in gitguard.BARE_REPAIR_STEPS:
        try:
            git(git_dir, *step)
        except GitError as exc:
            # Exit 5 from `config --unset` is git's "key not present".
            if "--unset" not in step or exc.returncode != 5:
                raise


def add_worktree(git_dir, branch, path, start=None, orphan=False):
    """Check `branch` out at `path` as a linked worktree of `git_dir`.

    `orphan` creates the branch unborn. `start` is the commit-ish a new branch
    is created at, and sets up tracking when it is a remote-tracking ref. With
    neither, `branch` is one the repository already has. git resolves a
    relative `path` against its own working directory, not the repository.
    """
    args = ["worktree", "add"]
    if orphan:
        args += ["--orphan", "-b", branch, path]
    elif start is not None:
        args += ["-b", branch, path, start]
    else:
        args += [path, branch]
    git(git_dir, *args)


def _claim(dest):
    """Create `dest`, returning the topmost path `_release` may remove.

    Anything already at `dest` is someone else's -- an empty directory
    included, since `_release` deletes what this created. That is `dest` plus
    any parent `makedirs` brought with it, so a failed `clone URL a/b/c`
    leaves no `a` behind.
    """
    if os.path.exists(dest) or os.path.islink(dest):
        raise LayoutError("%s already exists" % dest)
    claimed = dest
    parent = os.path.dirname(claimed)
    while parent and not os.path.exists(parent):
        claimed = parent
        parent = os.path.dirname(parent)
    try:
        os.makedirs(dest)
    except OSError as exc:
        raise LayoutError("cannot create %s: %s" % (dest, exc.strerror))
    return claimed


def _release(dest, claimed):
    """Undo a `_claim` whose build failed, back up to `claimed`.

    The parents come off one at a time with `os.rmdir`, which stops at the
    first one something else has since filled.
    """
    shutil.rmtree(dest, ignore_errors=True)
    path = dest
    while path != claimed:
        path = os.path.dirname(path)
        try:
            os.rmdir(path)
        except OSError:
            return


def clone(url, dest, branch=None):
    """Build the layout at `dest` from `url`, returning the worktree path.

    `branch` chooses what is checked out and what the bare HEAD names; without
    it both follow the remote's default.
    """
    dest = os.path.abspath(dest)
    claimed = _claim(dest)
    try:
        git_dir = os.path.join(dest, ".git")
        init_bare(git_dir)
        fetch_origin(git_dir, url)
        default = branch or remote_default_branch(git_dir)
        set_head(git_dir, default)
        make_bare_by_worktree_config(git_dir)
        path = os.path.join(dest, default)
        add_worktree(git_dir, default, path, start="origin/%s" % default)
        return path
    except BaseException:
        _release(dest, claimed)
        raise


def init(dest, branch=None):
    """Build an empty layout at `dest`, returning the worktree path.

    The default branch is `branch`, or whatever `init.defaultBranch` gives the
    new repository; it is unborn either way, so the worktree starts empty.
    """
    dest = os.path.abspath(dest)
    claimed = _claim(dest)
    try:
        git_dir = os.path.join(dest, ".git")
        init_bare(git_dir, initial_branch=branch)
        default = branch or local_default_branch(git_dir)
        make_bare_by_worktree_config(git_dir)
        path = os.path.join(dest, default)
        add_worktree(git_dir, default, path, orphan=True)
        return path
    except BaseException:
        _release(dest, claimed)
        raise
