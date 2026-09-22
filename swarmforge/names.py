"""Docker names derived from a host directory.

A name has to read in `docker ps` and be unique to the directory it names, so a
token is a readable hint plus a short digest of the canonical path. The digest
carries the uniqueness -- a basename does not, since the worktree layout gives
every repository a `master` -- and both halves are stable, because a name a user
cannot reproduce is a session they cannot stop.

A leaf: nothing in the package is below it, and both sides of the container
boundary may import it.
"""

import hashlib
import os
import re
import sys


# Everything docker refuses in a container or network name.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")

# Collision-free over the directories one machine holds.
DIGEST_LENGTH = 8

# Characters of readable hint a project token keeps. Docker registers a session
# tong's `<harness>-<hint>-<digest>-tong-<tong>` as a DNS label, capped at 63
# and truncated by nothing downstream; this cut holds the container name to 42
# and leaves 21 for `-tong-<tong>`.
PROJECT_HINT_LIMIT = 24


def sanitize_token(name):
    """`name` reduced to docker's characters, runs collapsed and ends trimmed."""
    return _UNSAFE.sub("-", name).strip("-_.")


def canonical_path(path):
    """`path` absolute and normalized, so two spellings give one token.

    Symlinks are left to the caller; `project_token` resolves them.
    """
    return os.path.normpath(os.path.abspath(path))


def path_digest(path):
    """Short hex digest of `path` canonicalized: the unique half of a token.

    Hashed as filesystem bytes, so a path that is not valid UTF-8 digests
    rather than raising.
    """
    digest = hashlib.sha256(os.fsencode(canonical_path(path)))
    return digest.hexdigest()[:DIGEST_LENGTH]


def path_token(path, hint_from=None, hint_limit=None):
    """Docker-name token for one host directory: readable hint, then digest.

    The hint is the basename of `hint_from`, or of `path` itself when no other
    directory is named -- a caller passes one when the readable name sits
    somewhere other than at the path being identified -- and is cut to
    `hint_limit` characters when one is given. A hint that sanitizes away to
    nothing is dropped, leaving the digest, which is a valid name on its own.
    """
    hint_path = path if hint_from is None else hint_from
    hint = sanitize_token(os.path.basename(canonical_path(hint_path)))
    if hint_limit is not None:
        hint = hint[:hint_limit].rstrip("-_.")
    digest = path_digest(path)
    return "%s-%s" % (hint, digest) if hint else digest


def project_token(project_dir):
    """The token the Makefile names a session's container with.

    Symlinks are resolved, so a link and the directory it points at are one
    session rather than two container names over one workspace: `make` reads
    `PROJECT_DIR` from `getcwd()` while a shell alias passes the logical
    `$(pwd)`, and the two spell the same directory differently.
    """
    return path_token(
        os.path.realpath(project_dir), hint_limit=PROJECT_HINT_LIMIT)


def main(argv):
    """Print the token naming one project directory, for the Makefile.

    A command rather than something the launcher hands back, because make needs
    the token while it is still reading variables. Naming no directory is an
    error rather than a default, which make would bind silently.
    """
    if len(argv) != 1 or not argv[0].strip():
        sys.stderr.write("usage: project-name <project-dir>\n")
        return 2
    sys.stdout.write("%s\n" % project_token(argv[0]))
    return 0
