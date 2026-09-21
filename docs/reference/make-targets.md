# Make targets

`make` is the entry point for everything that builds or runs a container. Every variable named here is defined in [Environment variables](environment.md).

Run the targets from your project directory, against the checkout's `Makefile`:

```bash
make -C /path/to/Swarmforge run_claude PROJECT_DIR=$(pwd)
```

`PROJECT_DIR` has to be passed under `-C`, because `make` changes directory before reading anything.

## The harness families

Sixteen targets, four per harness:

| Harness | Build | Update | Run | Stop |
| --- | --- | --- | --- | --- |
| [Claude Code](../harnesses/claude-code.md) | `build_claude` | `update_claude` | `run_claude` | `stop_claude` |
| [Codex CLI](../harnesses/codex.md) | `build_codex` | `update_codex` | `run_codex` | `stop_codex` |
| [Grok Build CLI](../harnesses/grok.md) | `build_grok` | `update_grok` | `run_grok` | `stop_grok` |
| [OpenCode](../harnesses/opencode.md) | `build_opencode` | `update_opencode` | `run_opencode` | `stop_opencode` |

A `harness_rules` macro generates all sixteen from the knobs each [`harness.mk`](../development/architecture.md) declares, so the table below uses `<harness>` for the name and `<PREFIX>_` for that fragment's variable prefix (`CLAUDE_`, `OPENCODE_`, …).

| Target | What it does | Accepts |
| --- | --- | --- |
| `build_<harness>` | Builds the `harness-runtime` stage of `anvil/Dockerfile` with `--build-arg AGENT=<harness>` and tags it `$(<PREFIX>_IMG)`. | `DEBIAN_TAG`, `SWARMFORGE_HARNESS_INSTALL_BUST`, `<PREFIX>_IMG`, and for OpenCode `OPENCODE_VERSION` — see [Building images](environment.md#building-images) |
| `update_<harness>` | Re-runs `build_<harness>` from the harness install step onward, leaving the shared stages below it cached. | the same, except `SWARMFORGE_HARNESS_INSTALL_BUST`, which it sets |
| `run_<harness>` | Creates the host directories that harness needs, then hands the `docker run` command line to the [launcher](../tongs/README.md). Replaces a container of the same name, and runs with `--rm`. | [the session variables](environment.md#running-a-session), [that harness's storage knobs](environment.md#per-harness-storage-and-arguments), [the asset roots](environment.md#asset-layers), [the config layer variables](environment.md#config-layers) |
| `stop_<harness>` | `docker rm -f` on that harness's container, silent if there is none. | `<PREFIX>_CTR` |
| `build_harnesses` | Builds all four images. | as `build_<harness>` |
| `clean` | Stops every harness container, stops Ollama, and removes the network. | `NETWORK` |

## Container names

A session's container is named `<harness>-<directory>-<digest>` — `claude-master-3f9a1c72` — where the digest is the first eight hex digits of the SHA-256 of `PROJECT_DIR` resolved to an absolute path with its symlinks followed. The basename alone would not identify a session: the [worktree layout](../cli.md) puts each branch in a sibling directory, so `repo1/master` and `repo2/master` share one, and `run_<harness>` removes a container of the name it is about to use before it starts. The name is a function of the directory and nothing else, so the same `PROJECT_DIR` always reproduces it — and so do two spellings of one directory.

Everything the launcher derives afterwards carries that name: the per-session docker network is `swarmforge-session-<container>`, and each `session` [tong](../tongs/README.md)'s container is `<container>-tong-<tong>`. That last one docker registers as a DNS label, which may not exceed 63 characters, so the readable half of the name is cut to 24. That holds the container name to 42 and leaves 21 for `-tong-<tong>`, so a branch name of any length fits and a tong named in 15 characters or fewer does too. The digest is what identifies the directory; the cut costs only readability.

Set `<PREFIX>_CTR` to name the container yourself; `run_<harness>` and `stop_<harness>` both read it, so the two stay in agreement.

## Images without a harness

| Target | What it does |
| --- | --- |
| `build_broker` | Builds `tongs/docker-broker` and tags it `$(BROKER_IMG)`, for a [broker tong](../tongs/broker.md). |
| `opencode_network` | Creates the `$(NETWORK)` docker network if it is not there. Every `run_<harness>` target, `run_ollama`, and `test-skills` depends on it, and it is the default goal. |

## Ollama

Ollama serves models from your own machine, so a session can use one without a provider account.

| Target | What it does |
| --- | --- |
| `run_ollama` | Starts `$(OLLAMA_IMG)` detached as `$(OLLAMA_CTR)`, with the checkout's `ollama/` as its model store, `--gpus=all`, and `$(OLLAMA_PORT)` published. Replaces a container already under that name. |
| `logs_ollama` | `docker logs -f` on it. |
| `stop_ollama` | `docker rm -f` on it, silent if there is none. |
| `gpu_stat` | `nvidia-smi`. |

Seven targets exec into the running container to pull and run one model: `run_llama_3-1-8b`, `run_gpt-oss-20b`, `run_gpt-oss-120b`, `run_devstral2_small`, `run_qwen_3-5-27b`, `run_qwen_3-5-35b`, `run_gemma4_26b`. Each takes no variables and has no prerequisite, so `make run_ollama` comes first.

## Tests and lint

What each one runs is in [Testing](../development/testing.md).

| Target | Accepts |
| --- | --- |
| `test` | `PYTHON` |
| `lint` | `RUFF` |
| `test-skills` | `MODEL` (required), `EVAL_MODEL`, `TEST_SKILL`, `TEST_ENABLE_JUDGE`, `TEST_TIMEOUT_S`, `TEST_DATA_DIR`, `OPENCODE_IMG`, `PROJECT_DIR`, `NETWORK` |
