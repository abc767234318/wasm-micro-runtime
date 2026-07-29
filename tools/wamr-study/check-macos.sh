#!/usr/bin/env bash

set -u

failed=0

check_command() {
    name="$1"
    command_name="$2"
    hint="$3"

    if command -v "$command_name" >/dev/null 2>&1; then
        printf '[OK] %s: %s\n' "$name" "$(command -v "$command_name")"
    else
        printf '[MISSING] %s: %s\n' "$name" "$hint"
        failed=1
    fi
}

check_command "CMake" "cmake" "Install CMake and add it to PATH."
check_command "LLDB" "lldb" "Install Xcode Command Line Tools or LLVM."
check_command "WABT wat2wasm" "wat2wasm" "Install WABT and add wat2wasm to PATH."
check_command "VS Code CLI" "code" "Install the code command in PATH."

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"

for relative_path in \
    ".vscode/tasks.json" \
    ".vscode/launch.json" \
    ".vscode/c_cpp_properties.json" \
    ".vscode/wasm/minimal.wat" \
    "product-mini/platforms/darwin/CMakeLists.txt"
do
    if [ -f "$repo_root/$relative_path" ]; then
        printf '[OK] %s\n' "$relative_path"
    else
        printf '[MISSING] %s\n' "$relative_path"
        failed=1
    fi
done

if command -v code >/dev/null 2>&1; then
    extensions="$(code --list-extensions 2>/dev/null || true)"
    for extension in ms-vscode.cpptools vadimcn.vscode-lldb
    do
        if printf '%s\n' "$extensions" | grep -Fx "$extension" >/dev/null 2>&1; then
            printf '[OK] VS Code extension: %s\n' "$extension"
        else
            printf '[MISSING] VS Code extension: %s\n' "$extension"
            failed=1
        fi
    done
fi

if [ "$failed" -ne 0 ]; then
    printf '\nmacOS WAMR study environment is not ready.\n'
    exit 1
fi

printf '\nmacOS WAMR study environment is ready.\n'
printf 'Open the repository with: code .\n'
printf 'Then select: WAMR Classic [macOS]: 1 Lifecycle\n'
