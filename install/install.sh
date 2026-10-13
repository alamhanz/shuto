#!/bin/sh
# shuto installer for macOS and Linux. Per-user, no sudo needed.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/alamhanz/shuto/main/install/install.sh | sh
#
# Options (when running the downloaded file):
#   --from <source>   wheel path, PyPI name or URL to install instead of the latest release
#                     (when run from a copy of the repository, that copy is installed)
#   --no-shortcut     do not create launchers
#   --no-start        do not start shuto at the end
# The SHUTO_SOURCE environment variable does the same as --from.
# With none of these, the latest release on GitHub is installed.
set -eu

# The "latest release" page redirects to the newest tag (no API, so no rate limit).
LATEST_RELEASE_PAGE="https://github.com/alamhanz/shuto/releases/latest"
RELEASE_DOWNLOAD="https://github.com/alamhanz/shuto/releases/download"
SOURCE="${SHUTO_SOURCE:-}"
# uv installer pinned to a known release (0.12.22, current when this was written) so a changed
# "latest" installer cannot alter what runs here. Raise it on purpose after checking.
UV_INSTALLER="https://astral.sh/uv/0.12.22/install.sh"
SHORTCUT=1
START=1

while [ $# -gt 0 ]; do
    case "$1" in
        --from) [ $# -ge 2 ] || { echo "--from needs a value" >&2; exit 2; }; SOURCE="$2"; shift ;;
        --no-shortcut) SHORTCUT=0 ;;
        --no-start) START=0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# Run from a downloaded or cloned copy of the repository (and no source given)? Then build and
# install that copy. When piped into sh ("curl | sh") there is no script path, so this is skipped.
CHECKOUT=""
case "$0" in
    *install.sh)
        ROOT="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)" || ROOT=""
        if [ -n "$ROOT" ] && [ -f "$ROOT/pyproject.toml" ] && grep -q '^name *= *"shuto"' "$ROOT/pyproject.toml"; then
            CHECKOUT="$ROOT"
        fi
        ;;
esac

say() { printf '[shuto] %s\n' "$1"; }
fail() {
    printf '[shuto] Something went wrong: %s\n' "$1" >&2
    printf '[shuto] See docs/install.md (Troubleshooting) or run the line again.\n' >&2
    exit 1
}

# --from is parsed above, so decide the final source only now.
USE_LATEST=0
[ -n "$SOURCE" ] || [ -n "$CHECKOUT" ] || USE_LATEST=1

# Find the newest release; print "<version> <wheel url>" or nothing. The version comes from where
# the "latest" page redirects to; the wheel's existence is checked too. No jq needed.
latest_release() {
    final="$(curl -fsSL -o /dev/null -w '%{url_effective}' -H 'User-Agent: shuto-installer' "$LATEST_RELEASE_PAGE" 2>/dev/null)" || return 1
    case "$final" in
        */releases/tag/v*) ver="${final##*/releases/tag/v}" ;;
        *) return 1 ;;
    esac
    printf '%s\n' "$ver" | grep -Eq '^[0-9]+(\.[0-9]+)*([.-]?(a|b|rc|alpha|beta|dev|post)\.?[0-9]+)?$' || return 1
    url="$RELEASE_DOWNLOAD/v$ver/shuto-$ver-py3-none-any.whl"
    curl -fsSLI -o /dev/null -H 'User-Agent: shuto-installer' "$url" 2>/dev/null || return 1
    printf '%s %s\n' "$ver" "$url"
}

# Do the lookup before anything is installed, so a failure leaves the computer untouched.
if [ "$USE_LATEST" = 1 ]; then
    LATEST="$(latest_release)" || LATEST=""
    [ -n "$LATEST" ] || fail "Could not find the latest shuto release on GitHub. Check your internet connection and run this line again (on a shared network, try again later), or download it from https://github.com/alamhanz/shuto/releases"
    say "Installing shuto ${LATEST%% *}..."
    SOURCE="shuto[heic] @ ${LATEST#* }"
fi

say "Step 1 of 5: checking for the uv installer helper..."
if ! command -v uv >/dev/null 2>&1; then
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        [ -x "$d/uv" ] && PATH="$d:$PATH" && break
    done
fi
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (a small tool that also installs Python for you)..."
    curl -LsSf "$UV_INSTALLER" | sh || fail "could not install uv. Check your internet connection."
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv was installed but could not be found. Open a new Terminal and run this line again."
fi

# Percent-encode a path for a file:// URI (keeps letters, digits, - . _ ~ and /).
uri_encode() {
    printf '%s' "$1" | od -An -v -tx1 | tr -s ' 
' '

' | while read -r h; do
        [ -n "$h" ] || continue
        case "$h" in
            2d|2e|2f|3[0-9]|4[1-9a-f]|5[0-9a]|5f|6[1-9a-f]|7[0-9a]|7e)
                # shellcheck disable=SC2059
                o="$(printf '%03o' "0x$h")"
                printf "\\${o}" ;;
            *) printf '%%%s' "$(printf '%s' "$h" | tr 'a-f' 'A-F')" ;;
        esac
    done
}

BUILD_DIR=""
cleanup() { [ -z "$BUILD_DIR" ] || rm -rf "$BUILD_DIR"; }
trap cleanup EXIT
trap 'exit 1' INT TERM
if [ -z "$SOURCE" ] && [ "$USE_LATEST" = 0 ]; then
    say "Preparing shuto from this folder..."
    BUILD_DIR="$(mktemp -d)"
    uv build --wheel --python 3.12 --out-dir "$BUILD_DIR" "$CHECKOUT" || fail "could not prepare shuto from this folder. Check your internet connection."
    WHEEL="$(ls "$BUILD_DIR"/shuto-*.whl 2>/dev/null | head -n 1)"
    [ -n "$WHEEL" ] || fail "could not prepare shuto from this folder."
    SOURCE="shuto[heic] @ file://$(uri_encode "$WHEEL")"
fi

say "Step 2 of 5: installing shuto (this can take a few minutes the first time)..."
uv tool install --force --python 3.12 "$SOURCE" || fail "the shuto install did not finish. Check your internet connection."
cleanup

say "Step 3 of 5: making the shuto command available..."
uv tool update-shell >/dev/null 2>&1 || true
BIN_DIR="$(uv tool dir --bin)"
PATH="$BIN_DIR:$PATH"
[ -x "$BIN_DIR/shuto" ] || fail "shuto was installed but $BIN_DIR/shuto was not found."

if [ "$SHORTCUT" = 1 ]; then
    say "Step 4 of 5: creating the shuto launcher..."
    "$BIN_DIR/shuto" shortcut || say "Could not create the launcher. You can still start shuto by typing: shuto"
else
    say "Step 4 of 5: skipping the launcher."
fi

say "Step 5 of 5: checking for Ollama (the free AI helper that runs on your computer)..."
if command -v ollama >/dev/null 2>&1 || [ -d /Applications/Ollama.app ]; then
    say "Ollama is installed."
else
    say "Ollama is not installed yet. shuto needs it to write titles and keywords."
    say "Download it from https://ollama.com/download, install it, then come back."
    if [ "$START" = 1 ]; then
        if command -v open >/dev/null 2>&1; then
            open "https://ollama.com/download" >/dev/null 2>&1 || true
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "https://ollama.com/download" >/dev/null 2>&1 || true
        fi
    fi
fi

say "Done. Next time, double-click the shuto launcher, or type: shuto"
if [ "$START" = 1 ]; then
    say "Starting shuto in your browser..."
    exec "$BIN_DIR/shuto"
fi
