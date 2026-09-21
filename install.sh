#!/usr/bin/env bash
set -euo pipefail

choose_target_rc() {
    local requested="${OC_RC_FILE:-}"
    if [[ -n "$requested" ]]; then
        printf '%s' "$requested"
        return 0
    fi

    local shell_name
    shell_name="$(basename "${SHELL:-}")"

    local -a candidates=()
    case "$shell_name" in
        zsh)
            candidates+=("$HOME/.zshrc" "$HOME/.zprofile")
            ;;
        bash|sh)
            candidates+=("$HOME/.bashrc" "$HOME/.bash_profile")
            ;;
    esac

    local platform
    platform="$(uname -s 2>/dev/null || true)"
    if [[ "$platform" == "Darwin" ]]; then
        candidates+=("$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.bashrc")
    else
        candidates+=("$HOME/.bashrc" "$HOME/.bash_profile")
    fi

    candidates+=("$HOME/.profile")

    local dedup=()
    local candidate
    for candidate in "${candidates[@]}"; do
        [[ -z "$candidate" ]] && continue
        local seen=0
        local existing
        for existing in "${dedup[@]}"; do
            if [[ "$existing" == "$candidate" ]]; then
                seen=1
                break
            fi
        done
        if [[ $seen -eq 0 ]]; then
            dedup+=("$candidate")
        fi
    done

    if [[ ${#dedup[@]} -eq 0 ]]; then
        printf '%s' "$HOME/.bashrc"
        return 0
    fi

    for candidate in "${dedup[@]}"; do
        if [[ -f "$candidate" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done

    printf '%s' "${dedup[0]}"
}

# Every path returns 0: a failed link must not fail the install.
link_command() {
    local script_dir="$1"
    local source="$script_dir/bin/swarmforge"
    local bin_dir="$HOME/.local/bin"
    local link="$bin_dir/swarmforge"

    if ! mkdir -p "$bin_dir" 2>/dev/null; then
        echo "Could not create $bin_dir; skipping the swarmforge link" >&2
        return 0
    fi

    # Only a real file is left alone; any symlink, dangling or not, is replaced below.
    if [[ -e "$link" && ! -L "$link" ]]; then
        echo "Not replacing $link: it is not a symlink" >&2
        return 0
    fi

    if ! ln -sfn "$source" "$link" 2>/dev/null; then
        echo "Could not create $link; skipping the swarmforge link" >&2
        return 0
    fi
    echo "Linked $link -> $source"

    case ":${PATH:-}:" in
        *":$bin_dir:"*) ;;
        *) echo "$bin_dir is not on PATH; add it to run swarmforge by name" >&2 ;;
    esac
}

main() {
    if [[ -z "${HOME:-}" ]]; then
        echo "HOME is unset; nothing to install" >&2
        exit 1
    fi

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    link_command "$script_dir"

    local target
    target="$(choose_target_rc)"

    if [[ -z "$target" ]]; then
        echo "Unable to determine which shell rc file to update." >&2
        exit 1
    fi

    if [[ ! -f "$target" ]]; then
        touch "$target"
        echo "Created $target to hold shell customizations"
    fi

    local makefile_path alias_line
    makefile_path="$script_dir/Makefile"
    printf -v alias_line "alias oc='make -f %q run_opencode PROJECT_DIR=\$(pwd)'" "$makefile_path"
    local marker="# Added by Swarmforge installer"

    if grep -Fxq "$alias_line" "$target" 2>/dev/null; then
        echo "Alias already configured in $target"
        return 0
    fi

    {
        echo ""
        echo "$marker"
        echo "$alias_line"
    } >> "$target"

    echo "Alias added to $target"
}

main "$@"
