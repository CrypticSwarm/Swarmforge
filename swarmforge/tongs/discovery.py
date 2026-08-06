"""Reading tong definitions out of the layer directories and merging them.

One YAML file per tong under `.swarmforge/tongs/`, with the filename as the
tong's identity. Layers are read lowest to highest precedence and merged
wholesale into `{name: {"source": layer, "definition": defn}}` -- the shape the
rest of the package consumes. A missing layer directory is simply empty, which
is what keeps a checkout with no tong definitions inert.
"""

import os

from swarmforge.yamlite import parse_map

from .model import TRUSTED_LAYERS, WORKSPACE, warn


# --- YAML loading -------------------------------------------------------------


def load_yaml(text):
    """Parse a plain-YAML tong document into a dict (empty dict if blank)."""
    lines = text.split("\n")
    data, _ = parse_map(lines, 0, 0)
    return data


def load_tong_file(path):
    """Read and parse a single tong YAML file. Returns the definition dict."""
    with open(path, "r", encoding="utf-8") as handle:
        return load_yaml(handle.read())


# --- Layer discovery ----------------------------------------------------------


def load_tong_dir(path):
    """Discover tong definitions in one layer directory.

    Returns {tong_name: definition}. The tong name is the filename without its
    `.yaml`/`.yml` extension (filename = tong identity). Missing directories
    yield {} so absent layers are simply empty -- the basis of the
    inert-when-empty invariant. Only top-level files are read.
    """
    out = {}
    if not path or not os.path.isdir(path):
        return out
    for filename in sorted(os.listdir(path)):
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue
        full = os.path.join(path, filename)
        if not os.path.isfile(full):
            continue
        name = filename.rsplit(".", 1)[0]
        try:
            out[name] = load_tong_file(full)
        except (ValueError, OSError) as exc:
            warn("skipping %s: %s" % (full, exc))
    return out


def discover(layer_dirs):
    """Discover every layer.

    `layer_dirs` is an ordered list of `(layer_name, path)` pairs, lowest to
    highest precedence (see LAYERS). Returns the same ordered list with each
    path replaced by its `{tong_name: definition}` mapping, ready for
    `merge_tongs`.
    """
    return [(layer, load_tong_dir(path)) for layer, path in layer_dirs]


# --- Merge --------------------------------------------------------------------


def merge_tongs(layers):
    """Merge discovered layers by name into the effective tong set.

    `layers` is an ordered list of `(layer_name, {name: definition})` pairs,
    lowest to highest precedence (the output of `discover`). Returns
    `{name: {"source": layer_name, "definition": definition}}`.

    Rules:
      * Merge by name; a higher layer replaces a lower one **wholesale** (never a
        field-merge), like skill packages.
      * `disable: true` switches off an inherited tong and is itself omitted.
      * Privilege: the (untrusted) workspace layer may **disable** a tong owned
        by a trusted layer but may not **redefine** it -- privileged tongs stay
        owned by trusted layers.

    The `source` records the winning layer, which drives approval gating: only
    workspace-sourced tongs prompt (see `is_workspace_sourced`).
    """
    merged = {}
    for layer, tongs in layers:
        for name in sorted(tongs):
            defn = tongs[name]
            disabled = isinstance(defn, dict) and defn.get("disable") is True
            existing = merged.get(name)
            owned_by_trusted = existing is not None and existing["source"] in TRUSTED_LAYERS

            if layer == WORKSPACE and owned_by_trusted:
                # Workspace may switch a trusted tong off, but not redefine it.
                if disabled:
                    merged.pop(name, None)
                else:
                    warn(
                        "workspace tong '%s' cannot redefine the %s-layer "
                        "definition; keeping the trusted one"
                        % (name, existing["source"])
                    )
                continue

            if disabled:
                merged.pop(name, None)
                continue

            merged[name] = {"source": layer, "definition": defn}
    return merged
