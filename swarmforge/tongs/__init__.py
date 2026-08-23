"""Pure launcher core for Swarmforge tongs (Swarmforge-managed sidecar processes).

A *tong* is a sibling container started with (or for) an *anvil* (harness
container). Definitions are one YAML file per tong under `.swarmforge/tongs/`,
discovered across the same four layers as agents (lowest to highest precedence):

    user   -> ~/.swarmforge/tongs/        (SWARMFORGE_USER_ASSETS_DIR)
    org    -> $ORG/.swarmforge/tongs/     (SWARMFORGE_ORG_ASSETS_DIR)
    repo   -> <checkout>/tongs/           (SWARMFORGE_REPO_TONGS_DIR)
    workspace -> <workspace>/.swarmforge/tongs/

This package is the pure core of the tongs launcher, one module per concern:

    model       the vocabulary of a definition, and readiness resolution
    discovery   reading the layer directories and merging by name
    validate    schema validation
    secrets     secret references, the provider table, FIFO delivery
    mounts      the `mounts:` magic words and their docker bind specs
    mcp         what each `interface:` contributes to the anvil
    network     per-session network planning
    approvals   config hash, privilege summary, approval keying
    argv        docker names, and the run argv for a tong and for the anvil
    cli         the `validate`/`discover` diagnostic commands

Every name the launcher and the tests use is re-exported below, so a caller
imports `swarmforge.tongs` and never has to know which module a function sits
in. Modules import each other directly; only the package's own surface is
gathered here. Each re-export is a fresh binding rather than an alias, so
anything that redirects a name -- a test pointing a path at a temp directory --
has to redirect it on the module that owns it, not on this one.

Every function is side-effect free (aside from the small JSON/YAML file readers)
so it can be unit-tested exactly like `swarmforge/agents/translate.py` -- see
`tests/test_tongs_<module>.py`, one per module above.

It performs no orchestration: no docker, no networks, no exec-based secret
resolution, no prompting. Secret resolution is driven by a caller-injected
resolver (see `substitute_secrets`), which keeps the core pure.

YAML parsing reuses the dependency-free subset parser in `swarmforge.yamlite`,
so the launcher needs no third-party packages.
"""

from .approvals import (
    config_hash,
    is_approved,
    is_workspace_sourced,
    load_approvals,
    privilege_summary,
    record_approval,
    save_approvals,
)
from .argv import (
    SHARED_CONTAINER_PREFIX,
    SHARED_NETWORK_PREFIX,
    anvil_option_value,
    inject_anvil_argv,
    org_scope_token,
    session_container_name,
    shared_container_name,
    shared_network_name,
    to_create_argv,
    tong_resource_flags,
    tong_run_argv,
)
from .cli import main
from .discovery import (
    discover,
    load_tong_dir,
    load_tong_file,
    load_yaml,
    merge_tongs,
)
from .mcp import (
    MCP_DEFAULT_PATH,
    MCP_EMITTERS,
    alias_collisions,
    anvil_env,
    anvil_mounts,
    canonical_alias,
    mcp_config_claude,
    mcp_config_opencode,
    mcp_tongs,
    mcp_url,
    plan_injection,
    tong_aliases,
    tong_env_prefix,
    tong_env_var,
)
from .model import (
    DEFAULT_READINESS_TIMEOUT_S,
    ENV_PREFIX,
    INTERFACE_KINDS,
    LABEL_CONFIG_HASH,
    LABEL_TONG_NAME,
    LAYERS,
    LIFECYCLES,
    ORG,
    READINESS_MODES,
    REPO,
    SOCKET_MOUNT,
    TRANSPORTS,
    TRUSTED_LAYERS,
    USER,
    WORKSPACE,
    WORKSPACE_HOST_ENV,
    parse_duration,
    readiness_settings,
    warn,
)
from .mounts import (
    DEFAULT_DOCKER_SOCKET,
    DEFAULT_WORKSPACE_MOUNT_TARGET,
    MOUNT_MODES,
    MOUNT_WORDS,
    WORKSPACE_MOUNT,
    mount_destination,
    mount_target_error,
    normalize_mount_target,
    overlapping_mount_error,
    parse_mount,
    reserved_mount_targets,
    tong_mount_specs,
    workspace_mount_placements,
)
from .network import SESSION_NET_PREFIX, plan_network, session_network_name
from .secrets import (
    ENV_NAME_RE,
    PROVIDER_DEFAULT_KEY,
    PROVIDER_ENTRY_KEYS,
    PROVIDER_OVERRIDES_KEY,
    SECRET_FIFO_ABSENT_EXIT,
    SECRET_FIFO_DIR,
    SECRET_FIFO_TARGET,
    SECRET_FIFO_TMPFS,
    SECRET_INJECT_SHELL,
    SECRET_REF_RE,
    SECRET_REF_TOKEN,
    UnmappedSecretError,
    declared_run_override,
    find_secret_refs,
    load_secret_providers,
    parse_secret_ref,
    partition_secret_env,
    plan_tong_secrets,
    render_secret_exports,
    resolve_exec_target,
    secret_deliver_command,
    secret_inject_argv,
    secret_provider_command,
    substitute_secrets,
)
from .validate import DNS_NAME_MAX_LEN, validate_tong

__all__ = [
    # approvals
    "config_hash",
    "is_approved",
    "is_workspace_sourced",
    "load_approvals",
    "privilege_summary",
    "record_approval",
    "save_approvals",
    # argv
    "SHARED_CONTAINER_PREFIX",
    "SHARED_NETWORK_PREFIX",
    "anvil_option_value",
    "inject_anvil_argv",
    "org_scope_token",
    "session_container_name",
    "shared_container_name",
    "shared_network_name",
    "to_create_argv",
    "tong_resource_flags",
    "tong_run_argv",
    # cli
    "main",
    # discovery
    "discover",
    "load_tong_dir",
    "load_tong_file",
    "load_yaml",
    "merge_tongs",
    # mcp
    "MCP_DEFAULT_PATH",
    "MCP_EMITTERS",
    "alias_collisions",
    "anvil_env",
    "anvil_mounts",
    "canonical_alias",
    "mcp_config_claude",
    "mcp_config_opencode",
    "mcp_tongs",
    "mcp_url",
    "plan_injection",
    "tong_aliases",
    "tong_env_prefix",
    "tong_env_var",
    # model
    "DEFAULT_READINESS_TIMEOUT_S",
    "ENV_PREFIX",
    "INTERFACE_KINDS",
    "LABEL_CONFIG_HASH",
    "LABEL_TONG_NAME",
    "LAYERS",
    "LIFECYCLES",
    "ORG",
    "READINESS_MODES",
    "REPO",
    "SOCKET_MOUNT",
    "TRANSPORTS",
    "TRUSTED_LAYERS",
    "USER",
    "WORKSPACE",
    "WORKSPACE_HOST_ENV",
    "parse_duration",
    "readiness_settings",
    "warn",
    # mounts
    "DEFAULT_DOCKER_SOCKET",
    "DEFAULT_WORKSPACE_MOUNT_TARGET",
    "MOUNT_MODES",
    "MOUNT_WORDS",
    "WORKSPACE_MOUNT",
    "mount_destination",
    "mount_target_error",
    "normalize_mount_target",
    "overlapping_mount_error",
    "parse_mount",
    "reserved_mount_targets",
    "tong_mount_specs",
    "workspace_mount_placements",
    # network
    "SESSION_NET_PREFIX",
    "plan_network",
    "session_network_name",
    # secrets
    "ENV_NAME_RE",
    "PROVIDER_DEFAULT_KEY",
    "PROVIDER_ENTRY_KEYS",
    "PROVIDER_OVERRIDES_KEY",
    "SECRET_FIFO_ABSENT_EXIT",
    "SECRET_FIFO_DIR",
    "SECRET_FIFO_TARGET",
    "SECRET_FIFO_TMPFS",
    "SECRET_INJECT_SHELL",
    "SECRET_REF_RE",
    "SECRET_REF_TOKEN",
    "UnmappedSecretError",
    "declared_run_override",
    "find_secret_refs",
    "load_secret_providers",
    "parse_secret_ref",
    "partition_secret_env",
    "plan_tong_secrets",
    "render_secret_exports",
    "resolve_exec_target",
    "secret_deliver_command",
    "secret_inject_argv",
    "secret_provider_command",
    "substitute_secrets",
    # validate
    "DNS_NAME_MAX_LEN",
    "validate_tong",
]
