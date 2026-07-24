// Turning a validated command + caller-supplied parameters into a concrete
// `docker run` invocation. This is the broker's security boundary, so it trusts
// nothing: images are fixed by config, mounts resolve only the `workspace` magic
// word against the host path the launcher injected, and parameters can only
// append config-defined tokens or insert an enum value re-checked against its
// allowed set. Workers are always spawned with an argv array -- never a shell --
// so no parameter value can be interpreted as a flag, path, or shell metacharacter.

import { spawn } from "node:child_process";
import type { CommandDef } from "./config.js";

export class BrokerError extends Error {}

export type RunResult = {
  exitCode: number;
  stdout: string;
  stderr: string;
};

export type Spawn = (command: string, args: string[]) => Promise<RunResult>;

export const MAX_WORKER_OUTPUT_BYTES = 1024 * 1024;

// Resolve a validated `workspace[:<target>][:<mode>]` spec to a docker `-v` value
// against the host workspace path.
export function resolveWorkspaceMount(spec: string, workspaceHost: string): string {
  const parts = spec.split(":");
  let target = "/workspace";
  let mode: string | undefined;
  for (let i = 1; i < parts.length; i++) {
    const field = parts[i];
    if (field.startsWith("/")) target = field;
    else mode = field;
  }
  return mode ? `${workspaceHost}:${target}:${mode}` : `${workspaceHost}:${target}`;
}

// Map caller inputs onto the additive effects the config permits: tokens appended
// to the worker command and fixed environment overrides. Inputs are applied in
// declaration order so the resulting argv is deterministic.
export function applyParams(
  command: CommandDef,
  inputs: Record<string, unknown>,
): { append: string[]; env: Record<string, string> } {
  const append: string[] = [];
  const env: Record<string, string> = {};
  for (const param of command.params) {
    if (param.type === "boolean") {
      const value = param.name in inputs ? inputs[param.name] : param.default;
      if (typeof value !== "boolean") {
        throw new BrokerError(`parameter '${param.name}' must be a boolean`);
      }
      if (value) {
        append.push(...param.whenTrue.appendCommand);
        Object.assign(env, param.whenTrue.env);
      }
      continue;
    }
    // enum
    const provided = param.name in inputs ? inputs[param.name] : undefined;
    const selected = provided === undefined ? param.default : String(provided);
    if (selected === undefined) {
      if (param.required) throw new BrokerError(`missing required parameter '${param.name}'`);
      continue;
    }
    // Re-check against the allowed set even though the MCP schema already enforces
    // it: this function must be safe on its own, never trusting the caller.
    if (!param.values.includes(selected)) {
      throw new BrokerError(`'${selected}' is not an allowed value for parameter '${param.name}'`);
    }
    if (param.target.kind === "append") append.push(selected);
    else env[param.target.var] = selected;
  }
  return { append, env };
}

// Build the argv passed to `docker` (i.e. starting at `run`). Pure and total for a
// validated command; throws only when a workspace mount is requested but the host
// path was not injected.
export function buildWorkerArgv(
  command: CommandDef,
  inputs: Record<string, unknown>,
  workspaceHost: string | undefined,
): string[] {
  const { append, env } = applyParams(command, inputs);
  const argv = ["run", "--rm"];
  if (command.entrypoint) argv.push("--entrypoint", command.entrypoint);
  if (command.workdir) argv.push("-w", command.workdir);
  for (const network of command.networks) argv.push("--network", network);

  const mergedEnv = { ...command.env, ...env };
  for (const key of Object.keys(mergedEnv).sort()) argv.push("-e", `${key}=${mergedEnv[key]}`);

  for (const spec of command.mounts) {
    if (!workspaceHost) {
      throw new BrokerError(
        `command '${command.name}' mounts the workspace but SWARMFORGE_WORKSPACE_HOST_PATH is unset`,
      );
    }
    argv.push("-v", resolveWorkspaceMount(spec, workspaceHost));
  }

  if (command.resources.memory) argv.push("--memory", command.resources.memory);
  if (command.resources.cpus) argv.push("--cpus", command.resources.cpus);
  if (command.resources.gpus) argv.push("--gpus", command.resources.gpus);

  argv.push(command.image);
  argv.push(...command.command, ...append);
  return argv;
}

const realSpawn: Spawn = (command, args) =>
  new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    const appendBounded = (chunks: Buffer[], currentBytes: number, chunk: Buffer | string): number => {
      if (currentBytes >= MAX_WORKER_OUTPUT_BYTES) return currentBytes;
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      const remaining = MAX_WORKER_OUTPUT_BYTES - currentBytes;
      const next = buffer.length > remaining ? buffer.subarray(0, remaining) : buffer;
      chunks.push(next);
      return currentBytes + next.length;
    };
    child.stdout.on("data", (chunk) => {
      stdoutBytes = appendBounded(stdoutChunks, stdoutBytes, chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderrBytes = appendBounded(stderrChunks, stderrBytes, chunk);
    });
    child.on("error", reject);
    child.on("close", (code) =>
      resolve({
        exitCode: code ?? 0,
        stdout: Buffer.concat(stdoutChunks, stdoutBytes).toString(),
        stderr: Buffer.concat(stderrChunks, stderrBytes).toString(),
      }),
    );
  });

function capOutput(value: string): string {
  const bytes = Buffer.byteLength(value);
  if (bytes <= MAX_WORKER_OUTPUT_BYTES) return value;
  return Buffer.from(value).subarray(0, MAX_WORKER_OUTPUT_BYTES).toString();
}

// Run one worker to completion, capturing its output. The container is `--rm`, so
// nothing is left behind once it exits.
export function runWorker(
  command: CommandDef,
  inputs: Record<string, unknown>,
  workspaceHost: string | undefined,
  doSpawn: Spawn = realSpawn,
): Promise<RunResult> {
  const argv = buildWorkerArgv(command, inputs, workspaceHost);
  return doSpawn("docker", argv).then((result) => ({
    ...result,
    stdout: capOutput(result.stdout),
    stderr: capOutput(result.stderr),
  }));
}

export function formatResult(label: string, result: RunResult): string {
  const ok = result.exitCode === 0;
  const lines = [`${label}: ${ok ? "success" : `failed (exit ${result.exitCode})`}`];
  if (result.stdout.trim()) lines.push("--- stdout ---", result.stdout.trimEnd());
  if (result.stderr.trim()) lines.push("--- stderr ---", result.stderr.trimEnd());
  return lines.join("\n");
}
