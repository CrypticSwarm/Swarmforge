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

# Characters of repository and of worktree the readable half of a project token
# keeps. Docker registers a session tong's `<harness>-<hint>-<digest>-tong-<tong>`
# as a DNS label, capped at 63 and truncated by nothing downstream; 12 + 1 + 11
# holds the container name to 42 and leaves 21 for `-tong-<tong>`.
REPO_HINT_LIMIT = 12
WORKTREE_HINT_LIMIT = 11


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


def dir_hint(path, limit=None):
    """`path`'s basename as a docker name, cut to `limit` and not left on a separator."""
    hint = sanitize_token(os.path.basename(canonical_path(path)))
    return hint[:limit].rstrip("-_.") if limit is not None else hint


def path_token(path, hint):
    """Docker-name token for one host directory: `hint`, then `path`'s digest.

    An empty `hint` leaves the digest, which is a valid name by itself.
    """
    digest = path_digest(path)
    return "%s-%s" % (hint, digest) if hint else digest


def holds_a_repository(path):
    """Whether a git repository lives directly in `path`, as a checkout or a bare root."""
    return os.path.exists(os.path.join(path, ".git"))


def project_token(project_dir):
    """The token the Makefile names a session's container with: repository, then worktree.

    The repository is prefixed only when the parent directory holds it -- true
    of a worktree beside its bare root and of a subdirectory of a checkout, not
    of a repository root, whose parent is wherever the user keeps their code.
    Symlinks are resolved, so a link and its target are one session.
    """
    resolved = os.path.realpath(project_dir)
    parent = os.path.dirname(resolved)
    hints = [dir_hint(resolved, WORKTREE_HINT_LIMIT)]
    if parent != resolved and holds_a_repository(parent):
        hints.insert(0, dir_hint(parent, REPO_HINT_LIMIT))
    return path_token(resolved, "-".join(hint for hint in hints if hint))


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
