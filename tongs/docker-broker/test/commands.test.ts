import { test } from "node:test";
import assert from "node:assert/strict";
import { parseConfig, type CommandDef } from "../src/config.js";
import {
  buildWorkerArgv,
  applyParams,
  resolveWorkspaceMount,
  BrokerError,
  runWorker,
  MAX_WORKER_OUTPUT_BYTES,
} from "../src/commands.js";

function commandFrom(command: Record<string, unknown>): CommandDef {
  const cfg = parseConfig({
    name: "b",
    allowed_images: ["img@sha256:abc"],
    commands: [{ image: "img@sha256:abc", ...command }],
  });
  return cfg.commands[0];
}

test("a base command builds run --rm <image> <command>", () => {
  const cmd = commandFrom({ name: "build", description: "d", command: ["make", "build"] });
  assert.deepEqual(buildWorkerArgv(cmd, {}, undefined), [
    "run",
    "--rm",
    "img@sha256:abc",
    "make",
    "build",
  ]);
});

test("a boolean param appends tokens and env only when true", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    command: ["start"],
    params: [{ name: "local", type: "boolean", when_true: { append_command: ["--local"], env: { LOCAL: "1" } } }],
  });
  assert.deepEqual(applyParams(cmd, { local: true }), { append: ["--local"], env: { LOCAL: "1" } });
  assert.deepEqual(applyParams(cmd, { local: false }), { append: [], env: {} });
  assert.deepEqual(applyParams(cmd, {}), { append: [], env: {} });
});

test("a boolean param rejects non-booleans if schema validation is bypassed", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    command: ["start"],
    params: [{ name: "local", type: "boolean", when_true: { append_command: ["--local"] } }],
  });
  assert.throws(() => applyParams(cmd, { local: "false" }), BrokerError);
});

test("a boolean default applies when the arg is absent", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    command: ["go"],
    params: [{ name: "fast", type: "boolean", default: true, when_true: { append_command: ["fast"] } }],
  });
  assert.deepEqual(buildWorkerArgv(cmd, {}, undefined), ["run", "--rm", "img@sha256:abc", "go", "fast"]);
});

test("an enum append target appends the selected value as one token", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    command: ["run.sh"],
    params: [{ name: "suite", type: "enum", values: ["unit", "e2e"], append_value: true }],
  });
  assert.deepEqual(buildWorkerArgv(cmd, { suite: "e2e" }, undefined), [
    "run",
    "--rm",
    "img@sha256:abc",
    "run.sh",
    "e2e",
  ]);
});

test("an enum env target sets the env var", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    params: [{ name: "target", type: "enum", values: ["app", "lib"], env_var: "TARGET" }],
  });
  assert.deepEqual(applyParams(cmd, { target: "lib" }), { append: [], env: { TARGET: "lib" } });
});

test("an enum value outside its set is rejected even if the schema is bypassed", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    params: [{ name: "t", type: "enum", values: ["a", "b"], append_value: true }],
  });
  assert.throws(() => applyParams(cmd, { t: "; rm -rf /" }), BrokerError);
});

test("a missing required enum is rejected", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    params: [{ name: "t", type: "enum", values: ["a", "b"], append_value: true, required: true }],
  });
  assert.throws(() => applyParams(cmd, {}), /missing required parameter/);
});

test("workspace mounts resolve against the host path", () => {
  assert.equal(resolveWorkspaceMount("workspace", "/host/ws"), "/host/ws:/workspace");
  assert.equal(resolveWorkspaceMount("workspace:ro", "/host/ws"), "/host/ws:/workspace:ro");
  assert.equal(resolveWorkspaceMount("workspace:/code", "/host/ws"), "/host/ws:/code");
  assert.equal(resolveWorkspaceMount("workspace:/code:ro", "/host/ws"), "/host/ws:/code:ro");
});

test("a workspace mount without a host path is an error", () => {
  const cmd = commandFrom({ name: "x", description: "d", mounts: ["workspace"], command: ["go"] });
  assert.throws(() => buildWorkerArgv(cmd, {}, undefined), /SWARMFORGE_WORKSPACE_HOST_PATH is unset/);
  assert.deepEqual(buildWorkerArgv(cmd, {}, "/host/ws"), [
    "run",
    "--rm",
    "-v",
    "/host/ws:/workspace",
    "img@sha256:abc",
    "go",
  ]);
});

test("env is emitted sorted and param env overrides static env", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    env: { B: "static", A: "1" },
    params: [{ name: "p", type: "enum", values: ["override"], env_var: "B" }],
  });
  const argv = buildWorkerArgv(cmd, { p: "override" }, undefined);
  // -e A=1 comes before -e B=override, and B is the param's value, not "static".
  assert.deepEqual(argv, ["run", "--rm", "-e", "A=1", "-e", "B=override", "img@sha256:abc"]);
});

test("appended values stay single argv elements (no shell splitting)", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    command: ["echo"],
    params: [{ name: "msg", type: "enum", values: ["a b c"], append_value: true }],
  });
  const argv = buildWorkerArgv(cmd, { msg: "a b c" }, undefined);
  assert.equal(argv[argv.length - 1], "a b c");
});

test("resources and networks render as docker flags", () => {
  const cmd = commandFrom({
    name: "x",
    description: "d",
    networks: ["build-net"],
    resources: { memory: "512m", cpus: "2", gpus: "all" },
    command: ["go"],
  });
  const argv = buildWorkerArgv(cmd, {}, undefined);
  assert.ok(argv.includes("--network") && argv[argv.indexOf("--network") + 1] === "build-net");
  assert.ok(argv.includes("--memory") && argv[argv.indexOf("--memory") + 1] === "512m");
  assert.ok(argv.includes("--gpus") && argv[argv.indexOf("--gpus") + 1] === "all");
});

test("worker output capture is capped", async () => {
  const cmd = commandFrom({ name: "x", description: "d", command: ["go"] });
  const result = await runWorker(cmd, {}, undefined, async () => ({
    exitCode: 0,
    stdout: "x".repeat(MAX_WORKER_OUTPUT_BYTES + 1),
    stderr: "y".repeat(MAX_WORKER_OUTPUT_BYTES + 1),
  }));

  assert.equal(result.stdout.length, MAX_WORKER_OUTPUT_BYTES);
  assert.equal(result.stderr.length, MAX_WORKER_OUTPUT_BYTES);
});
