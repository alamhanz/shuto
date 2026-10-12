#!/bin/sh
# shuto uninstaller for macOS and Linux. Per-user, no sudo needed.
#
# Usage (once the repository is public):
#   curl -LsSf https://raw.githubusercontent.com/alamhanz/shuto/main/install/uninstall.sh | sh
#
# Options (when running the downloaded file):
#   --yes           do not ask for confirmation
#   --delete-data   also delete shuto's own data (database, thumbnails, settings)
#   --keep-login    keep the saved Shutterstock login
# Your photos, Ollama and its models are never touched. uv itself is not removed.
set -eu

OPTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|--delete-data|--keep-login) OPTS="$OPTS $1" ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '[shuto] %s\n' "$1"; }

for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -d "$d" ] && PATH="$d:$PATH"
done

say "Step 1 of 2: removing shuto icons, saved login and (if you choose) data..."
SHUTO=""
if command -v shuto >/dev/null 2>&1; then
    SHUTO="$(command -v shuto)"
elif command -v uv >/dev/null 2>&1 && [ -x "$(uv tool dir --bin)/shuto" ]; then
    SHUTO="$(uv tool dir --bin)/shuto"
fi
if [ -n "$SHUTO" ]; then
    # When piped from curl, stdin is this script, so questions must be read from the terminal.
    if [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
        exec 3</dev/tty
    else
        exec 3<&0
    fi
    # shellcheck disable=SC2086
    if "$SHUTO" uninstall $OPTS <&3; then
        :
    else
        case "$OPTS" in
            *--yes*) say "shuto could not finish its cleanup; continuing." ;;
            *) say "Stopped. Nothing was removed."; exit 0 ;;
        esac
    fi
else
    say "The shuto program was not found. Removing the icons only."
    rm -f "$HOME/Desktop/shuto.command" "$HOME/Applications/shuto.command" \
        "$HOME/Desktop/shuto.desktop" "$HOME/.local/share/applications/shuto.desktop"
fi

say "Step 2 of 2: removing the shuto program..."
if command -v uv >/dev/null 2>&1; then
    uv tool uninstall shuto || say "shuto was not installed with uv, or is already removed."
else
    say "uv was not found, so there is nothing more to remove."
fi

say "Done. Your photos, Ollama and its models were not touched."
say "To remove a model: ollama rm <model>. To remove uv: uv cache clean, then delete the uv and uvx files from ~/.local/bin."
