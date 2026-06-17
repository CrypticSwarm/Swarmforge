#!/usr/bin/env python3
"""Host-side launcher that wraps an anvil (harness container) run.

The Makefile resolves the docker-run argv for an anvil (`run_opencode` /
`run_claude`) and the host paths of the four tong definition layers, then
delegates the actual launch to this script:

    run_anvil.py [--user-tongs DIR] [--org-tongs DIR] [--repo-tongs DIR]
                 [--workspace-tongs DIR] [--workspace PATH] [--approvals PATH]
                 [--no-prompt] -- docker run -it --rm ... <image> ...

Tongs are sibling containers that must be orchestrated from the host (they are
started alongside the anvil, not from inside it), which is why this wrapper sits
between Make and `docker run`. It discovers tong definitions across the four
layers using the pure core in `tongs.py`, then runs the anvil.

First-run approval
------------------
The user, org, and Swarmforge-repo layers are installed deliberately and are
trusted. The workspace is any repo you happened to clone, so a workspace-sourced
tong -- which may request secrets, host mounts, or the docker socket -- is gated:
before the anvil starts, the launcher prints the privilege summary and asks the
user to approve it. Approval is keyed by workspace path + tong name + a hash of
the merged definition (so any change re-prompts) and persists in the user-layer
store passed as `--approvals`. The scripted `--no-prompt` mode fails closed
(refusing the run) rather than auto-approving an unapproved tong.

Passthrough invariant
---------------------
With **no tong definitions discovered across all four layers**, the launcher
execs the anvil argv verbatim -- byte-identical to the direct `docker run` Make
would otherwise have issued. Existing repos ship no tong layers, so discovery is
empty, the approval gate sees nothing to gate, and this wrapper is a transparent
exec. `scripts/test_run_anvil.py` asserts this byte-for-byte.

The anvil argv (everything after `--`) is forwarded to `os.execvp` unchanged, so
the anvil process replaces this one and keeps the controlling tty, signal
delivery, and `--rm` cleanup it had before.
"""

import collections
import importlib.util
import os
import sys

# Load the pure core (layer discovery + name-based merge) by path, the same way
# tongs.py loads translate_agents.py, so the launcher needs no package install
# and no assumptions about the current working directory.
_TONGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tongs.py")
_spec = importlib.util.spec_from_file_location("tongs", _TONGS_PATH)
tongs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tongs)


USAGE = (
    "usage: run_anvil.py [--user-tongs DIR] [--org-tongs DIR] "
    "[--repo-tongs DIR] [--workspace-tongs DIR] [--workspace PATH] "
    "[--approvals PATH] [--no-prompt] -- <anvil command>"
)

# Each flag names the host directory for one definition layer. The merge always
# orders layers canonically (LAYERS, lowest to highest precedence) regardless of
# the order the flags are passed.
LAYER_FLAGS = {
    "--user-tongs": tongs.USER,
    "--org-tongs": tongs.ORG,
    "--repo-tongs": tongs.REPO,
    "--workspace-tongs": tongs.WORKSPACE,
}

# Parsed launcher options. `workspace` is the workspace root used to key approval
# of workspace-sourced tongs; `approvals` is the store path (default resolved in
# main); `no_prompt` makes the approval gate fail closed for scripted runs.
LauncherOptions = collections.namedtuple(
    "LauncherOptions", ["layer_dirs", "workspace", "approvals", "no_prompt"]
)


class UsageError(ValueError):
    """Raised for malformed launcher arguments (reported, then exit 2)."""


def parse_args(argv):
    """Split launcher options from the anvil command at the first ``--``.

    Returns ``(options, anvil_cmd)`` where ``options`` is a ``LauncherOptions``
    (its ``layer_dirs`` ordered by canonical precedence, only the layers that
    were given) and ``anvil_cmd`` is the argv after ``--``. Raises ``UsageError``
    if the separator is missing, the command is empty, or an option is malformed.
    """
    paths = {}
    workspace = None
    approvals = None
    no_prompt = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            anvil_cmd = list(argv[index + 1:])
            if not anvil_cmd:
                raise UsageError("missing anvil command after '--'")
            layer_dirs = [(layer, paths[layer]) for layer in tongs.LAYERS if layer in paths]
            return LauncherOptions(layer_dirs, workspace, approvals, no_prompt), anvil_cmd
        if token in LAYER_FLAGS:
            if index + 1 >= len(argv):
                raise UsageError("%s requires a directory argument" % token)
            paths[LAYER_FLAGS[token]] = argv[index + 1]
            index += 2
            continue
        if token == "--workspace":
            if index + 1 >= len(argv):
                raise UsageError("--workspace requires a path argument")
            workspace = argv[index + 1]
            index += 2
            continue
        if token == "--approvals":
            if index + 1 >= len(argv):
                raise UsageError("--approvals requires a path argument")
            approvals = argv[index + 1]
            index += 2
            continue
        if token == "--no-prompt":
            no_prompt = True
            index += 1
            continue
        raise UsageError("unexpected argument %r" % token)
    raise UsageError("missing '--' separating launcher options from the anvil command")


def default_approvals_path():
    """Path to the approvals store in the user layer when none is passed.

    Mirrors the Makefile's `SWARMFORGE_USER_ASSETS_DIR` default (~/.swarmforge),
    so the launcher and Make agree on where approvals live even if Make does not
    pass `--approvals` explicitly.
    """
    base = os.environ.get("SWARMFORGE_USER_ASSETS_DIR") or os.path.join(
        os.path.expanduser("~"), ".swarmforge"
    )
    return os.path.join(base, "approvals.json")


def discover_tongs(layer_dirs):
    """Merged tong set across the given layers ({} when none are present)."""
    return tongs.merge_tongs(tongs.discover(layer_dirs))


# --- First-run approval -------------------------------------------------------


class ApprovalDenied(Exception):
    """A workspace-sourced tong was not approved; the launch must not proceed."""


def render_privilege_summary(name, summary):
    """Human-readable block describing what a workspace tong requests.

    `summary` is the structured output of `tongs.privilege_summary`. Only the
    privileges actually requested are shown, and docker-socket access -- the
    broadest grant, since it is full control of the host's docker -- is always
    called out explicitly so it cannot be approved unseen.
    """
    lines = ["Workspace tong %r requests approval:" % name]
    lines.append("  image:    %s" % (summary.get("image") or "(none declared)"))
    secrets = summary.get("secrets") or []
    if secrets:
        rendered = ", ".join("%s:%s" % (s["provider"], s["ref"]) for s in secrets)
        lines.append("  secrets:  %s" % rendered)
    mounts = summary.get("mounts") or []
    if mounts:
        lines.append("  mounts:   %s" % ", ".join(str(m) for m in mounts))
    networks = summary.get("networks") or []
    if networks:
        lines.append("  networks: %s" % ", ".join(str(n) for n in networks))
    if summary.get("socket"):
        lines.append("  docker socket: full host docker control")
    return "\n".join(lines)


def _prompt_yes_no(question, out, inp):
    """Ask a yes/no question on `out`/`inp`, defaulting to No.

    EOF (a closed or non-interactive stdin) reads as No, so a gate that cannot
    actually ask the user never silently approves.
    """
    out.write("%s [y/N]: " % question)
    out.flush()
    answer = inp.readline()
    if not answer:
        out.write("\n")
        return False
    return answer.strip().lower() in ("y", "yes")


def gate_workspace_tongs(merged, workspace, approvals_path, prompt=True, out=None, inp=None):
    """Gate every workspace-sourced tong on first-run approval.

    The user/org/repo layers are trusted and skip the gate; only the workspace
    layer (any repo you happened to clone) is gated. For each workspace tong that
    is not already approved, the privilege summary is printed and the user is
    asked to approve it. Approval is keyed by workspace path + tong name + a hash
    of the merged definition, so any change to the definition re-prompts; newly
    granted approvals are persisted to `approvals_path` (the user layer). The
    `workspace` key is the checkout root, so a git worktree (which has its own
    root) re-approves rather than inheriting another checkout's approval.

    With no workspace-sourced tongs the gate is a no-op, which is what keeps a
    launch with zero (or only trusted) tongs byte-identical to a direct docker
    run.

    Raises `ApprovalDenied` -- the launch must not proceed -- when a workspace
    tong is unapproved and the user declines, when `prompt` is False (the
    scripted `--no-prompt` posture fails closed rather than auto-approving), or
    when there is no workspace path to key the approval by.
    """
    out = sys.stderr if out is None else out
    inp = sys.stdin if inp is None else inp

    pending = [
        (name, merged[name]["definition"])
        for name in sorted(merged)
        if tongs.is_workspace_sourced(merged[name]["source"])
    ]
    if not pending:
        return

    if not workspace:
        raise ApprovalDenied(
            "refusing to evaluate workspace tong approval without a workspace path"
        )

    approvals = tongs.load_approvals(approvals_path)
    recorded = False
    for name, defn in pending:
        if tongs.is_approved(approvals, workspace, name, defn):
            continue
        out.write(render_privilege_summary(name, tongs.privilege_summary(defn)) + "\n")
        if not prompt:
            raise ApprovalDenied(
                "workspace tong %r is unapproved and --no-prompt fails closed" % name
            )
        if not _prompt_yes_no("Approve workspace tong %r?" % name, out, inp):
            raise ApprovalDenied("workspace tong %r was not approved" % name)
        tongs.record_approval(approvals, workspace, name, defn)
        recorded = True

    if recorded:
        tongs.save_approvals(approvals_path, approvals)


def exec_anvil(anvil_cmd):
    """Exec the anvil argv, replacing this process.

    On success this never returns. If the command cannot be execed (e.g. the
    binary is missing from PATH), report it and return 127 -- the shell's
    convention for an uninvocable command -- rather than surfacing a traceback.
    """
    try:
        os.execvp(anvil_cmd[0], anvil_cmd)
    except OSError as exc:
        tongs.warn("cannot exec %r: %s" % (anvil_cmd[0], exc))
        return 127


def main(argv):
    try:
        opts, anvil_cmd = parse_args(argv)
    except UsageError as exc:
        tongs.warn(str(exc))
        tongs.warn(USAGE)
        return 2

    merged = discover_tongs(opts.layer_dirs)

    # Gate workspace-sourced tongs before anything else runs. With none present
    # (the common case) this is a no-op and the launch is unchanged; otherwise an
    # unapproved or declined workspace tong stops the launch before the anvil.
    try:
        gate_workspace_tongs(
            merged,
            opts.workspace,
            opts.approvals or default_approvals_path(),
            prompt=not opts.no_prompt,
        )
    except ApprovalDenied as exc:
        tongs.warn(str(exc))
        return 1

    if merged:
        # The launcher discovers tongs but does not start them; the anvil runs
        # without them. The passthrough invariant only governs the empty case,
        # so surface the discovered tongs rather than ignoring them silently.
        tongs.warn(
            "%d tong definition(s) discovered (%s); this launcher does not "
            "start tongs, so the anvil runs without them"
            % (len(merged), ", ".join(sorted(merged)))
        )

    # On success exec_anvil replaces this process; it only returns a status if
    # the anvil command could not be execed.
    return exec_anvil(anvil_cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
