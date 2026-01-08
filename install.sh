#!/usr/bin/env bash
set -euo pipefail

main() {
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    local target="$HOME/.bashrc"
    if [[ ! -f "$target" ]]; then
        echo "Expected to find $target but it does not exist. Create it and rerun this installer." >&2
        exit 1
    fi

    local alias_line
    printf -v alias_line "alias oc='make -C %q run_opencode PROJECT_DIR=\$(pwd)'" "$script_dir"
    local marker="# Added by LLM tools installer"

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
