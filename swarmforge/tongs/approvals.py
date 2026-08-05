"""The config hash, the privileges a definition asks for, and approval keying.

Only a workspace-sourced tong -- one that came from a repo you happened to clone
-- gates on approval. The gate needs three things: a stable hash of the
definition, a summary of what it asks for so a reviewer can judge it, and a
store that remembers the answer per workspace and tong. Rendering the summary
and prompting are the caller's job.
"""

import hashlib
import json
import os

from .model import WORKSPACE
from .mounts import _has_socket_mount
from .secrets import find_secret_refs


# --- Config hash --------------------------------------------------------------


def config_hash(defn):
    """Stable SHA-256 hex digest of a definition.

    Canonical JSON (sorted keys) makes the hash independent of mapping order.
    The same function serves two callers: the approval hash is taken over the
    merged definition before secret resolution, and the staleness label hash
    over the resolved definition. Callers choose the input.
    """
    canonical = json.dumps(defn, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Privilege summary --------------------------------------------------------


def privilege_summary(defn):
    """Structured summary of what a definition asks for, for the approval gate.

    Gathers the privileges a reviewer must see before approving a
    workspace-sourced tong: image, secret references, mounts, networks, and
    docker-socket access. Rendering and prompting are the caller's job; this
    just assembles the facts.
    """
    return {
        "image": defn.get("image"),
        "secrets": [{"provider": p, "ref": r} for p, r in find_secret_refs(defn)],
        "mounts": list(defn.get("mounts") or []),
        "networks": list(defn.get("networks") or []),
        "socket": _has_socket_mount(defn),
    }


# --- Approval keying ----------------------------------------------------------
# Approvals are keyed by workspace path + tong name + definition hash and stored
# in the user layer (~/.swarmforge/approvals.json). Any change to the definition
# changes its hash and re-prompts. Only workspace-sourced tongs gate.


def is_workspace_sourced(source_layer):
    """True if a tong's winning layer is the (untrusted) workspace and so gates."""
    return source_layer == WORKSPACE


def load_approvals(path):
    """Load the approvals store, returning {} when it is absent or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_approvals(path, approvals):
    """Persist the approvals store as pretty JSON, creating parent dirs."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(approvals, handle, indent=2, sort_keys=True)
        handle.write("\n")


def is_approved(approvals, workspace_path, name, defn):
    """True if `defn` (by its config hash) is approved for this workspace+tong.

    Fails closed (returns False) on a missing or malformed store entry rather
    than raising -- a hand-edited approvals.json must never crash the gate.
    """
    entry = approvals.get(workspace_path)
    if not isinstance(entry, dict):
        return False
    return entry.get(name) == config_hash(defn)


def record_approval(approvals, workspace_path, name, defn):
    """Return `approvals` updated to approve `defn` for this workspace+tong.

    Mutates and returns the store (same object) so callers can persist it.
    """
    approvals.setdefault(workspace_path, {})[name] = config_hash(defn)
    return approvals
