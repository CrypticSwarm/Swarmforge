import { test } from "node:test";
import assert from "node:assert/strict";
import { parseConfig, ConfigError, type BrokerConfig } from "../src/config.js";

function base(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    name: "demo-broker",
    allowed_images: ["toolchain@sha256:abc"],
    commands: [
      { name: "build", description: "Build it", image: "toolchain@sha256:abc", command: ["make", "build"] },
    ],
    ...overrides,
  };
}

test("a minimal config parses and normalizes defaults", () => {
  const cfg: BrokerConfig = parseConfig(base());
  assert.equal(cfg.name, "demo-broker");
  assert.equal(cfg.commands.length, 1);
  const cmd = cfg.commands[0];
  assert.deepEqual(cmd.command, ["make", "build"]);
  assert.deepEqual(cmd.mounts, []);
  assert.deepEqual(cmd.env, {});
  assert.deepEqual(cmd.params, []);
});

test("an empty allowlist is refused", () => {
  assert.throws(() => parseConfig(base({ allowed_images: [] })), ConfigError);
});

test("a command image outside the allowlist is refused", () => {
  const doc = base({
    commands: [{ name: "x", description: "d", image: "other:latest" }],
  });
  assert.throws(() => parseConfig(doc), /not in allowed_images/);
});

test("duplicate command names are refused", () => {
  const doc = base({
    commands: [
      { name: "dup", description: "d", image: "toolchain@sha256:abc" },
      { name: "dup", description: "d", image: "toolchain@sha256:abc" },
    ],
  });
  assert.throws(() => parseConfig(doc), /duplicate command/);
});

test("a worker may not mount the docker socket", () => {
  const doc = base({
    commands: [{ name: "x", description: "d", image: "toolchain@sha256:abc", mounts: ["docker-socket"] }],
  });
  assert.throws(() => parseConfig(doc), /may not mount the docker socket/);
});

test("an unrecognized mount magic word is refused", () => {
  const doc = base({
    commands: [{ name: "x", description: "d", image: "toolchain@sha256:abc", mounts: ["/etc:/etc"] }],
  });
  assert.throws(() => parseConfig(doc), /not a recognized mount/);
});

test("a mount access mode must come last", () => {
  const doc = base({
    commands: [{ name: "x", description: "d", image: "toolchain@sha256:abc", mounts: ["workspace:ro:/code"] }],
  });
  assert.throws(() => parseConfig(doc), /must be the final field/);
});

test("a workspace mount may only specify one target path", () => {
  const doc = base({
    commands: [{ name: "x", description: "d", image: "toolchain@sha256:abc", mounts: ["workspace:/code:/other"] }],
  });
  assert.throws(() => parseConfig(doc), /multiple target paths/);
});

test("a boolean param with no effect is refused", () => {
  const doc = base({
    commands: [
      {
        name: "x",
        description: "d",
        image: "toolchain@sha256:abc",
        params: [{ name: "flag", type: "boolean", when_true: {} }],
      },
    ],
  });
  assert.throws(() => parseConfig(doc), /has no effect/);
});

test("a boolean param that omits when_true is refused", () => {
  const doc = base({
    commands: [
      {
        name: "x",
        description: "d",
        image: "toolchain@sha256:abc",
        params: [{ name: "flag", type: "boolean" }],
      },
    ],
  });
  assert.throws(() => parseConfig(doc), /when_true is required/);
});

test("an enum param needs exactly one target", () => {
  const both = base({
    commands: [
      {
        name: "x",
        description: "d",
        image: "toolchain@sha256:abc",
        params: [{ name: "t", type: "enum", values: ["a", "b"], append_value: true, env_var: "T" }],
      },
    ],
  });
  assert.throws(() => parseConfig(both), /exactly one of/);

  const neither = base({
    commands: [
      {
        name: "x",
        description: "d",
        image: "toolchain@sha256:abc",
        params: [{ name: "t", type: "enum", values: ["a", "b"] }],
      },
    ],
  });
  assert.throws(() => parseConfig(neither), /exactly one of/);
});

test("an enum default outside its values is refused", () => {
  const doc = base({
    commands: [
      {
        name: "x",
        description: "d",
        image: "toolchain@sha256:abc",
        params: [{ name: "t", type: "enum", values: ["a", "b"], append_value: true, default: "z" }],
      },
    ],
  });
  assert.throws(() => parseConfig(doc), /is not one of values/);
});

test("a required enum cannot also carry a default", () => {
  const doc = base({
    commands: [
      {
        name: "x",
        description: "d",
        image: "toolchain@sha256:abc",
        params: [{ name: "t", type: "enum", values: ["a", "b"], append_value: true, required: true, default: "a" }],
      },
    ],
  });
  assert.throws(() => parseConfig(doc), /cannot also have a default/);
});

test("a full command with params normalizes cleanly", () => {
  const doc = base({
    description: "demo",
    commands: [
      {
        name: "compile",
        description: "Compile",
        image: "toolchain@sha256:abc",
        mounts: ["workspace:/code"],
        workdir: "/code",
        command: ["make"],
        env: { CI: "1" },
        resources: { memory: "512m" },
        params: [
          { name: "fast", type: "boolean", default: true, when_true: { append_command: ["build_fast"] } },
          { name: "target", type: "enum", values: ["app", "lib"], env_var: "TARGET" },
        ],
      },
    ],
  });
  const cfg = parseConfig(doc);
  const cmd = cfg.commands[0];
  assert.deepEqual(cmd.mounts, ["workspace:/code"]);
  assert.equal(cmd.resources.memory, "512m");
  assert.equal(cmd.params.length, 2);
  const fast = cmd.params[0];
  assert.equal(fast.type === "boolean" && fast.default, true);
  const target = cmd.params[1];
  assert.ok(target.type === "enum" && target.target.kind === "env" && target.target.var === "TARGET");
});
