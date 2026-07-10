// Declarative broker configuration: the set of narrow docker-task verbs this
// broker exposes. The shape deliberately mirrors a Swarmforge tong definition --
// each command describes the *worker* container to spawn (image, mounts, command,
// env, resources, networks) -- with an MCP surface (name, description, typed
// params) layered on top. There is no "run an arbitrary container" verb: the set
// of images named across the commands, gated by `allowed_images`, is the entire
// allowlist.
//
// Loading is fail-closed. Any structural problem throws ConfigError and the broker
// refuses to start, so a misconfigured allowlist or an injectable parameter can
// never reach the docker socket.

import { readFileSync } from "node:fs";
import yaml from "js-yaml";

export class ConfigError extends Error {}

export type Effect = {
  // Tokens appended (as whole argv words, never shell-split) to the worker command.
  appendCommand: string[];
  // Fixed environment overrides applied to the worker.
  env: Record<string, string>;
};

export type BooleanParam = {
  name: string;
  type: "boolean";
  description: string;
  default: boolean;
  // Applied verbatim when the caller passes `true`. Drawn entirely from config;
  // the caller chooses only whether to apply it.
  whenTrue: Effect;
};

export type EnumParam = {
  name: string;
  type: "enum";
  description: string;
  values: string[];
  required: boolean;
  default?: string;
  // Where the chosen value (always one of `values`) goes: appended as a single
  // command token, or set as the value of a fixed env var.
  target: { kind: "append" } | { kind: "env"; var: string };
};

export type Param = BooleanParam | EnumParam;

export type Resources = {
  memory?: string;
  cpus?: string;
  gpus?: string;
};

export type CommandDef = {
  name: string;
  description: string;
  image: string;
  // Magic-word mount specs (only `workspace[:<target>][:<mode>]`); never raw host
  // paths and never the docker socket.
  mounts: string[];
  workdir?: string;
  entrypoint?: string;
  command: string[];
  env: Record<string, string>;
  networks: string[];
  resources: Resources;
  params: Param[];
};

export type BrokerConfig = {
  name: string;
  description: string;
  allowedImages: string[];
  commands: CommandDef[];
};

const NAME_RE = /^[a-zA-Z0-9_-]+$/;
const PARAM_NAME_RE = /^[a-zA-Z0-9_]+$/;
const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown, where: string): string {
  if (typeof value !== "string") {
    throw new ConfigError(`${where} must be a string`);
  }
  return value;
}

function asNonEmptyString(value: unknown, where: string): string {
  const s = asString(value, where);
  if (s.trim() === "") throw new ConfigError(`${where} must not be empty`);
  return s;
}

function asStringArray(value: unknown, where: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new ConfigError(`${where} must be a list`);
  return value.map((item, i) => asString(item, `${where}[${i}]`));
}

function asStringMap(value: unknown, where: string): Record<string, string> {
  if (value === undefined) return {};
  if (!isPlainObject(value)) throw new ConfigError(`${where} must be a mapping`);
  const out: Record<string, string> = {};
  for (const [key, raw] of Object.entries(value)) {
    if (!ENV_KEY_RE.test(key)) {
      throw new ConfigError(`${where}: '${key}' is not a valid environment variable name`);
    }
    out[key] = asString(raw, `${where}.${key}`);
  }
  return out;
}

// `workspace`, `workspace:ro`, `workspace:/code`, `workspace:/code:ro` -- the
// only legal mount. A worker never receives `docker-socket` (that would re-hand
// out the broker's own privilege) or a raw host path.
function validateMount(raw: unknown, where: string): string {
  const spec = asString(raw, where);
  const parts = spec.split(":");
  if (parts[0] === "docker-socket") {
    throw new ConfigError(`${where}: a worker may not mount the docker socket`);
  }
  if (parts[0] !== "workspace") {
    throw new ConfigError(`${where}: '${spec}' is not a recognized mount (only 'workspace')`);
  }
  if (parts.length > 3) {
    throw new ConfigError(`${where}: '${spec}' has too many ':'-separated fields`);
  }
  let seenTarget = false;
  for (let i = 1; i < parts.length; i++) {
    const field = parts[i];
    const looksLikePath = field.startsWith("/");
    const looksLikeMode = field === "ro" || field === "rw";
    if (!looksLikePath && !looksLikeMode) {
      throw new ConfigError(`${where}: '${field}' is neither a target path nor an access mode`);
    }
    // A mode may only appear last.
    if (looksLikeMode && i !== parts.length - 1) {
      throw new ConfigError(`${where}: access mode '${field}' must be the final field`);
    }
    if (looksLikePath) {
      if (seenTarget) {
        throw new ConfigError(`${where}: '${spec}' has multiple target paths`);
      }
      seenTarget = true;
    }
  }
  return spec;
}

function validateResources(value: unknown, where: string): Resources {
  if (value === undefined) return {};
  if (!isPlainObject(value)) throw new ConfigError(`${where} must be a mapping`);
  const out: Resources = {};
  for (const key of ["memory", "cpus", "gpus"] as const) {
    if (value[key] !== undefined) out[key] = asNonEmptyString(value[key], `${where}.${key}`);
  }
  const unknown = Object.keys(value).filter((k) => !["memory", "cpus", "gpus"].includes(k));
  if (unknown.length) throw new ConfigError(`${where}: unknown resource(s) ${unknown.join(", ")}`);
  return out;
}

function validateEffect(value: unknown, where: string): Effect {
  if (!isPlainObject(value)) throw new ConfigError(`${where} must be a mapping`);
  const effect: Effect = {
    appendCommand: asStringArray(value.append_command, `${where}.append_command`),
    env: asStringMap(value.env, `${where}.env`),
  };
  if (effect.appendCommand.length === 0 && Object.keys(effect.env).length === 0) {
    throw new ConfigError(`${where} has no effect (set append_command and/or env)`);
  }
  return effect;
}

function validateParam(value: unknown, where: string): Param {
  if (!isPlainObject(value)) throw new ConfigError(`${where} must be a mapping`);
  const name = asNonEmptyString(value.name, `${where}.name`);
  if (!PARAM_NAME_RE.test(name)) {
    throw new ConfigError(`${where}.name '${name}' must match ${PARAM_NAME_RE}`);
  }
  const description = value.description === undefined ? "" : asString(value.description, `${where}.description`);
  const type = asString(value.type, `${where}.type`);

  if (type === "boolean") {
    const dflt = value.default === undefined ? false : value.default;
    if (typeof dflt !== "boolean") throw new ConfigError(`${where}.default must be a boolean`);
    // An omitted when_true would advertise a parameter that does nothing when set.
    if (value.when_true === undefined) {
      throw new ConfigError(`${where}.when_true is required (a boolean param must declare its effect)`);
    }
    return {
      name,
      type: "boolean",
      description,
      default: dflt,
      whenTrue: validateEffect(value.when_true, `${where}.when_true`),
    };
  }

  if (type === "enum") {
    const values = asStringArray(value.values, `${where}.values`);
    if (values.length === 0) throw new ConfigError(`${where}.values must be non-empty`);
    if (new Set(values).size !== values.length) throw new ConfigError(`${where}.values has duplicates`);
    const required = value.required === undefined ? false : value.required;
    if (typeof required !== "boolean") throw new ConfigError(`${where}.required must be a boolean`);
    let dflt: string | undefined;
    if (value.default !== undefined) {
      if (required) throw new ConfigError(`${where}: a required param cannot also have a default`);
      dflt = asString(value.default, `${where}.default`);
      if (!values.includes(dflt)) throw new ConfigError(`${where}.default '${dflt}' is not one of values`);
    }
    const hasAppend = value.append_value === true;
    const envVar = value.env_var === undefined ? undefined : asNonEmptyString(value.env_var, `${where}.env_var`);
    if (hasAppend === (envVar !== undefined)) {
      throw new ConfigError(`${where} must set exactly one of append_value: true or env_var`);
    }
    if (envVar !== undefined && !ENV_KEY_RE.test(envVar)) {
      throw new ConfigError(`${where}.env_var '${envVar}' is not a valid environment variable name`);
    }
    const target: EnumParam["target"] = hasAppend ? { kind: "append" } : { kind: "env", var: envVar! };
    return { name, type: "enum", description, values, required, default: dflt, target };
  }

  throw new ConfigError(`${where}.type '${type}' must be 'boolean' or 'enum'`);
}

function validateCommand(value: unknown, where: string, allowedImages: string[]): CommandDef {
  if (!isPlainObject(value)) throw new ConfigError(`${where} must be a mapping`);
  const name = asNonEmptyString(value.name, `${where}.name`);
  if (!NAME_RE.test(name)) throw new ConfigError(`${where}.name '${name}' must match ${NAME_RE}`);
  const image = asNonEmptyString(value.image, `${where}.image`);
  if (!allowedImages.includes(image)) {
    throw new ConfigError(`${where}.image '${image}' is not in allowed_images`);
  }
  const mounts = (value.mounts === undefined ? [] : asStringArray(value.mounts, `${where}.mounts`)).map(
    (m, i) => validateMount(m, `${where}.mounts[${i}]`),
  );
  const params = (value.params === undefined ? [] : (value.params as unknown[])).map((p, i) =>
    validateParam(p, `${where}.params[${i}]`),
  );
  const seen = new Set<string>();
  for (const p of params) {
    if (seen.has(p.name)) throw new ConfigError(`${where}: duplicate param '${p.name}'`);
    seen.add(p.name);
  }
  return {
    name,
    description: asNonEmptyString(value.description, `${where}.description`),
    image,
    mounts,
    workdir: value.workdir === undefined ? undefined : asNonEmptyString(value.workdir, `${where}.workdir`),
    entrypoint: value.entrypoint === undefined ? undefined : asNonEmptyString(value.entrypoint, `${where}.entrypoint`),
    command: asStringArray(value.command, `${where}.command`),
    env: asStringMap(value.env, `${where}.env`),
    networks: asStringArray(value.networks, `${where}.networks`),
    resources: validateResources(value.resources, `${where}.resources`),
    params,
  };
}

export function parseConfig(doc: unknown): BrokerConfig {
  if (!isPlainObject(doc)) throw new ConfigError("config root must be a mapping");
  const name = asNonEmptyString(doc.name, "name");
  const description = doc.description === undefined ? "" : asString(doc.description, "description");
  const allowedImages = asStringArray(doc.allowed_images, "allowed_images");
  if (allowedImages.length === 0) {
    throw new ConfigError("allowed_images must list at least one image (the broker refuses to run with no allowlist)");
  }
  if (!Array.isArray(doc.commands) || doc.commands.length === 0) {
    throw new ConfigError("commands must be a non-empty list");
  }
  const commands = doc.commands.map((c, i) => validateCommand(c, `commands[${i}]`, allowedImages));
  const names = new Set<string>();
  for (const c of commands) {
    if (names.has(c.name)) throw new ConfigError(`duplicate command name '${c.name}'`);
    names.add(c.name);
  }
  return { name, description, allowedImages, commands };
}

export function loadConfig(path: string): BrokerConfig {
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch (err) {
    throw new ConfigError(`cannot read broker config at ${path}: ${(err as Error).message}`);
  }
  return parseConfig(yaml.load(raw));
}
