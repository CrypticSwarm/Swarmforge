"""Open a repository as one tmux session, with a window per worktree.

The session belongs to the repository's root (`worktrees.find_repository`),
so any path inside the repository opens the same session, named for the root.
Its windows are:

    Repo        the root
    <default>   the default branch's worktree, when that is not Repo
    ...         every other worktree, in the order git lists them

A worktree's window is named for its path under the root, title-cased, so
`feature/tmux-tui` opens as `Feature Tmux Tui`; a name already taken gets a
number. Worktrees whose directory is gone are skipped.

Opening is idempotent: it only adds windows whose name is not open yet, and
never types into a window; each starts in its worktree.
"""

import collections
import os
import re

from swarmforge import command, worktrees

REPO_WINDOW = "Repo"

# For a worktree whose path holds no letters or digits.
FALLBACK_WINDOW = "Worktree"

# tmux rewrites these to `_` in a session name, so a target spelled with them misses.
_SESSION_RESERVED = re.compile(r"[.:]")

_WORD = re.compile(r"[^\W_]+")

Window = collections.namedtuple("Window", "name directory")


class TmuxError(command.CommandError):
    """A tmux command failed; see `command.CommandError`."""


def tmux(*args, capture=False, quiet=False):
    """Run tmux through `command.run`."""
    return command.run(["tmux"] + list(args), TmuxError,
                       capture=capture, quiet=quiet)


def _literal(value):
    """`value` escaped for a tmux option that expands formats, as `-s` and `-c` do."""
    return value.replace("#", "##")


def window_name(root, path):
    """The worktree at `path` named from its place under `root`.

    A worktree outside `root` is named for its basename alone.
    """
    relative = os.path.relpath(path, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        relative = os.path.basename(path)
    words = _WORD.findall(relative)
    return " ".join(word[:1].upper() + word[1:].lower()
                    for word in words) or FALLBACK_WINDOW


def session_name(root, name=None):
    """`name`, or the root's directory name, spelled the way tmux stores it."""
    return _SESSION_RESERVED.sub("_", name or os.path.basename(root))


def _default_first(entries, root_branch, candidates):
    """`entries` with the default branch's worktree moved to the front.

    Candidates are tried in order, stopping at `root_branch`, the main
    worktree's own branch, so a stale `master` beside a root on `main` stays
    where git listed it.
    """
    branches = [entry.branch for entry in entries]
    for candidate in candidates:
        if candidate in branches:
            index = branches.index(candidate)
            return [entries[index]] + entries[:index] + entries[index + 1:]
        if candidate == root_branch:
            break
    return entries


def plan_windows(path):
    """The windows for `path`'s repository, Repo first."""
    repository = worktrees.find_repository(path)
    main, linked = repository.worktrees[0], repository.worktrees[1:]
    linked = _default_first(
        [entry for entry in linked if os.path.isdir(entry.path)], main.branch,
        worktrees.default_branch_candidates(repository.git_dir))
    root = repository.root
    windows = [Window(REPO_WINDOW, root)]
    taken = {REPO_WINDOW}
    for entry in linked:
        base = name = window_name(root, entry.path)
        number = 1
        while name in taken:
            number += 1
            name = "%s %d" % (base, number)
        taken.add(name)
        windows.append(Window(name, entry.path))
    return windows


def has_session(session):
    try:
        tmux("has-session", "-t", "=" + session, quiet=True)
    except TmuxError:
        return False
    return True


def window_names(session):
    listing = tmux("list-windows", "-t", "=" + session,
                   "-F", "#{window_name}", capture=True)
    return set(listing.splitlines())


def open_session(path, name=None):
    """Create or top up the session for `path`'s repository, returning its name.

    A new session starts on its Repo window; windows added to an existing one
    do not take focus.
    """
    windows = plan_windows(path)
    root = windows[0].directory
    session = session_name(root, name)
    if not has_session(session):
        tmux("new-session", "-d", "-s", _literal(session), "-n", REPO_WINDOW,
             "-c", _literal(root))
    opened = window_names(session)
    for window in windows:
        if window.name not in opened:
            tmux("new-window", "-d", "-t", "=%s:" % session,
                 "-n", window.name, "-c", _literal(window.directory))
    return session


def attach(session):
    """Put this terminal on `session`.

    Inside tmux that is switching the client; outside, this process is replaced
    by `tmux attach-session`, so it does not return.
    """
    target = "=" + session
    if os.environ.get("TMUX"):
        tmux("switch-client", "-t", target)
        return
    argv = ["tmux", "attach-session", "-t", target]
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise TmuxError(command.not_runnable(argv, exc))
