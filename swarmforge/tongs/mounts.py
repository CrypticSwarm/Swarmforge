"""The `mounts:` magic words and the docker `-v` specs they turn into.

A definition never names a raw host path: it asks for a mount by word, and this
module decides where that lands inside the container and what `-v` value docker
is given: a host path the launcher knows, or a named volume it names. Validation
and argv assembly both come through here, so a mount is judged by the same rules
whichever asks.
"""

import collections
import hashlib
import json
import posixpath
import re

from swarmforge.names import canonical_path, name_hint

from .model import (
    ORG,
    REPO,
    SESSION_HANDLE_ENV,
    SOCKET_MOUNT,
    TMUX_MOUNT,
    USER,
    WORKSPACE,
    workspace_key,
)
from .secrets import SECRET_FIFO_DIR, SECRET_INJECT_SHELL, partition_secret_env


WORKSPACE_MOUNT = "workspace"
VOLUME_MOUNT = "volume"
MOUNT_WORDS = (WORKSPACE_MOUNT, SOCKET_MOUNT, VOLUME_MOUNT, TMUX_MOUNT)
TARGETED_MOUNT_WORDS = (WORKSPACE_MOUNT, VOLUME_MOUNT)
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

# One `mounts:` entry as `checked_mounts` judged it; `error` is None when it is accepted.
CheckedMount = collections.namedtuple(
    "CheckedMount", ["mount", "word", "volume", "target", "mode", "destination", "error"])

VOLUME_NAME_PREFIX = "swarmforge-volume"

# 128 bits, so no definition can search its way onto another scope's volume.
VOLUME_DIGEST_LENGTH = 32

ORG_VOLUME_SCOPE = "org"
WORKSPACE_VOLUME_SCOPE = "workspace"
LOCAL_VOLUME_SCOPE = "local"

# No `_`, so the last `_` of a docker volume name splits the volume back off.
VOLUME_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")

# With the hint cap, keeps a docker volume name far below a 255-byte directory name.
VOLUME_NAME_MAX_LENGTH = 64
VOLUME_HINT_LIMIT = 32

# `env` holds the tong's `TMUX` and `TMUX_PANE`, the pane None outside a pane.
TmuxServer = collections.namedtuple("TmuxServer", ["host_dir", "socket_path", "env"])


def mounts_word(defn, word):
    """True if a tong's `mounts:` request the magic `word`.

    The word may carry a name, a target and/or a mode (e.g. `workspace:/code:ro`),
    so compare only the word before the first colon.
    """
    for mount in defn.get("mounts") or []:
        if isinstance(mount, str) and mount.split(":", 1)[0] == word:
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
    """Split a `mounts:` entry into `(word, volume, target, mode)`.

    The grammar is `<word>[:/target][:mode]`: the magic word, an optional absolute
    path naming where the container sees the mount, and an optional access mode,
    which is always last -- so `workspace`, `workspace:ro`, `workspace:/work` and
    `workspace:/work:ro` are all valid. `target`/`mode` are None when not declared;
    the caller supplies the default target for its magic word. The `volume` word
    is spelled `volume:<name>:/target[:mode]`, its target required; `volume` is
    that name, and None for every other word.

    The word is checked against `words` before the rest of the entry, since a mount
    nobody recognizes has no meaningful target or mode; narrow that set to accept
    fewer words, never widen it to accept a raw host path. Raises `ValueError` for
    an unaccepted word, a field that is neither an absolute path nor a mode, a mode
    that is not last, more than one target, a target resolving to the root, or a
    volume mount whose name is malformed or whose target is missing.
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
    volume = None
    if word == VOLUME_MOUNT:
        if not fields or not VOLUME_NAME_RE.fullmatch(fields[0]):
            raise ValueError(
                "mount %r: a volume mount is spelled volume:<name>:/target[:mode], "
                "with a name of letters, digits, '.' and '-' that starts with a "
                "letter or digit" % (mount,)
            )
        if len(fields[0]) > VOLUME_NAME_MAX_LENGTH:
            raise ValueError(
                "mount %r: a volume name is at most %d characters"
                % (mount, VOLUME_NAME_MAX_LENGTH)
            )
        volume, fields = fields[0], fields[1:]
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
    if word == VOLUME_MOUNT and target is None:
        raise ValueError("mount %r: a volume mount needs an absolute target path" % (mount,))
    return word, volume, target, mode


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
    if mounts_word(defn, SOCKET_MOUNT):
        paths[socket_path] = "where the docker socket is mounted"
    if mounts_word(defn, TMUX_MOUNT):
        paths[TMUX_MOUNT_TARGET] = "where the tmux socket directory is mounted"
    return paths


def mount_destination(word, target, socket_path=DEFAULT_DOCKER_SOCKET):
    """Where a mount lands inside the container, normalized.

    The declared target when there is one, otherwise the word's default: /workspace
    for the workspace, its own host path for the socket (that is where a docker
    client looks for it), and `TMUX_MOUNT_TARGET` for the tmux socket directory
    (the tong's `TMUX` names the socket there); a volume has none. Raises
    `ValueError` for a targetless volume and any other word, so a new word
    cannot inherit the socket's default.
    """
    if word == WORKSPACE_MOUNT:
        return normalize_mount_target(target or DEFAULT_WORKSPACE_MOUNT_TARGET)
    if word == SOCKET_MOUNT:
        return normalize_mount_target(socket_path)
    if word == VOLUME_MOUNT and target is not None:
        return normalize_mount_target(target)
    if word == TMUX_MOUNT:
        return TMUX_MOUNT_TARGET
    raise ValueError("mount %r has no destination" % (word,))


def mount_target_error(mount, word, target, destination, reserved):
    """Why a mount's target is unusable, or None if it is fine.

    Two rules beyond the grammar: only `workspace` and `volume` take a target, and
    no mount may land on one of the `reserved` destinations (`{path: why}`, from
    `reserved_mount_targets`) -- docker layers overlapping mounts, which either
    buries the tong's wiring or has docker create a mountpoint inside the user's
    workspace on the host. `destination` is where the mount actually lands
    (`mount_destination`), so a default target is judged like a declared one.
    Validation and argv assembly both ask this, so both give the same verdict and
    the same message for one `reserved` map.
    """
    if word not in TARGETED_MOUNT_WORDS:
        if target is not None:
            return ("mount %r: only the %s mounts take a target path"
                    % (mount, " and ".join(repr(w) for w in TARGETED_MOUNT_WORDS)))
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


def overlapping_mount_error(mount, destination, volume, placed):
    """Why a mount collides with one already placed, or None if it is clear.

    `volume` is the name `parse_mount` read from `mount`, and `placed` the
    `(mount, destination, volume)` triples accepted so far. Docker refuses two binds onto one
    destination outright, and creates the inner mountpoint of nested ones inside
    the outer bind -- inside the user's workspace, for a workspace bind. A volume
    mounts only once.
    """
    for other, other_destination, other_volume in placed:
        if _targets_overlap(destination, other_destination):
            return ("mount %r: %s overlaps mount %r at %s"
                    % (mount, destination, other, other_destination))
        if volume is not None and other_volume == volume:
            return "mount %r: volume %r is already mounted by %r" % (mount, volume, other)
    return None


def tong_volume_name(tong_name, volume, scope):
    """The docker named volume behind one tong's `volume:<volume>` mount.

    `swarmforge-volume-[<hint>-]<digest>_<volume>`. The digest, over `scope`
    (from `volume_scope`) and the raw tong name, is the identity; the hint is the
    sanitized name, only for reading in `docker volume ls`.
    """
    scope_class, scope_path = scope
    key = json.dumps([scope_class, scope_path, tong_name])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:VOLUME_DIGEST_LENGTH]
    hint = name_hint(tong_name, VOLUME_HINT_LIMIT)
    parts = [VOLUME_NAME_PREFIX] + ([hint] if hint else []) + [digest]
    return "%s_%s" % ("-".join(parts), volume)


def volume_scope(layer, org_tongs_dir=None, workspace=None):
    """The `(class, path)` scope that names the volumes of a tong from `layer`.

    Org: its tongs directory, canonical as `org_scope_token` reads it.
    Workspace: the checkout's `workspace_key`, like its approvals. User and
    repo: `(LOCAL_VOLUME_SCOPE, None)`. None when the org or workspace directory
    is unknown, which `tong_mount_specs` refuses rather than falling back to the
    local scope. Raises `ValueError` for an unknown layer.
    """
    if layer == ORG:
        return (ORG_VOLUME_SCOPE, canonical_path(org_tongs_dir)) if org_tongs_dir else None
    if layer == WORKSPACE:
        return (WORKSPACE_VOLUME_SCOPE, workspace_key(workspace)) if workspace else None
    if layer in (USER, REPO):
        return (LOCAL_VOLUME_SCOPE, None)
    raise ValueError("unknown layer %r has no volume scope" % (layer,))


def checked_mounts(defn, socket_path=DEFAULT_DOCKER_SOCKET):
    """Each `mounts:` entry judged by every mount rule, as a `CheckedMount`.

    Validation and argv assembly both walk a definition's mounts through here, so
    they accept and refuse the same entries with the same message. An entry is
    refused for not being a string, failing `parse_mount`, or any of
    `mount_target_error`, `mount_mode_error` and `overlapping_mount_error`; a
    refused entry has `error` set (and `word` and `destination` None when it never
    parsed) and is not placed, so later entries are judged for overlap against the
    accepted ones only.
    """
    reserved = reserved_mount_targets(defn, socket_path)
    placed = []                          # (mount, destination, volume) already accepted
    for mount in defn.get("mounts") or []:
        if not isinstance(mount, str):
            yield CheckedMount(mount, None, None, None, None, None,
                               "mount entries must be strings, got %r" % (mount,))
            continue
        try:
            word, volume, target, mode = parse_mount(mount)
            destination = mount_destination(word, target, socket_path)
        except ValueError as exc:
            yield CheckedMount(mount, None, None, None, None, None, str(exc))
            continue
        error = (mount_target_error(mount, word, target, destination, reserved)
                 or mount_mode_error(mount, word, mode)
                 or overlapping_mount_error(mount, destination, volume, placed))
        if not error:
            placed.append((mount, destination, volume))
        yield CheckedMount(mount, word, volume, target, mode, destination, error)


def _accepted_mounts(defn, socket_path):
    """`checked_mounts`, raising `ValueError` at the first refused entry."""
    for checked in checked_mounts(defn, socket_path):
        if checked.error:
            raise ValueError(checked.error)
        yield checked


def tong_mount_specs(defn, workspace, socket_path=DEFAULT_DOCKER_SOCKET,
                     tong_name=None, volume_scope=None, extra_mount_specs=(),
                     tmux_socket_dir=None):
    """Concrete docker `-v` specs for a tong's `mounts:` magic words.

    Returns the list of `-v` *values* (the orchestrator pairs each with a `-v`
    flag). `workspace[:/target][:mode]` mounts the session workspace, at
    /workspace unless the definition names another target; `docker-socket[:mode]`
    bind-mounts the host docker socket onto the same path it has on the host;
    `volume:<name>:/target[:mode]` mounts the named volume
    `tong_volume_name(tong_name, name, volume_scope)`, which docker creates on
    first use and the launcher never removes. A volume may not overlap one of
    `extra_mount_specs` (see `tong_run_argv`), or docker would leave their
    mountpoints inside it. `tmux-socket[:ro]` bind-mounts `tmux_socket_dir` --
    the host directory holding the launcher's tmux socket, from
    `resolve_tmux_server` -- read-only at `TMUX_MOUNT_TARGET`. The directory
    rather than the socket, because tmux deletes and recreates the socket when
    its server restarts.
    Raises `ValueError` for any entry `checked_mounts` refuses, a volume
    overlapping one of `extra_mount_specs`, a `workspace` or `tmux-socket` mount
    when no host source for it is known, or a `volume` mount without a
    `tong_name` and `volume_scope` to name it by -- a definition never names a raw
    host path, so anything else is a mistake that should stop the launch.
    """
    specs = []
    for checked in _accepted_mounts(defn, socket_path):
        mount, word, mode = checked.mount, checked.word, checked.mode
        destination = checked.destination
        if word == WORKSPACE_MOUNT:
            if not workspace:
                raise ValueError("mount 'workspace' requested but no workspace path is known")
            source = workspace
        elif word == SOCKET_MOUNT:
            source = socket_path
        elif word == VOLUME_MOUNT:
            if not tong_name or volume_scope is None:
                raise ValueError(
                    "mount %r needs the tong's name and scope to name its volume" % (mount,))
            source = tong_volume_name(tong_name, checked.volume, volume_scope)
            for extra in extra_mount_specs:
                # Ride-along sources never contain a colon; gitguard refuses those.
                extra_destination = extra.split(":")[1]
                if _targets_overlap(destination, extra_destination):
                    raise ValueError(
                        "mount %r: %s overlaps %s, which is mounted alongside the "
                        "workspace" % (mount, destination, extra_destination))
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
    return specs


def resolve_tmux_server(tmux, tmux_pane=None):
    """The launcher's tmux server as a `tmux-socket` tong reaches it, a `TmuxServer`.

    `tmux` and `tmux_pane` are the launcher's raw `$TMUX` and `$TMUX_PANE`, and
    this is the one place they are parsed. `$TMUX` is
    `<socket-path>,<server-pid>,<session-id>`; the result holds the socket's parent
    directory (what the mount binds), the socket's host path, and the tong's env:
    `TMUX` re-pointed at the socket's name under `TMUX_MOUNT_TARGET`, so a stock
    tmux client in the tong reaches the host server with no flags, and `TMUX_PANE`
    as given (None when it is unset).

    Raises `ValueError` when `tmux` is unset, or unless it has exactly three
    comma-separated fields, the pid and session id are decimal, and the socket path
    is a normalized absolute path with no `:` (docker's `-v` separator), `,` (tmux
    reads the path up to the first one), or whitespace. The socket must also sit
    directly in tmux's own per-user directory (`tmux-<uid>`): binding a `-S`
    socket's parent could hand the tong an arbitrary host directory, such as a home
    directory. A set `tmux_pane` must be a pane id (`%<digits>`).
    """
    if not tmux:
        raise ValueError("the launcher is not running inside tmux ($TMUX is unset)")
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
    if tmux_pane and not (isinstance(tmux_pane, str) and _TMUX_PANE_RE.fullmatch(tmux_pane)):
        raise ValueError("$TMUX_PANE %r is not a tmux pane id ('%%<number>')" % (tmux_pane,))
    container_tmux = "%s/%s,%s,%s" % (TMUX_MOUNT_TARGET, socket_name, pid, session)
    return TmuxServer(host_dir, path, {"TMUX": container_tmux, "TMUX_PANE": tmux_pane or None})


def tmux_secret_env_error(defn):
    """Why a `tmux-socket` definition's secret env is refused, or None if it is fine.

    The launcher owns `TMUX_LAUNCHER_ENV` on a tong that mounts `tmux-socket` and
    replaces a plain value the definition gives one of them, but a secret value is
    exported by the FIFO wrapper after docker's `-e` values and so would win.
    Refusing a secret reference there keeps the launcher's values the ones the
    tong runs with.
    """
    env = defn.get("env")
    if not mounts_word(defn, TMUX_MOUNT) or not isinstance(env, dict):
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
    definition mounts no workspace. Raises `ValueError` for a refused
    entry, like `tong_mount_specs`.
    """
    return [(checked.destination, checked.mode)
            for checked in _accepted_mounts(defn, socket_path)
            if checked.word == WORKSPACE_MOUNT]
