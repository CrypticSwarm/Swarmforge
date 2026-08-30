#!/usr/bin/env python3
"""Translate unified Swarmforge agent definitions into harness-native formats.

Unified agent definitions are markdown files whose YAML frontmatter is a
superset of the OpenCode agent schema. The markdown body is the agent's
system prompt and passes through unchanged. Recognized frontmatter:

  description: <when to delegate to this agent>      (required)
  mode: subagent | primary | all                     (OpenCode only)
  model: <provider/model-id or harness alias>
  temperature: <float>                               (OpenCode only)
  tools:                                             (map of tool -> bool)
    write: false
  claude:                                            (per-harness overrides,
    model: haiku                                      merged into the output
  opencode:                                           frontmatter verbatim)
    permission: ...
  codex:
    model_reasoning_effort: high

The agent's identity is its filename (foo.md -> agent "foo"); a `name` field
is emitted only for harnesses that require one. Tool names use OpenCode's
lowercase ids; for harnesses without an equivalent the entry is dropped.
`disable: true` skips the agent on harnesses without native disable support.

Per-target rules:
  opencode  Frontmatter passes through minus other harnesses' override
            blocks; `model` is dropped unless provider-qualified (contains
            "/"). Translation is idempotent, so a directory can be
            translated in place.
  claude    Emits name/description, maps `tools: {x: false}` entries to
            `disallowedTools`, rewrites `model` (anthropic/<id> -> <id>,
            other providers dropped, aliases pass through), and drops
            OpenCode-only fields.
  codex     Emits project-agent TOML. OpenAI model prefixes are stripped,
            other providers are dropped, and codex overrides are merged.
            Generic tool restrictions are dropped.

Usage: python3 -m swarmforge.agents.translate <target> <dest_dir> <src_dir>...

Later source directories override earlier ones by filename. Missing or
empty source paths are skipped. Only top-level *.md files are read.
"""

import os
import sys

from swarmforge import harness
from swarmforge.agents.emit import render, split_frontmatter, warn
from swarmforge.harness.claude import to_claude
from swarmforge.harness.codex import normalize_codex_name, render_codex, to_codex
from swarmforge.harness.opencode import to_opencode
from swarmforge.harness.spec import provided
from swarmforge.yamlite import parse_map, parse_scalar

# This module's public surface: the CLI's own entry points, plus the
# frontmatter helpers and per-harness emitters they run on, which stay
# importable from the CLI's module name.
__all__ = [
    "EMITTERS",
    "load_agents",
    "main",
    "normalize_codex_name",
    "parse_map",
    "parse_scalar",
    "render",
    "render_codex",
    "run",
    "split_frontmatter",
    "to_claude",
    "to_codex",
    "to_opencode",
    "warn",
]

EMITTERS = {
    name: harness.get(name).SPEC.agent_emitter
    for name in harness.names()
    if provided(harness.get(name).SPEC.agent_emitter)
}


def load_agents(src_dirs):
    agents = {}
    for src_dir in src_dirs:
        if not src_dir or not os.path.isdir(src_dir):
            continue
        for filename in sorted(os.listdir(src_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(src_dir, filename)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            try:
                agents[filename] = split_frontmatter(text)
            except ValueError as exc:
                warn("skipping %s: %s" % (path, exc))
    return agents


def run(target, dest_dir, src_dirs, home=""):
    """Run the target harness's emitter over the loaded agents.

    Each emitted file is written under `dest_dir`, then the harness's
    finalize hook runs over everything that was emitted. `home`, when
    non-empty, is handed to that hook, so post-translation work landing in
    the user's home has a home to land in.
    """
    emitter = EMITTERS.get(target)
    if emitter is None:
        warn("unknown target '%s' (expected: %s)" % (target, ", ".join(sorted(EMITTERS))))
        return 2

    agents = load_agents(src_dirs)
    if not agents:
        return 0

    os.makedirs(dest_dir, exist_ok=True)
    emitted = []
    for filename, (meta, body) in agents.items():
        name = filename[: -len(".md")]
        result = emitter(name, meta, body)
        if result is None:
            continue
        out_filename, text = result
        out_path = os.path.join(dest_dir, out_filename)
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        emitted.append((name, meta, out_path))

    harness.get(target).SPEC.finalize_agents(dest_dir, emitted, home)
    return 0


def main(argv):
    """Translate the agents named on the command line."""
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    # A bare CLI run has no home, so nothing a finalize hook would publish
    # into one is written.
    return run(argv[0], argv[1], argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
