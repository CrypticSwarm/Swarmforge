"""The `mounts:` magic words and the docker `-v` specs they turn into.

A definition never names a raw host path: it asks for a mount by word, and this
module decides where that lands inside the container and what `-v` value docker
is given: a host path the launcher knows, or a named volume it names. Validation
and argv assembly both come through here, so a mount is judged by the same rules
whichever asks.
"""

import hashlib
import json
import posixpath
import re

from swarmforge.names import canonical_path, name_hint

from .model import ORG, REPO, SOCKET_MOUNT, USER, WORKSPACE, workspace_key
from .secrets import SECRET_FIFO_DIR, SECRET_INJECT_SHELL, partition_secret_env


WORKSPACE_MOUNT = "workspace"
VOLUME_MOUNT = "volume"
MOUNT_WORDS = (WORKSPACE_MOUNT, SOCKET_MOUNT, VOLUME_MOUNT)
TARGETED_MOUNT_WORDS = (WORKSPACE_MOUNT, VOLUME_MOUNT)
DEFAULT_WORKSPACE_MOUNT_TARGET = "/workspace"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"

# An allowlist, so an unvetted mount option cannot reach the daemon.
MOUNT_MODES = ("ro", "rw")

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
    is spelled `volume:<name>:/target[:mode]`: its name comes first and its target
    is required, since a named volume has no natural place to land. `volume` is
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
    `docker-socket` mount. `mount_target_error`
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
    return paths


def mount_destination(word, target, socket_path=DEFAULT_DOCKER_SOCKET):
    """Where a mount lands inside the container, normalized.

    The declared target when there is one, otherwise the word's default: /workspace
    for the workspace, its own host path for the socket (that is where a docker
    client looks for it). A volume has no default, so it needs its target. Raises
    `ValueError` for a targetless volume and for any other word, so a magic word
    added without a default cannot inherit the socket's.
    """
    if word == WORKSPACE_MOUNT:
        return normalize_mount_target(target or DEFAULT_WORKSPACE_MOUNT_TARGET)
    if word == SOCKET_MOUNT:
        return normalize_mount_target(socket_path)
    if word == VOLUME_MOUNT and target is not None:
        return normalize_mount_target(target)
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
        # The socket lands on its own reserved path by construction.
        return None
    for path, why in sorted(reserved.items()):
        if _targets_overlap(destination, path):
            return "mount %r: %s overlaps %s, %s" % (mount, destination, path, why)
    return None


def overlapping_mount_error(mount, destination, volume, placed):
    """Why a mount collides with one already placed, or None if it is clear.

    `volume` is the name `parse_mount` read from `mount`, and `placed` the
    `(mount, destination, volume)` triples accepted so far. Docker refuses two binds onto one
    destination outright, and creates the inner mountpoint of nested ones inside
    the outer bind -- inside the user's workspace, for a workspace bind. One volume
    is mounted once: a second target for the same data, perhaps with another
    access mode, is far likelier a mistake than a need.
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

    `swarmforge-volume-[<hint>-]<digest>_<volume>`. The digest is the volume's
    identity: the first `VOLUME_DIGEST_LENGTH` hex digits of a SHA-256 over the
    scope and the raw tong name, where `scope` is a `(class, path)` pair -- an
    `ORG_VOLUME_SCOPE` or `WORKSPACE_VOLUME_SCOPE` with its canonical directory,
    or `(LOCAL_VOLUME_SCOPE, None)` for this machine's own layers. Two tongs
    share a volume only from one scope under exactly one name. The hint is the
    tong name sanitized for docker and cut to `VOLUME_HINT_LIMIT`, there to be
    read in `docker volume ls`; it decides nothing, since sanitizing can turn two
    names into one. Volume names
    exclude `_` and the digest has a fixed width, so the last `_` splits the
    volume back off.
    """
    scope_class, scope_path = scope
    key = json.dumps([scope_class, scope_path, tong_name])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:VOLUME_DIGEST_LENGTH]
    hint = name_hint(tong_name, VOLUME_HINT_LIMIT)
    parts = [VOLUME_NAME_PREFIX] + ([hint] if hint else []) + [digest]
    return "%s_%s" % ("-".join(parts), volume)


def volume_scope(layer, org_tongs_dir=None, workspace=None):
    """The `(class, path)` scope that names the volumes of a tong from `layer`.

    An org tong's volumes belong to its org's tongs directory (canonical, as
    `org_scope_token` reads it); a workspace tong's to the checkout's top level
    `workspace`, keyed by `workspace_key` like its approvals, so every session in
    that checkout, from any subdirectory, shares them -- as does any later
    checkout at that path. The user and repo layers are this machine's own
    and share `(LOCAL_VOLUME_SCOPE, None)`, so a repo tong and the same-named
    user tong it replaces in the merge use one volume. None for an org or
    workspace tong with no directory to scope it by, which `tong_mount_specs`
    refuses rather than falling back to the local scope. Raises `ValueError` for
    an unknown layer.
    """
    if layer == ORG:
        return (ORG_VOLUME_SCOPE, canonical_path(org_tongs_dir)) if org_tongs_dir else None
    if layer == WORKSPACE:
        return (WORKSPACE_VOLUME_SCOPE, workspace_key(workspace)) if workspace else None
    if layer in (USER, REPO):
        return (LOCAL_VOLUME_SCOPE, None)
    raise ValueError("unknown layer %r has no volume scope" % (layer,))


def tong_mount_specs(defn, workspace, socket_path=DEFAULT_DOCKER_SOCKET,
                     tong_name=None, volume_scope=None):
    """Concrete docker `-v` specs for a tong's `mounts:` magic words.

    Returns the list of `-v` *values* (the orchestrator pairs each with a `-v`
    flag). `workspace[:/target][:mode]` mounts the session workspace, at
    /workspace unless the definition names another target; `docker-socket[:mode]`
    bind-mounts the host docker socket onto the same path it has on the host;
    `volume:<name>:/target[:mode]` mounts the named volume
    `tong_volume_name(tong_name, name, volume_scope)`, which docker creates on
    first use and never removes with the container.
    Raises `ValueError` for a non-string entry, a malformed mount, an unusable or
    colliding destination, a `workspace` mount when no workspace path is known, or
    a `volume` mount without a `tong_name` and `volume_scope` to name it by -- a
    definition never names a raw host path, so anything else is a mistake that
    should stop the launch.
    """
    specs = []
    reserved = reserved_mount_targets(defn, socket_path)
    placed = []                          # (mount, destination, volume) already emitted
    for mount in defn.get("mounts") or []:
        if not isinstance(mount, str):
            raise ValueError("mount entries must be strings, got %r" % (mount,))
        word, volume, target, mode = parse_mount(mount)
        destination = mount_destination(word, target, socket_path)
        reason = (mount_target_error(mount, word, target, destination, reserved)
                  or overlapping_mount_error(mount, destination, volume, placed))
        if reason:
            raise ValueError(reason)
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
            source = tong_volume_name(tong_name, volume, volume_scope)
        else:
            # Unreachable via `parse_mount`; a new word must not inherit the socket bind.
            raise ValueError("mount %r has no docker spec" % (mount,))
        spec = "%s:%s" % (source, destination)
        if mode:
            spec += ":" + mode
        specs.append(spec)
        placed.append((mount, destination, volume))
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
        word, _, target, mode = parse_mount(mount)
        if word == WORKSPACE_MOUNT:
            placements.append((mount_destination(word, target, socket_path), mode))
    return placements
