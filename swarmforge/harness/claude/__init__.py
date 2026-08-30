"""The Claude Code harness."""

import os
import shutil
import sys

from swarmforge.agents.emit import OPENCODE_ONLY_FIELDS, render, warn
from swarmforge.config import merge_json
from swarmforge.harness.spec import HarnessSpec, Waiver

# Derived from the layers on every run and never read as an input. It stays
# off the persistent home -- one directory shared by every container for this
# user, where it would carry an org layer's permissions, hooks, and env into
# later runs that do not mount that layer -- and rides claude's command line
# instead, delivered by the entrypoint's exec.
SETTINGS_FILE = "/run/swarmforge/claude-settings.json"

# The image's own defaults, and the bottom settings layer: any higher and the
# image would overrule a key a session chose. This is the path claude's
# image.sh installs to.
IMAGE_DEFAULT_SETTINGS = "/usr/local/share/swarmforge/claude-settings.json"

# Holds the git wrapper the root phase installs when the workspace needs one.
# The entrypoint puts this directory ahead of the real git on PATH exactly when
# the wrapper is standing there.
WRAPPER_DIR = "/usr/local/libexec/swarmforge"

# The wrapper's text, given the real git, the worktree path recorded on the
# host, and the directory the same checkout is mounted at in the container.
GIT_WRAPPER = """\
#!/bin/sh
# Swarmforge git wrapper: rewrite worktree paths for container compatibility.
case "$*" in
  *worktree*list*--porcelain*)
    "%(git)s" "$@" | sed "s|^worktree %(host)s$|worktree %(workspace)s|"
    ;;
  *)
    exec "%(git)s" "$@"
    ;;
esac
"""

# State only: nothing claude loads as configuration or code belongs here.
STATE_DIRS = (
    "projects",
    "sessions",
    "file-history",
    "session-env",
    "shell-snapshots",
    "plans",
    "tasks",
    "todos",
    "backups",
    "cache",
    "paste-cache",
    "plugins",
)
STATE_FILES = (
    "history.jsonl",
    "stats-cache.json",
    "keybindings.json",
    ".last-cleanup",
    "scheduled_tasks.lock",
)

# OpenCode tool id -> Claude Code tool name. Ids mapping to None have no
# Claude equivalent and are dropped.
CLAUDE_TOOL_NAMES = {
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "list": None,
    "patch": None,
    "skill": "Skill",
    "task": "Task",
    "todoread": None,
    "todowrite": "TodoWrite",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
}


def _override_keys():
    # Imported at call time: the registry package imports this module while
    # building the harness table, so the table does not exist yet at import.
    from swarmforge import harness

    return harness.agent_override_keys()


def to_claude(name, meta):
    if meta.get("disable") is True:
        return None
    out = {"name": meta.get("name", name)}
    if "description" in meta:
        out["description"] = meta["description"]
    else:
        warn("agent '%s' has no description" % name)

    model = meta.get("model")
    if model is not None:
        provider, sep, model_id = str(model).partition("/")
        if not sep:
            out["model"] = model
        elif provider == "anthropic":
            out["model"] = model_id

    tools = meta.get("tools")
    if isinstance(tools, dict):
        disallowed = []
        for tool, enabled in tools.items():
            if enabled is not False:
                continue
            mapped = CLAUDE_TOOL_NAMES.get(tool)
            if mapped is None:
                if tool not in CLAUDE_TOOL_NAMES:
                    warn("agent '%s': unknown tool '%s' skipped" % (name, tool))
                continue
            disallowed.append(mapped)
        if disallowed:
            out["disallowedTools"] = ", ".join(disallowed)
    elif tools is not None:
        warn("agent '%s': 'tools' must be a map of tool -> bool" % name)

    skipped = OPENCODE_ONLY_FIELDS | _override_keys() | {"name", "description", "model"}
    for key, value in meta.items():
        if key not in skipped:
            out[key] = value

    overrides = meta.get("claude")
    if isinstance(overrides, dict):
        out.update(overrides)
    return out


def agent_emitter(name, meta, body):
    """The native filename and full file text for one agent, or None to skip it."""
    out = to_claude(name, meta)
    if out is None:
        return None
    return "%s.md" % name, render(out, body)


def mcp_fragment(servers):
    """Claude Code `--mcp-config` document for the given servers.

    HTTP MCP servers keyed by canonical alias under `mcpServers`, the shape
    Claude reads from the file passed as `claude --mcp-config <path>`. Returns
    `{}` when `servers` is empty.
    """
    out = {alias: {"type": "http", "url": url} for alias, url in servers.items()}
    return {"mcpServers": out} if out else {}


def finalize_config(ctx):
    """Build the settings file claude is handed on its command line.

    Settings are built repo -> user -> org above the image defaults. A failed
    build must still leave valid JSON at the path the exec names, and an empty
    object is the safe reading of "no layer could be applied".
    """
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    # A path is passed for every layer whether or not it exists, which is the
    # normal case merge_json.build_file skips over; an empty layer contributes
    # "/settings.json", which is no more present than the rest.
    sources = [IMAGE_DEFAULT_SETTINGS] + [
        src + "/settings.json"
        for src in (ctx.config_repo_src, ctx.config_user_src, ctx.config_org_src)
    ]
    try:
        merge_json.build_file(SETTINGS_FILE, sources)
    except Exception:
        print(
            "Warning: could not build Claude settings.json; continuing",
            file=sys.stderr,
        )
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        except OSError:
            pass


def link_entry(target, path):
    """Point `path` at `target`, replacing whatever stands there.

    A directory is removed whole, and anything else -- a file, a symlink,
    dangling or not -- is unlinked. A symlink to a directory is unlinked
    rather than emptied: the entry owns the link, never what it points at.
    """
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)
    os.symlink(target, path)


def link_state(ctx):
    """Link the state that outlives the run into the config destination.

    Only the allowlisted state survives: claude loads configuration and code
    out of this dir, and a shared one would hand a session's writes to the
    next container. A link holds only what claude writes in place -- an entry
    rewritten by rename replaces it. A directory must exist in the shared home
    before it is linked, or claude's own mkdir fails on the link. This runs
    after the config merge, which wipes this destination under
    SWARMFORGE_CONFIG_RESET.

    A link that cannot be made stops the container: a session started without
    it writes its history into a directory that dies with the run.
    """
    shared = ctx.home + "/.claude"
    os.makedirs(ctx.config_dest, exist_ok=True)

    # The one piece of state claude keeps beside its config dir, not inside it.
    link_entry(ctx.home + "/.claude.json",
               os.path.join(ctx.config_dest, ".claude.json"))

    for entry in STATE_DIRS:
        try:
            os.makedirs(shared + "/" + entry, exist_ok=True)
        except OSError:
            # Something already stands at that name, a file among them; the
            # entry is linked to it either way.
            pass
        link_entry(shared + "/" + entry,
                   os.path.join(ctx.config_dest, entry))

    for entry in STATE_FILES:
        # The link dangles until claude writes the file, which is what an
        # untouched piece of state looks like.
        link_entry(shared + "/" + entry,
                   os.path.join(ctx.config_dest, entry))


def worktree_pointer(dotgit):
    """The git directory `dotgit` points at, empty when it points at none.

    A linked worktree carries a `.git` file naming its administrative
    directory. A regular checkout has a `.git` directory instead, and a file
    holding anything else names nothing.
    """
    if not os.path.isfile(dotgit):
        return ""
    with open(dotgit, "r", encoding="utf-8") as handle:
        text = handle.read()
    prefix = "gitdir:"
    found = [
        line[len(prefix):].lstrip(" ")
        for line in text.split("\n")
        if line.startswith(prefix)
    ]
    return "\n".join(found).rstrip("\n")


def root_setup(ctx):
    """Install a git wrapper that rewrites the workspace's worktree paths.

    Claude Code's /resume discovers sessions by running `git worktree list
    --porcelain` and matching the paths it prints against the project
    directories under ~/.claude/projects. A workspace that is a linked
    worktree of a bare repo carries worktree metadata recording the HOST path
    of the checkout, which does not exist inside the container, so the
    CWD-match finds nothing and /resume reports "No conversations found to
    resume".

    The wrapper stands in front of the real git and rewrites that one host
    path to the directory the same checkout is mounted at, in the output of
    that one command. It is written only when the workspace is a linked
    worktree whose recorded path differs from the container's, so a plain
    checkout and a worktree mounted at its host path both run the real git
    untouched. A missing git is fatal: the session's harness cannot work
    without one.
    """
    workspace = ctx.cwd
    dotgit = workspace + "/.git"

    gitdir_ptr = worktree_pointer(dotgit)
    if not gitdir_ptr:
        return

    # The administrative directory points back at the worktree's own .git,
    # which is where the host path is recorded.
    reverse_file = gitdir_ptr + "/gitdir"
    if not os.path.isfile(reverse_file):
        return

    with open(reverse_file, "r", encoding="utf-8") as handle:
        host_dotgit = handle.read().rstrip("\n")
    host_worktree = os.path.dirname(host_dotgit)
    if host_worktree == workspace:
        return

    real_git = shutil.which("git")
    if real_git is None:
        raise FileNotFoundError("git not found on PATH")

    os.makedirs(WRAPPER_DIR, exist_ok=True)
    path = WRAPPER_DIR + "/git"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(GIT_WRAPPER % {
            "git": real_git,
            "host": host_worktree,
            "workspace": workspace,
        })

    # `chmod +x`: the execute bits the umask in force allows, added to
    # whatever the file already carries.
    umask = os.umask(0o022)
    os.umask(umask)
    os.chmod(path, (os.stat(path).st_mode & 0o7777) | (0o111 & ~umask))


def pre_exec(ctx, argv, env):
    """Shape the argv and environment claude is exec'd with.

    The wrapper directory goes ahead of the real git on PATH exactly when the
    root phase left a wrapper standing there; that placement is what turns the
    rewrite on.

    The settings file rides the command line, where settings outrank every
    file, so the org layer beats even the checkout's own .claude/settings.json.
    `user` stays among the sources: that scope carries skills, commands, and
    agents discovery. The flags go ahead of the caller's arguments, which
    leaves the session's own trailing arguments the last word.

    The config directory is the destination the root phases merged. The
    credential store is named rather than linked, because credentials are
    written by rename and a rename replaces a link with a container-local
    file; claude's token-refresh lock sits in the same directory, so
    concurrent containers rotate the shared token one at a time.
    """
    env = dict(env)
    if os.access(WRAPPER_DIR + "/git", os.X_OK):
        env["PATH"] = WRAPPER_DIR + ":" + env["PATH"]
    if os.path.isfile(SETTINGS_FILE):
        argv = argv[:1] + [
            "--settings", SETTINGS_FILE,
            "--setting-sources", "user,project,local",
        ] + argv[1:]
    env["CLAUDE_CONFIG_DIR"] = ctx.config_dest
    env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = ctx.home + "/.claude"
    return argv, env


SPEC = HarnessSpec(
    name="claude",
    binary="claude",
    config_dest="/run/swarmforge/claude-config",
    config_reset=False,
    # agents/ is kept out because unified agent translation is its sole
    # source; settings.json merges by key through finalize_config instead of
    # overlaying whole; .credentials.json stays out because the default user
    # layer is the host's own ~/.claude and the store is named elsewhere, so
    # a merged copy is a secret nothing reads.
    layer_excludes=(
        "./skills",
        "./commands",
        "./agents",
        "./settings.json",
        "./.credentials.json",
    ),
    keyed_files=("opencode.json",),
    skills_dest="{config}/skills",
    commands_dest="{config}/commands",
    agents_dest="{config}/agents",
    mcp_fragment=mcp_fragment,
    mcp_delivery=("flag", "--mcp-config"),
    mcp_merge=Waiver(
        "the fragment reaches claude on its command line; nothing merges it "
        "into a config file"
    ),
    agent_emitter=agent_emitter,
    extra_chown_paths=("/run/swarmforge/claude-config",),
    finalize_config=finalize_config,
    link_state=link_state,
    root_setup=root_setup,
    pre_exec=pre_exec,
)
