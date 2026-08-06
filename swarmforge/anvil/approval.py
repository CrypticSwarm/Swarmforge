"""The first-run approval gate for workspace-sourced tongs.

The user, org, and Swarmforge-repo layers are installed deliberately and are
trusted. The workspace is any repo you happened to clone, so a workspace-sourced
tong -- which may request secrets, host mounts, or the docker socket -- is gated:
its privilege summary is rendered and the user is asked to approve it before the
anvil starts. Approval is keyed by workspace path, tong name, and a hash of the
merged definition, so any change re-prompts, and persists in the user layer.

Deciding what a definition asks for and remembering the answer are the pure core's
job; rendering the question and reading the reply are this module's.
"""

import sys

from swarmforge import tongs


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
