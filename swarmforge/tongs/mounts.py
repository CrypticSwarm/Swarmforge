"""The `mounts:` magic words and the docker bind specs they turn into.

A definition never names a raw host path: it asks for a mount by word, and this
module decides where that lands inside the container and what `-v` value docker
is given. Validation and argv assembly both come through here, so a mount is
judged by the same rules whichever asks.
"""

import posixpath
import re

from .model import SESSION_HANDLE_ENV, SOCKET_MOUNT, TMUX_MOUNT
from .secrets import SECRET_FIFO_DIR, SECRET_INJECT_SHELL, partition_secret_env


WORKSPACE_MOUNT = "workspace"
MOUNT_WORDS = (WORKSPACE_MOUNT, SOCKET_MOUNT, TMUX_MOUNT)
DEFAULT_WORKSPACE_MOUNT_TARGET = "/workspace"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
# Fixed, so validation never depends on the host; beside, not under, the secret tmpfs.
TMUX_MOUNT_TARGET = "/run/swarmforge-tmux"

# An allowlist, so an unvetted mount option cannot reach the daemon.
MOUNT_MODES = ("ro", "rw")

# What tmux creates (mode 0700) under $TMUX_TMPDIR or /tmp for each user's sockets.
_TMUX_SOCKET_DIR_RE = re.compile(r"tmux-[0-9]+")
_DECIMAL_RE = re.compile(r"[0-9]+")
_TMUX_PANE_RE = re.compile(r"%[0-9]+")

# The env the launcher sets on a tmux-socket tong; a definition never supplies it.
TMUX_LAUNCHER_ENV = ("TMUX", "TMUX_PANE", SESSION_HANDLE_ENV)


def _has_socket_mount(defn):
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == SOCKET_MOUNT:
            return True
    return False


def _has_tmux_mount(defn):
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == TMUX_MOUNT:
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
            # A known word the caller disallows is a refusal, not a typo.
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
                # Docker would keep the whitespace in the name, so the image finds nothing there.
                raise ValueError(
                    "mount %r: target path %r contains whitespace" % (mount, field)
                )
            if normalize_mount_target(field) == "/":
                # Docker refuses this too; failing here keeps it a config error.
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
    `docker-socket` mount, the tmux socket directory with a `tmux-socket` mount.
    `mount_target_error`
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
    if _has_tmux_mount(defn):
        paths[TMUX_MOUNT_TARGET] = "where the tmux socket directory is mounted"
    return paths


def mount_destination(word, target, socket_path=DEFAULT_DOCKER_SOCKET):
    """Where a mount lands inside the container, normalized.

    The declared target when there is one, otherwise the word's default: /workspace
    for the workspace, its own host path for the socket (that is where a docker
    client looks for it), and `TMUX_MOUNT_TARGET` for the tmux socket directory
    (the tong's `TMUX` names the socket there). Raises `ValueError` for any other
    word, so a magic word added without a default cannot inherit the socket's.
    """
    if word == WORKSPACE_MOUNT:
        return normalize_mount_target(target or DEFAULT_WORKSPACE_MOUNT_TARGET)
    if word == SOCKET_MOUNT:
        return normalize_mount_target(socket_path)
    if word == TMUX_MOUNT:
        return TMUX_MOUNT_TARGET
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
        # Each socket lands on its own reserved path by construction.
        return None
    for path, why in sorted(reserved.items()):
        if _targets_overlap(destination, path):
            return "mount %r: %s overlaps %s, %s" % (mount, destination, path, why)
    return None


def mount_mode_error(mount, word, mode):
    """Why a mount's access mode is refused, or None if it is fine.

    `tmux-socket` is read-only whatever it declares. A tmux client only needs
    `connect()` on the socket, which a read-only bind allows; a writable bind of
    the socket directory would let the tong replace the socket the host's own
    tmux clients dial. Validation and argv assembly both ask this, like
    `mount_target_error`, so both refuse `tmux-socket:rw` with one message.
    """
    if word == TMUX_MOUNT and mode == "rw":
        return "mount %r: the '%s' mount is always read-only" % (mount, TMUX_MOUNT)
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


def tong_mount_specs(defn, workspace, socket_path=DEFAULT_DOCKER_SOCKET,
                     tmux_socket_dir=None):
    """Concrete docker `-v` specs for a tong's `mounts:` magic words.

    Returns the list of `-v` *values* (the orchestrator pairs each with a `-v`
    flag). `workspace[:/target][:mode]` mounts the session workspace, at
    /workspace unless the definition names another target; `docker-socket[:mode]`
    bind-mounts the host docker socket onto the same path it has on the host;
    `tmux-socket[:ro]` bind-mounts `tmux_socket_dir` -- the host directory holding
    the launcher's tmux socket, from `resolve_tmux_socket` -- read-only at
    `TMUX_MOUNT_TARGET`. The directory rather than the socket, because tmux
    deletes and recreates the socket when its server restarts.
    Raises `ValueError` for a non-string entry, a malformed mount, an unusable or
    colliding destination, a refused access mode, or a `workspace` or
    `tmux-socket` mount when no host source for it is known -- a definition never
    names a raw host path, so anything else is a mistake that should stop the
    launch.
    """
    specs = []
    reserved = reserved_mount_targets(defn, socket_path)
    placed = []                          # (mount, destination) already emitted
    for mount in defn.get("mounts") or []:
        if not isinstance(mount, str):
            raise ValueError("mount entries must be strings, got %r" % (mount,))
        word, target, mode = parse_mount(mount)
        destination = mount_destination(word, target, socket_path)
        reason = (mount_target_error(mount, word, target, destination, reserved)
                  or mount_mode_error(mount, word, mode)
                  or overlapping_mount_error(mount, destination, placed))
        if reason:
            raise ValueError(reason)
        if word == WORKSPACE_MOUNT:
            if not workspace:
                raise ValueError("mount 'workspace' requested but no workspace path is known")
            source = workspace
        elif word == SOCKET_MOUNT:
            source = socket_path
        elif word == TMUX_MOUNT:
            if not tmux_socket_dir:
                raise ValueError(
                    "mount '%s' requested but no tmux socket directory is known" % TMUX_MOUNT)
            source = tmux_socket_dir
            mode = "ro"
        else:
            # Unreachable via `parse_mount`; a new word must not inherit the socket bind.
            raise ValueError("mount %r has no docker spec" % (mount,))
        spec = "%s:%s" % (source, destination)
        if mode:
            spec += ":" + mode
        specs.append(spec)
        placed.append((mount, destination))
    return specs


def resolve_tmux_socket(tmux):
    """The host directory a `tmux-socket` mount binds, and the tong's own `TMUX`.

    `tmux` is the launcher's raw `$TMUX`, `<socket-path>,<server-pid>,<session-id>`,
    and this is the one place it is parsed. Returns
    `(host_dir, socket_path, container_tmux)`: the socket's parent directory, the
    socket's host path, and the same value re-pointed at the socket's name under
    `TMUX_MOUNT_TARGET`, so a stock tmux client in the tong reaches the host
    server with no flags.

    Raises `ValueError` unless the value has exactly three comma-separated fields,
    the pid and session id are decimal, and the socket path is a normalized
    absolute path with no `:` (docker's `-v` separator), `,` (tmux reads the path
    up to the first one), or whitespace. The socket must also sit directly in
    tmux's own per-user directory (`tmux-<uid>`): binding a `-S` socket's parent
    could hand the tong an arbitrary host directory, such as a home directory.
    """
    fields = tmux.rsplit(",", 2) if isinstance(tmux, str) else []
    if len(fields) != 3:
        raise ValueError(
            "$TMUX %r is not '<socket-path>,<server-pid>,<session-id>'" % (tmux,))
    path, pid, session = fields
    if not (_DECIMAL_RE.fullmatch(pid) and _DECIMAL_RE.fullmatch(session)):
        raise ValueError(
            "$TMUX %r: the server pid and session id must be decimal numbers" % (tmux,))
    if not path.startswith("/") or normalize_mount_target(path) != path:
        raise ValueError(
            "$TMUX %r: socket path %r is not a normalized absolute path" % (tmux, path))
    if any(char in ":," or char.isspace() for char in path):
        raise ValueError(
            "$TMUX %r: socket path %r contains ':', ',' or whitespace" % (tmux, path))
    host_dir, socket_name = posixpath.split(path)
    if not _TMUX_SOCKET_DIR_RE.fullmatch(posixpath.basename(host_dir)):
        raise ValueError(
            "$TMUX %r: socket %r is not in tmux's default per-user socket directory "
            "(tmux-<uid> under $TMUX_TMPDIR or /tmp); a socket at a custom -S path "
            "is refused, since its whole directory would be mounted" % (tmux, path))
    return (host_dir, path,
            "%s/%s,%s,%s" % (TMUX_MOUNT_TARGET, socket_name, pid, session))


def check_tmux_pane(pane):
    """The launcher's `$TMUX_PANE` (`%<digits>`), or `ValueError` if it is not one."""
    if not isinstance(pane, str) or not _TMUX_PANE_RE.fullmatch(pane):
        raise ValueError("$TMUX_PANE %r is not a tmux pane id ('%%<number>')" % (pane,))
    return pane


def tmux_secret_env_error(defn):
    """Why a `tmux-socket` definition's secret env is refused, or None if it is fine.

    The launcher owns `TMUX_LAUNCHER_ENV` on a tong that mounts `tmux-socket` and
    replaces a plain value the definition gives one of them, but a secret value is
    exported by the FIFO wrapper after docker's `-e` values and so would win.
    Refusing a secret reference there keeps the launcher's values the ones the
    tong runs with.
    """
    env = defn.get("env")
    if not _has_tmux_mount(defn) or not isinstance(env, dict):
        return None
    owned = [name for name in TMUX_LAUNCHER_ENV if name in partition_secret_env(env)[1]]
    if not owned:
        return None
    return ("env %s: the launcher sets %s on a tong that mounts '%s', so the "
            "definition may not make them secret references"
            % (", ".join(owned), ", ".join(TMUX_LAUNCHER_ENV), TMUX_MOUNT))


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
