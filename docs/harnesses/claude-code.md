# Claude Code

`make run_claude` starts a Claude Code container with the same workspace and git-worktree mounting as `make run_opencode`.
Claude state persists by mounting `$(CLAUDE_HOME_DIR)` to `/home/anvil`, keeping account/session files like `~/.claude/` and `~/.claude.json`.
The repo is mounted at a stable path derived from the git remote slug (with `/workspace` still mounted for compatibility), which groups sessions consistently across worktrees without host-specific absolute paths.

- To reuse existing host-native Claude sessions directly, run with `CLAUDE_HOME_DIR=$HOME`.
- Remote slugs map deterministically, e.g. `git@github.com:crypticswarm/Swarmforge.git` -> `/repos/crypticswarm/Swarmforge`. Override with `SWARMFORGE_REPO_SLUG=crypticswarm/Swarmforge` and `SWARMFORGE_REMOTE_NAME=<remote>`.

## Claude config layering

The three [config layers](../configuration.md#config-layers) merge into Claude's config dir at startup. `agents/` is held out of the merge along with the asset dirs: unified agent translation is its only source.

### The config directory

Claude runs with `CLAUDE_CONFIG_DIR` pointed at a container-local path, rebuilt from the config layers and the [asset pipeline](../configuration.md#asset-layers) on every run. Everything Claude reads as configuration or code lives in that directory.

State that must outlive the run (`projects/`, `history.jsonl`, …) is symlinked back in from the shared home. Credentials live in the same shared `~/.claude`, named by `CLAUDE_SECURESTORAGE_CONFIG_DIR`, so a login survives the run. `plugins/` is linked too, but read-only: plugin installs happen host-side.

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
