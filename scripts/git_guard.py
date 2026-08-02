#!/usr/bin/env python3
"""Docker mounts that keep the anvil out of a repo's git configuration.

Usage: git_guard.py --workspace DIR [--target CONTAINER_PATH]...

Prints one docker `-v` value per line (the caller supplies the `-v` itself), for
the workspace mounted at each `--target`. Nothing is printed when the workspace
is not a git repo.

The workspace is mounted read-write, but a few paths inside a git dir are not
project files -- they are instructions the *host's* git obeys later, outside the
container:

  * `config` -- aliases, `core.hooksPath`, `core.pager`, `core.sshCommand`,
    `core.fsmonitor`; several of these run a command on almost any git
    invocation.
  * `hooks/` -- scripts git runs on the user's next commit, checkout or push.
  * `commondir` -- one line naming where `config` and `hooks/` actually live.
    Git reads it in every repository, not just worktrees, so leaving it writable
    would let the container point git at a directory of its own and hand back
    the two paths above.
  * `config.worktree` -- a second config file, read when `extensions.worktreeConfig`
    is on. The container cannot turn the extension on (config is read-only), but
    `git sparse-checkout` turns it on for its own reasons, so repos that have it
    enabled are not unusual.

Each of those is bind-mounted read-only over the read-write workspace, for the
git dir itself and for every git dir reachable from it: the git dirs of
initialized submodules (`modules/`, recursively) and of linked worktrees
(`worktrees/`). A `.git` that is a pointer file rather than a directory -- a
linked worktree, a submodule checkout, `--separate-git-dir` -- is mounted
read-only too, so the container cannot repoint itself at a git dir none of these
mounts cover.

Each git dir is also bind-mounted onto itself, which makes it a mount point.
Without that the read-only mounts are trivially sidestepped: renaming the git
dir aside and copying it back reproduces the repo with a writable config, and
the copy is what the host reads afterwards. Renaming a mount point fails.

A read-only mount needs something on the host to cover, so a missing `hooks/`
or `commondir` is created first rather than left as a hole. Both are inert: an
empty hooks directory is what `git init` produces, and a `commondir` of `.`
resolves to the git dir that contains it, which is where git looks anyway.

Everything else in a git dir stays writable -- objects, refs, index, logs -- so
commits, branches and fetches work as usual. Writes that do land in config
(`git config --local`, `git remote add`, `git push -u`, and the branch tracking
that `git switch <remote-branch>` records) fail inside the container by design.

What this cannot reach: hooks that config already points *outside* the git dir
(`core.hooksPath = .githooks`, husky's `.husky/`), and attribute-driven filter
and diff commands, which a tracked `.gitattributes` can invoke just as well as
`.git/info/attributes`. Both live in the read-write workspace, which is not a
trust boundary -- an agent that can edit `package.json`, `Makefile` or
`.pre-commit-config.yaml` can already run code on the host by other means. The
point of these mounts is to close the paths that need no such cooperation.
"""

import os
import subprocess
import sys

# Files made read-only in a git dir that holds a repository's config and hooks:
# the workspace's own git dir, a submodule's, a linked worktree's shared common
# dir. The value is what to write when the file is absent, or None to guard it
# only where it already exists -- a git dir without a `config` is not a git dir
# worth inventing one for.
REPOSITORY_FILES = {"config": None, "commondir": ".\n"}
REPOSITORY_DIRS = ("hooks",)

# A per-worktree git dir under `worktrees/` has no config or hooks of its own --
# both come from the common dir -- but it carries the `commondir` pointer that
# says which common dir that is. Never written: unlike a repository's, its
# contents are a real relative path, and a worktree missing one is already
# broken.
WORKTREE_FILES = {"commondir": None}

# Guarded in both kinds of git dir, but only where `extensions.worktreeConfig`
# is on, since that is the only case git reads it. An empty one is inert.
WORKTREE_CONFIG = {"config.worktree": ""}


class UsageError(Exception):
    """The command line could not be parsed."""


def parse_args(argv):
    """Return (workspace, targets) from `--workspace DIR --target PATH...`."""
    workspace = None
    targets = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--workspace":
            if index + 1 >= len(argv):
                raise UsageError("--workspace requires a path argument")
            workspace = argv[index + 1]
            index += 2
            continue
        if token == "--target":
            if index + 1 >= len(argv):
                raise UsageError("--target requires a path argument")
            targets.append(argv[index + 1].rstrip("/") or "/")
            index += 2
            continue
        raise UsageError("unrecognized argument: %s" % token)
    if not workspace:
        raise UsageError("--workspace is required")
    if not targets:
        raise UsageError("at least one --target is required")
    return workspace, targets


def git_output(workspace, *args):
    """Stdout of a git command in `workspace`, or None if it failed."""
    try:
        completed = subprocess.run(
            ["git", "-C", workspace] + list(args),
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def common_git_dir(workspace):
    """The git dir holding `workspace`'s config and hooks, or None.

    Resolved through the filesystem so it can be compared against the workspace
    and its `.git` without a symlink in the path changing the answer.
    """
    found = git_output(
        workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not found:
        return None
    return os.path.realpath(found)


def worktree_config_enabled(workspace):
    """True if git reads `config.worktree` files in this repo."""
    return git_output(
        workspace, "config", "--bool", "extensions.worktreeConfig") == "true"


def submodule_git_dirs(git_dir):
    """(path, relative path) of every initialized submodule git dir below.

    Submodule git dirs live under `modules/`, keyed by submodule name -- which
    may itself contain slashes, so the intermediate directories are walked
    rather than assumed to be one level deep. A submodule can have submodules,
    so each hit is descended into as well.
    """
    found = []
    pending = [os.path.join(git_dir, "modules")]
    while pending:
        parent = pending.pop()
        try:
            entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.isfile(os.path.join(entry.path, "HEAD")):
                found.append((entry.path, os.path.relpath(entry.path, git_dir)))
                pending.append(os.path.join(entry.path, "modules"))
            else:
                pending.append(entry.path)
    return found


def worktree_git_dirs(git_dir):
    """(path, relative path) of every linked worktree's git dir below."""
    parent = os.path.join(git_dir, "worktrees")
    try:
        entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
    except OSError:
        return []
    return [
        (entry.path, os.path.relpath(entry.path, git_dir))
        for entry in entries if entry.is_dir(follow_symlinks=False)
    ]


def submodule_checkout(git_dir):
    """The checkout a submodule git dir belongs to, or None.

    `core.worktree` is written relative to the git dir when git creates the
    submodule, so it is resolved against it.
    """
    worktree = git_output(git_dir, "config", "--file",
                          os.path.join(git_dir, "config"), "core.worktree")
    if not worktree:
        return None
    return os.path.realpath(os.path.join(git_dir, worktree))


def ensure_present(path, contents, warn):
    """Create `path` if absent so a read-only mount has something to cover.

    `contents` of None means a directory. Returns whether the path is now there;
    a repo we cannot write to is reported rather than silently left unguarded.
    """
    if os.path.exists(path):
        return True
    try:
        if contents is None:
            os.mkdir(path)
        else:
            with open(path, "x") as handle:
                handle.write(contents)
    except OSError as exc:
        warn("could not create %s (%s); leaving it writable in the container"
             % (path, exc.strerror or exc))
        return False
    return True


def read_only(host, container):
    return "%s:%s:ro" % (host, container)


def git_dir_mounts(host_dir, container_dir, *, files, dirs=(), anchor=True,
                   worktree_config=False, warn=None):
    """Mounts guarding one git dir: the anchor, then the read-only paths."""
    specs = []
    if anchor:
        specs.append("%s:%s" % (host_dir, container_dir))
    wanted = dict(files)
    if worktree_config:
        wanted.update(WORKTREE_CONFIG)
    for name, contents in wanted.items():
        path = os.path.join(host_dir, name)
        if contents is None:
            if not os.path.isfile(path):
                continue
        elif not ensure_present(path, contents, warn):
            continue
        specs.append(read_only(path, os.path.join(container_dir, name)))
    for name in dirs:
        path = os.path.join(host_dir, name)
        if not ensure_present(path, None, warn):
            continue
        specs.append(read_only(path, os.path.join(container_dir, name)))
    return specs


def repository_mounts(host_dir, container_dir, *, anchor, worktree_config, warn):
    """Guard a git dir plus the submodule and worktree git dirs it contains.

    A nested git dir keeps its path relative to the git dir it was found in, so
    it lands under the same container path without a second lookup.
    """
    specs = git_dir_mounts(
        host_dir, container_dir, files=REPOSITORY_FILES, dirs=REPOSITORY_DIRS,
        anchor=anchor, worktree_config=worktree_config, warn=warn)
    for path, relative in submodule_git_dirs(host_dir):
        specs += git_dir_mounts(
            path, os.path.join(container_dir, relative),
            files=REPOSITORY_FILES, dirs=REPOSITORY_DIRS, anchor=False,
            worktree_config=worktree_config, warn=warn)
    for path, relative in worktree_git_dirs(host_dir):
        specs += git_dir_mounts(
            path, os.path.join(container_dir, relative), files=WORKTREE_FILES,
            anchor=False, worktree_config=worktree_config, warn=warn)
    return specs


def pointer_mounts(host_pointer, workspace, targets):
    """Read-only mounts for a `.git` that is a `gitdir:` pointer file.

    Emitted for every container path the pointer is visible from. A pointer
    outside the workspace is not mounted: it is not the container's to rewrite.
    """
    if not os.path.isfile(host_pointer):
        return []
    relative = os.path.relpath(host_pointer, workspace)
    if relative.startswith(os.pardir + os.sep) or relative == os.pardir:
        return []
    return [
        read_only(host_pointer, os.path.join(target, relative))
        for target in targets
    ]


def build_mounts(workspace, targets, warn=None):
    """Every mount that guards `workspace`'s git configuration, in order."""
    warn = warn or (lambda message: None)
    common = common_git_dir(workspace)
    if common is None:
        return []
    workspace_real = os.path.realpath(workspace)
    workspace_git = os.path.join(workspace, ".git")
    worktree_config = worktree_config_enabled(workspace)

    specs = []
    if common == workspace_real:
        # A bare repo: the workspace mount is the git dir, and is already a
        # mount point, so it needs the read-only paths but no anchor.
        for target in targets:
            specs += repository_mounts(
                common, target, anchor=False,
                worktree_config=worktree_config, warn=warn)
    elif common == os.path.realpath(workspace_git):
        for target in targets:
            specs += repository_mounts(
                workspace_git, os.path.join(target, ".git"), anchor=True,
                worktree_config=worktree_config, warn=warn)
    else:
        # A linked worktree or --separate-git-dir: the git dir lives outside
        # the workspace, so it is mounted at its own host path -- which is
        # where the pointer file names it.
        warn("Detected git dir outside the workspace; mounting it: %s" % common)
        specs += repository_mounts(
            common, common, anchor=True,
            worktree_config=worktree_config, warn=warn)
        specs += pointer_mounts(workspace_git, workspace, targets)

    # A submodule's checkout carries its own pointer file inside the workspace.
    # Rewriting it would send the host's git to a git dir nothing above covers.
    for path, _ in submodule_git_dirs(common):
        checkout = submodule_checkout(path)
        if checkout:
            specs += pointer_mounts(
                os.path.join(checkout, ".git"), workspace_real, targets)
    return specs


def main(argv, out=sys.stdout, err=sys.stderr):
    try:
        workspace, targets = parse_args(argv)
    except UsageError as exc:
        err.write("git_guard.py: %s\n" % exc)
        return 2

    def warn(message):
        err.write("%s\n" % message)

    for spec in build_mounts(workspace, targets, warn=warn):
        out.write("%s\n" % spec)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
