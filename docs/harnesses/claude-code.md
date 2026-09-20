# Claude Code

`make run_claude` starts a Claude Code container with the same workspace and git-worktree mounting as `make run_opencode`.
Claude state persists by mounting `$(CLAUDE_HOME_DIR)` to `/home/anvil`, keeping account/session files like `~/.claude/` and `~/.claude.json`.
The repo is mounted at a stable path derived from the git remote slug (with `/workspace` still mounted for compatibility), which groups sessions consistently across worktrees without host-specific absolute paths.

- To reuse existing host-native Claude sessions directly, run with `CLAUDE_HOME_DIR=$HOME`.
- Remote slugs map deterministically, e.g. `git@github.com:crypticswarm/Swarmforge.git` -> `/repos/crypticswarm/Swarmforge`. Override with `SWARMFORGE_REPO_SLUG=crypticswarm/Swarmforge` and `SWARMFORGE_REMOTE_NAME=<remote>`.

## Claude config layering

Three sources merge into Claude's config dir at startup (lowest to highest precedence):
- `SWARMFORGE_REPO_CONFIG_DIR` (default `claude/`, if present)
- `SWARMFORGE_USER_CONFIG_DIR` (default `~/.claude`)
- `SWARMFORGE_ORG_CONFIG_DIR` (optional; defaults to `$(SWARMFORGE_ORG_CONFIG_ROOT)/.claude` when that root is set)

Skills, commands, and `agents/` are excluded from this merge — they travel through the [asset pipeline](../configuration.md).

Config layers stack in the opposite order to the [asset layers](../configuration.md): assets order by specificity, so a repo's own skill wins, while config orders by **trust**, because these files carry permissions, hooks, and env. A checkout is whatever repo you cloned and sits at the bottom; the org layer is installed deliberately and sits on top.

### The config directory

Claude runs with `CLAUDE_CONFIG_DIR` pointed at a container-local path, rebuilt from the config layers and the asset pipeline on every run. Everything Claude reads as configuration or code lives in that directory, so a shared one would hand a session's writes to the next container and to any running alongside it.

State that must outlive the run (`projects/`, `history.jsonl`, …) is symlinked back in from the shared home; the allowlist is `STATE_DIRS`/`STATE_FILES` in `swarmforge/harness/claude/`. It fails safe — a directory Claude learns to load in a later release stays inert until listed — at the cost that an unlisted new state directory dies with the container. A link holds only what Claude writes in place: an entry it rewrites by rename replaces the link with a container-local file.

Credentials are that second kind, so `CLAUDE_SECURESTORAGE_CONFIG_DIR` names their store instead: `~/.claude` in the shared home. The rename lands on the persistent mount, and Claude's token-refresh lock sits in the same directory, so concurrent containers rotate the shared token one at a time.

`plugins/` is linked but mounted read-only: marketplace clones are worth keeping, but a session must not rewrite what the next container executes, so plugin installs happen host-side.

### settings.json

`settings.json` is the exception to the file-replacement rule: like `opencode.json`, it is merged **by key**, and it is rebuilt from scratch on every run rather than merged into whatever the last run left behind. Below the three layers sits a fourth the image ships (`swarmforge/harness/claude/claude-settings.json`), which is where the status line default comes from.

The result never touches the host or the shared home: it is built at a container-local path during the config phase, and `--settings <path> --setting-sources user,project,local` is spliced onto claude's command line ahead of the session's own arguments. `user` stays in the sources because that scope carries skills, commands, and agents discovery. Three consequences:

- The built file sits at command-line precedence, above the workspace's own `.claude/settings.json` and `settings.local.json` (both still load natively) — a key an org layer sets cannot be overridden from a checkout.
- A key edited from inside a session (`/config`, the statusline-setup skill) lands in the container-local `settings.json` and dies with the container. Put it in a config layer instead.
- Under `CLAUDE_HOME_DIR=$HOME`, your real `~/.claude/settings.json` is read as the user config layer and never written.

A layer whose `settings.json` is not valid JSON, or not a JSON object, is skipped with a message on stderr; the rest of the layers still apply.

This covers `settings.json` only. Every other layer file — a `CLAUDE.md`, a hooks script, `settings.local.json` — is replaced wholesale in the container-local config dir and dies with it.

## Status line

`make build_claude` bakes `swarmforge/harness/claude/statusline.sh` into the image at `/usr/local/bin/swarmforge-statusline`, and the image defaults layer points `statusLine` at it — so a container shows the model, directory, turn count, context percentage, and session token/cost totals with no host setup. It reads the session JSON on stdin and the transcript.

Being the lowest layer, its `statusLine` is overridden key by key — a layer that sets both `type` and `command`, as any real one does, replaces it entirely:

```json
{
  "statusLine": { "type": "command", "command": "~/.claude/my-statusline.sh" }
}
```
