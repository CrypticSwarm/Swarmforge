#!/usr/bin/env python3
"""Docker mounts that keep the anvil out of a repo's git configuration.

Usage: git-guard --workspace DIR [--target CONTAINER_PATH]...

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
    is on for that repo. The container cannot turn the extension on (config is
    read-only), but `git sparse-checkout` turns it on for its own reasons, so
    repos that have it enabled are not unusual.
  * `remotes/` and `branches/` -- the pre-config way to define a remote, still
    read when config defines no remote of that name, so a planted file there
    decides where a `git fetch <name>` goes and what it runs to get there.

Each of those is bind-mounted read-only, for the workspace's git dir and for
every git dir reachable from it: the git dirs of initialized submodules
(`modules/`) and of linked worktrees (`worktrees/`), and the same again from
each of those, since a submodule initialized inside a linked worktree keeps its
git dir under that worktree rather than under the repository. A `.git` that is
a pointer file rather than a directory -- a linked worktree, a submodule
checkout, `--separate-git-dir` -- is mounted read-only too, so the container
cannot repoint itself at a git dir none of these mounts cover.

Three things make read-only actually hold:

  * A read-only mount covers one path. The same host file reached through
    another mount of the workspace is still writable, so every guarded path is
    emitted once per `--target`.
  * A mount point cannot be renamed or removed, but a plain directory that
    merely *contains* one can: the submounts follow it, and the vacated path can
    be recreated writable, which is what the host reads afterwards. So every
    directory on the way down to a guarded path -- `.git`, `.git/modules`, a
    submodule's checkout -- is bound onto itself to make it a mount point too.
  * A read-only mount needs something on the host to cover, so a guarded path
    that is absent is created first rather than left as a gap -- a repo with no
    `config` works fine, and the container writing one is the whole problem.
    They are inert: empty directories are what `git init` produces, an empty
    `config` says nothing, and a `commondir` of `.` resolves to the git dir
    that contains it, which is where git looks anyway. Not quite invisible,
    though -- `git rev-parse --git-common-dir` answers with an absolute path
    where it used to answer `.git`, because git resolves the pointer it now
    finds.

Symlinks are refused rather than followed. A guarded path that is a symlink is
skipped, and the walk never descends through one: otherwise a container that
writes `.git/modules/x/HEAD` and points `.git/modules/x/config` at a host file
would have the next session mount that file in for it to read.

Everything else in a git dir stays writable -- objects, refs, index, logs -- so
commits, branches and fetches work as usual. Writes that do land in config
report `could not write config file ...: Device or resource busy`, which is the
point; note that git treats failing to record branch tracking as non-fatal, so
`git push -u` and `git switch <remote-branch>` still exit 0 and still say they
set up tracking that is not there.

Where this stops:

  * Only git dirs that exist when the session starts are covered. A repo the
    agent clones or `git init`s inside the workspace, or a submodule or worktree
    it adds mid-session, has an ordinary writable config and hooks -- as does an
    unrelated checkout already vendored inside the workspace, which is not
    reachable from the workspace's own git dir and is not searched for. Note
    what that allows: a `.git` written into an existing subdirectory shadows
    the guarded repo for anything run from inside that directory, is invisible
    to `git status`, `git clean` and `git diff` at the root (git skips entries
    named `.git` when scanning), and needs no git command from the user -- a
    prompt or editor that shells out to git in that directory is enough, and
    `core.fsmonitor` runs on the index refresh a dirty-state check does. Git's
    own gate for this, `safe.directory`, keys on ownership, and the anvil runs
    as the user's own uid so it never fires.
  * A path carrying a colon or a newline cannot be spelled as a docker `-v`
    value, so it is reported and left unguarded rather than turned into some
    other mount. Nothing is created on the host for such a path either.
  * Hooks and commands that config already points *outside* the git dir
    (`core.hooksPath = .githooks`, husky's `.husky/`, an `include.path` into the
    worktree) live in the workspace, as do attribute-driven filter and diff
    commands, which a tracked `.gitattributes` invokes just as well as
    `.git/info/attributes`.

The workspace is not a trust boundary: an agent that can edit `package.json`, a
`Makefile` or `.pre-commit-config.yaml` can already run code on the host by
other means. The point of these mounts is to close the paths that need no such
cooperation -- the ones that fire on a bare `git status` in a repo the user has
no reason to distrust.
"""

import os
import subprocess
import sys

# Files made read-only in a git dir that holds a repository's config and hooks:
# the workspace's own git dir, a submodule's, a linked worktree's shared common
# dir. The value is what to write when the file is absent -- an empty `config`
# is inert, and a repo works fine without one, so its absence is a hole to fill
# rather than a sign there is nothing to guard.
REPOSITORY_FILES = {"config": "", "commondir": ".\n"}

# `hooks/` is where git runs scripts from. `remotes/` and `branches/` are the
# pre-config way to define a remote, still read when config defines no remote
# of that name, so a planted file there decides where a `git fetch <name>`
# goes -- and what it runs, for a URL naming a transport that executes one.
REPOSITORY_DIRS = ("hooks", "remotes", "branches")

# A per-worktree git dir under `worktrees/` has no config or hooks of its own --
# both come from the common dir -- but it carries the `commondir` pointer that
# says which common dir that is. Never written: unlike a repository's, its
# contents are a real relative path.
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


# --- Reading the repo ---------------------------------------------------------


def git_output(cwd, *args):
    """Stdout of a git command, or None if git failed or is not installed."""
    try:
        completed = subprocess.run(
            ["git", "-C", cwd] + list(args), capture_output=True, text=True)
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def common_git_dir(workspace, warn):
    """The git dir holding `workspace`'s config and hooks, or None.

    Resolved through the filesystem so it can be compared against the workspace
    and its `.git` without a symlink in the path changing the answer.
    """
    found = git_output(
        workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not found:
        if os.path.exists(os.path.join(workspace, ".git")):
            warn("%s looks like a git repo but git would not read it; its git "
                 "configuration is writable in the container" % workspace)
        return None
    return os.path.realpath(found)


def read_config(git_dir):
    """A repository git dir's own config as a dict, or {}.

    NUL-separated so a value carrying a newline cannot pass itself off as
    another setting -- the config of a fabricated git dir is not trustworthy
    input. Keys arrive lowercased, as `git config --list` reports them.
    """
    config = os.path.join(git_dir, "config")
    if not os.path.isfile(config):
        return {}
    listed = git_output(git_dir, "config", "--file", config, "--list", "-z")
    if not listed:
        return {}
    values = {}
    for record in listed.split("\0"):
        if not record:
            continue
        key, newline, value = record.partition("\n")
        # A key written with no `=` at all has no value part, which git reads
        # as true; one written with an empty value reads as false.
        values[key] = value if newline else None
    return values


def git_int(value):
    """A config value as git reads an integer, or None if it is not one.

    Git accepts what C's `strtoimax` with base 0 does -- decimal, `0x` hex, a
    leading `0` for octal -- plus a `k`/`m`/`g` size suffix.
    """
    text = value
    scale = 1
    if text and text[-1] in "kKmMgG":
        scale = 1024 ** ("kmg".index(text[-1].lower()) + 1)
        text = text[:-1]
    if "_" in text:
        # Python reads these as digit separators; C stops at the underscore.
        return None
    for base in (0, 8):
        try:
            return int(text, base) * scale
        except ValueError:
            continue
    return None


def is_true(config, key):
    """Read `key` the way git reads a boolean: a bool word or a nonzero int."""
    if key not in config:
        return False
    raw = config[key]
    if raw is None:
        return True
    value = raw.strip().lower()
    if value in ("true", "yes", "on"):
        return True
    if value in ("false", "no", "off", ""):
        return False
    number = git_int(value)
    return number is not None and number != 0


def worktree_config_enabled(config):
    """True if git reads `config.worktree` for the repository `config` is from.

    The extension is per-repository, so each repository git dir answers for
    itself and for its own worktrees: a submodule that has had
    `git sparse-checkout` run in it has the extension on when the superproject
    does not. A worktree git dir has no config of its own, so asking it
    directly would always answer no.
    """
    return is_true(config, "extensions.worktreeconfig")


def _child_dirs(parent):
    """Directory entries of `parent`, skipping symlinks and unreadable paths."""
    if os.path.islink(parent):
        return []
    try:
        entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
    except OSError:
        return []
    return [entry.path for entry in entries
            if entry.is_dir(follow_symlinks=False)]


def submodule_git_dirs(git_dir):
    """The initialized submodule git dirs `git_dir` hosts.

    They live under `modules/`, keyed by submodule name -- which may itself
    contain slashes, so the intermediate directories are walked rather than
    assumed to be one level deep. A submodule's own submodules are not returned
    here: each hit is a repository, and the caller comes back round for it.
    """
    found = []
    pending = [os.path.join(git_dir, "modules")]
    while pending:
        for path in _child_dirs(pending.pop()):
            if os.path.isfile(os.path.join(path, "HEAD")):
                found.append(path)
            else:
                pending.append(path)
    return found


def worktree_git_dirs(git_dir):
    """Every linked worktree's git dir below `git_dir`."""
    return _child_dirs(os.path.join(git_dir, "worktrees"))


def submodule_checkout(git_dir, config):
    """The checkout a submodule git dir belongs to, or None.

    `core.worktree` is written relative to the git dir when git creates the
    submodule, so it is resolved against it.
    """
    worktree = config.get("core.worktree")
    if not worktree:
        return None
    return os.path.realpath(os.path.join(git_dir, worktree))


def worktree_checkout_pointer(worktree_git_dir):
    """The `.git` pointer file of a linked worktree, from its `gitdir` file."""
    record = os.path.join(worktree_git_dir, "gitdir")
    if not os.path.isfile(record):
        return None
    try:
        with open(record) as handle:
            named = handle.read().strip()
    except OSError:
        return None
    return os.path.realpath(named) if named else None


# --- Placing the mounts -------------------------------------------------------


def is_inside(path, root):
    """True if `path` is strictly below `root`. Both must be resolved."""
    if not root:
        return False
    relative = os.path.relpath(path, root)
    return relative != os.curdir and relative != os.pardir \
        and not relative.startswith(os.pardir + os.sep)


def ensure_present(path, contents, warn):
    """Create `path` if absent so a read-only mount has something to cover.

    `contents` of None means a directory. A git dir this user cannot write is
    reported and left alone: the container runs as the same user, so it cannot
    write there either, and failing the launch over it would help no one.
    """
    if os.path.islink(path):
        warn("%s is a symlink; leaving it writable in the container" % path)
        return False
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


def guarded_paths(git_dir, files, dirs, worktree_config, warn):
    """Host paths inside one git dir to make read-only, creating what is due."""
    wanted = dict(files)
    if worktree_config:
        wanted.update(WORKTREE_CONFIG)
    paths = []
    for name, contents in wanted.items():
        path = os.path.join(git_dir, name)
        if contents is None:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
        elif not ensure_present(path, contents, warn):
            continue
        paths.append(path)
    for name in dirs:
        path = os.path.join(git_dir, name)
        if ensure_present(path, None, warn):
            paths.append(path)
    return paths


def mountable(path, warn):
    """True if `path` survives the trip to docker as part of a `-v` value.

    A `-v` value is colon-separated and the caller reads one mount per line, so
    a path carrying either would be split into a different mount -- and
    directories inside a git dir are the container's to name. Callers check
    this before creating anything, so a path that cannot be guarded is also not
    modified.
    """
    if ":" in path or "\n" in path:
        warn("%s cannot be expressed as a docker mount (it contains a colon or "
             "newline); leaving it writable in the container" % path)
        return False
    return True


def ancestors_between(path, root):
    """The directories from `root` down to `path`, both ends excluded."""
    if not is_inside(path, root):
        return []
    parts = os.path.relpath(path, root).split(os.sep)
    return [os.path.join(root, *parts[:depth])
            for depth in range(1, len(parts))]


def build_mounts(workspace, targets, warn=None):
    """Every mount that guards `workspace`'s git configuration.

    Each host path is placed at every container path it is reachable from:
    below a `--target` when it lives in the workspace, and at its own absolute
    path when it does not -- which is where the pointer file naming it says to
    look. Two paths landing on the same container path keep the first, since
    docker refuses a repeated mount destination.
    """
    report = warn or (lambda message: None)
    reported = set()

    def warn(message):
        # An unguardable directory is reached once per path below it.
        if message not in reported:
            reported.add(message)
            report(message)

    workspace = os.path.realpath(workspace)
    common = common_git_dir(workspace, warn)
    if common is None:
        return []

    # A git dir outside the workspace is mounted at its own path, so that is
    # the mount everything inside it hangs off; inside the workspace, the
    # workspace mount is.
    outside = None if common == workspace or is_inside(common, workspace) \
        else common
    if outside:
        warn("Detected a git dir outside the workspace; mounting it: %s"
             % outside)

    def container_paths(host):
        if host == workspace:
            return list(targets)
        if is_inside(host, workspace):
            relative = os.path.relpath(host, workspace)
            return [os.path.join(target, relative) for target in targets]
        if host == outside or is_inside(host, outside):
            return [host]
        return []

    def root_of(host):
        return workspace if is_inside(host, workspace) else outside

    # Ordered so a directory precedes what hangs off it, which is how the
    # launcher's `set -x` output then reads.
    plan = []

    def guard(host, read_only):
        for ancestor in ancestors_between(host, root_of(host)):
            plan.append((ancestor, False))
        plan.append((host, read_only))

    # Guarding a git dir is moot if the pointer file naming it can be repointed
    # at an unguarded one.
    workspace_git = os.path.join(workspace, ".git")
    if os.path.islink(workspace_git):
        warn("%s is a symlink; the container can repoint it at a git dir none "
             "of these mounts cover" % workspace_git)
    pointers = [workspace_git]

    # A submodule is a repository in its own right: its git dir has the config
    # and hooks the host runs when the user works in it, and submodules and
    # worktrees of its own. A queue rather than a walk, because a repository is
    # reached two ways -- under a repository's `modules/`, and under a linked
    # worktree's, which is where git puts a submodule initialized in that
    # worktree rather than under the repository it belongs to.
    pending = [common]
    seen = set()
    while pending:
        repository = pending.pop()
        if repository in seen or not mountable(repository, warn):
            continue
        seen.add(repository)
        config = read_config(repository)
        worktree_config = worktree_config_enabled(config)
        if repository != workspace:
            # A bare repo is its own workspace mount, already a mount point.
            guard(repository, False)
        for path in guarded_paths(repository, REPOSITORY_FILES,
                                  REPOSITORY_DIRS, worktree_config, warn):
            guard(path, True)
        # Set on a submodule's git dir, and on the git dir of a repo whose
        # checkout is somewhere else; either way it names the checkout whose
        # `.git` points back here.
        checkout = submodule_checkout(repository, config)
        if checkout:
            pointers.append(os.path.join(checkout, ".git"))
        pending += submodule_git_dirs(repository)
        for worktree in worktree_git_dirs(repository):
            if not mountable(worktree, warn):
                continue
            guard(worktree, False)
            # The extension is the repository's, and a worktree git dir has no
            # config of its own to read it from.
            for path in guarded_paths(worktree, WORKTREE_FILES, (),
                                      worktree_config, warn):
                guard(path, True)
            pointers.append(worktree_checkout_pointer(worktree))
            pending += submodule_git_dirs(worktree)

    for pointer in pointers:
        if pointer and os.path.isfile(pointer) and not os.path.islink(pointer) \
                and container_paths(pointer) and mountable(pointer, warn):
            guard(pointer, True)

    specs = {}
    for host, read_only in plan:
        for container in container_paths(host):
            if not mountable(container, warn):
                continue
            spec = "%s:%s:ro" % (host, container) if read_only \
                else "%s:%s" % (host, container)
            specs.setdefault(container, spec)
    return list(specs.values())


def main(argv, out=sys.stdout, err=sys.stderr):
    try:
        workspace, targets = parse_args(argv)
    except UsageError as exc:
        err.write("git-guard: %s\n" % exc)
        return 2

    def warn(message):
        err.write("%s\n" % message)

    for spec in build_mounts(workspace, targets, warn=warn):
        out.write("%s\n" % spec)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
