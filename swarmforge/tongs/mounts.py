"""The `mounts:` magic words and the docker bind specs they turn into.

A definition never names a raw host path: it asks for a mount by word, and this
module decides where that lands inside the container and what `-v` value docker
is given. Validation and argv assembly both come through here, so a mount is
judged by the same rules whichever asks.
"""

import posixpath

from .model import SOCKET_MOUNT
from .secrets import SECRET_FIFO_DIR, SECRET_INJECT_SHELL, partition_secret_env


# Mount magic words (decision: opt-in words, never raw host paths from a
# definition). `workspace` mounts the session's workspace; `docker-socket` grants
# docker control (the broker's privilege, surfaced by the approval gate). A mount
# is spelled `<word>[:/target][:mode]` -- see `parse_mount` for the grammar.
WORKSPACE_MOUNT = "workspace"
MOUNT_WORDS = (WORKSPACE_MOUNT, SOCKET_MOUNT)
DEFAULT_WORKSPACE_MOUNT_TARGET = "/workspace"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"

# The docker access modes a mount may request. Anything else in the mode slot is
# rejected rather than forwarded, so a typo cannot reach the daemon as a mount
# option nobody vetted.
MOUNT_MODES = ("ro", "rw")


def _has_socket_mount(defn):
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == SOCKET_MOUNT:
            return True
    return False


def normalize_mount_target(path):
    """A mount target as docker resolves a bind destination.

    Collapses `.`/`..`/repeated separators so the spellings of one destination
    compare equal. `posixpath.normpath` keeps exactly two leading slashes; docker
    does not, so `//run` must not be told apart from `/run` here either.
    """
    return "/" + posixpath.normpath(path).lstrip("/")


def _targets_overlap(one, other):
    """True if two mount destinations are the same path or nest inside each other."""
    one, other = normalize_mount_target(one), normalize_mount_target(other)
    return (
        one == other
        or one.startswith(other.rstrip("/") + "/")
        or other.startswith(one.rstrip("/") + "/")
    )


def parse_mount(mount, words=MOUNT_WORDS):
    """Split a `mounts:` entry into `(word, target, mode)`.

    The grammar is `<word>[:/target][:mode]`: the magic word, an optional absolute
    path naming where the container sees the mount, and an optional access mode,
    which is always last -- so `workspace`, `workspace:ro`, `workspace:/work` and
    `workspace:/work:ro` are all valid. `target`/`mode` are None when not declared;
    the caller supplies the default target for its magic word.

    The word is checked against `words` before the rest of the entry, since a mount
    nobody recognizes has no meaningful target or mode; narrow that set to accept
    fewer words, never widen it to accept a raw host path. Raises `ValueError` for
    an unaccepted word, a field that is neither an absolute path nor a mode, a mode
    that is not last, more than one target, or a target resolving to the root.
    """
    fields = mount.split(":")
    word, fields = fields[0], fields[1:]
    if word not in words:
        if word in MOUNT_WORDS:
            # A real magic word the caller does not allow here is a policy refusal,
            # not a typo, so it must not read as one.
            raise ValueError(
                "mount %r: the %r mount is not allowed here (expected %s)"
                % (mount, word, " or ".join(repr(allowed) for allowed in words))
            )
        raise ValueError(
            "unknown mount %r (expected %s)"
            % (mount, " or ".join(repr(known) for known in words))
        )
    target = None
    mode = None
    for index, field in enumerate(fields):
        if field in MOUNT_MODES:
            if index != len(fields) - 1:
                raise ValueError(
                    "mount %r: access mode %r must be the last field" % (mount, field)
                )
            mode = field
        elif field.startswith("/"):
            if target is not None:
                raise ValueError("mount %r has more than one target path" % (mount,))
            if field.split() != [field]:
                # Whitespace would become part of the directory name, so the image
                # finds nothing where it looked.
                raise ValueError(
                    "mount %r: target path %r contains whitespace" % (mount, field)
                )
            if normalize_mount_target(field) == "/":
                # Mounting over the container's root would bury the image; docker
                # refuses it too, but failing here keeps it a config error.
                raise ValueError(
                    "mount %r: %r is not a usable target path (it is the "
                    "container's root)" % (mount, field)
                )
            target = field
        else:
            raise ValueError(
                "mount %r: %r is neither an absolute target path nor an access "
                "mode (%s)" % (mount, field, "/".join(MOUNT_MODES))
            )
    return word, target, mode


def reserved_mount_targets(defn, socket_path=DEFAULT_DOCKER_SOCKET):
    """Destinations a tong's own wiring occupies inside it: `{path: why}`.

    Derived from the definition, since each only exists for the tongs that ask for
    it: the secret-delivery tmpfs (and the shell whose wrapper creates and reads
    the FIFO on it) come with secret references, the docker socket with a
    `docker-socket` mount. `mount_target_error`
    compares destinations against this map, as written -- a target that reaches one
    of these only through a symlink inside the image is not something it can see.
    """
    paths = {}
    env = defn.get("env")
    if isinstance(env, dict) and partition_secret_env(env)[1]:
        paths[SECRET_FIFO_DIR] = "the tmpfs where the launcher delivers this tong's secrets"
        paths[SECRET_INJECT_SHELL] = "the shell the secret wrapper execs"
    if _has_socket_mount(defn):
        paths[socket_path] = "where the docker socket is mounted"
    return paths


def mount_destination(word, target, socket_path=DEFAULT_DOCKER_SOCKET):
    """Where a mount lands inside the container, normalized.

    The declared target when there is one, otherwise the word's default: /workspace
    for the workspace, its own host path for the socket (that is where a docker
    client looks for it). Raises `ValueError` for any other word, so a magic word
    added without a default cannot inherit the socket's.
    """
    if word == WORKSPACE_MOUNT:
        return normalize_mount_target(target or DEFAULT_WORKSPACE_MOUNT_TARGET)
    if word == SOCKET_MOUNT:
        return normalize_mount_target(socket_path)
    raise ValueError("mount %r has no destination" % (word,))


def mount_target_error(mount, word, target, destination, reserved):
    """Why a mount's target is unusable, or None if it is fine.

    Two rules beyond the grammar: only `workspace` takes a target, and no mount may
    land on one of the `reserved` destinations (`{path: why}`, from
    `reserved_mount_targets`) -- docker layers overlapping mounts, which either
    buries the tong's wiring or has docker create a mountpoint inside the user's
    workspace on the host. `destination` is where the mount actually lands
    (`mount_destination`), so a default target is judged like a declared one.
    Validation and argv assembly both ask this, so both give the same verdict and
    the same message for one `reserved` map.
    """
    if word != WORKSPACE_MOUNT:
        if target is not None:
            return ("mount %r: only the '%s' mount takes a target path"
                    % (mount, WORKSPACE_MOUNT))
        # The socket lands on its own reserved path by construction.
        return None
    for path, why in sorted(reserved.items()):
        if _targets_overlap(destination, path):
            return "mount %r: %s overlaps %s, %s" % (mount, destination, path, why)
    return None


def overlapping_mount_error(mount, destination, placed):
    """Why a mount collides with one already placed, or None if it is clear.

    `placed` is the `(mount, destination)` pairs accepted so far. Docker refuses two
    binds onto one destination outright, and creates the inner mountpoint of nested
    ones inside the outer bind -- inside the user's workspace, for a workspace bind.
    """
    for other, other_destination in placed:
        if _targets_overlap(destination, other_destination):
            return ("mount %r: %s overlaps mount %r at %s"
                    % (mount, destination, other, other_destination))
    return None


def tong_mount_specs(defn, workspace, socket_path=DEFAULT_DOCKER_SOCKET):
    """Concrete docker `-v` specs for a tong's `mounts:` magic words.

    Returns the list of `-v` *values* (the orchestrator pairs each with a `-v`
    flag). `workspace[:/target][:mode]` mounts the session workspace, at
    /workspace unless the definition names another target; `docker-socket[:mode]`
    bind-mounts the host docker socket onto the same path it has on the host.
    Raises `ValueError` for a non-string entry, a malformed mount, an unusable or
    colliding destination, or a `workspace` mount when no workspace path is known
    -- a definition never names a raw host path, so anything else is a mistake that
    should stop the launch.
    """
    specs = []
    reserved = reserved_mount_targets(defn, socket_path)
    placed = []                          # (mount, destination) already emitted
    for mount in defn.get("mounts") or []:
        if not isinstance(mount, str):
            raise ValueError("mount entries must be strings, got %r" % (mount,))
        word, target, mode = parse_mount(mount)
        # Normalized, so the destination emitted is the one that was judged.
        destination = mount_destination(word, target, socket_path)
        reason = (mount_target_error(mount, word, target, destination, reserved)
                  or overlapping_mount_error(mount, destination, placed))
        if reason:
            raise ValueError(reason)
        if word == WORKSPACE_MOUNT:
            if not workspace:
                raise ValueError("mount 'workspace' requested but no workspace path is known")
            source = workspace
        elif word == SOCKET_MOUNT:
            source = socket_path
        else:
            # Unreachable while `parse_mount` guards the word set; kept so a new
            # magic word cannot inherit the socket bind.
            raise ValueError("mount %r has no docker spec" % (mount,))
        spec = "%s:%s" % (source, destination)
        if mode:
            spec += ":" + mode
        specs.append(spec)
        placed.append((mount, destination))
    return specs


def workspace_mount_placements(defn, socket_path=DEFAULT_DOCKER_SOCKET):
    """Where a tong's `workspace` mounts land: `[(destination, mode)]`.

    One entry per `workspace` magic word in `mounts:`, in declaration order,
    with the normalized container destination and the declared access mode
    (None when the entry names no mode -- docker's read-write default). The
    orchestrator asks this to place the git-dir mounts a workspace checkout
    needs beside the workspace bind (see swarmforge.anvil). Empty when the
    definition mounts no workspace. Raises `ValueError` for a malformed
    entry, like `tong_mount_specs`.
    """
    placements = []
    for mount in defn.get("mounts") or []:
        if not isinstance(mount, str):
            raise ValueError("mount entries must be strings, got %r" % (mount,))
        word, target, mode = parse_mount(mount)
        if word == WORKSPACE_MOUNT:
            placements.append((mount_destination(word, target, socket_path), mode))
    return placements
