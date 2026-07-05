#!/bin/bash
# Installs the lovely injector (https://github.com/ethangreen-dev/lovely-injector)
# into the Balatro game folder on macOS. Balatro mods (including Brainstorm) load
# through lovely; on macOS Steam can't inject it, so after installing you launch
# the game with run_lovely_macos.sh instead of the Steam Play button.
#
# Usage:
#   bash install_lovely_macos.sh              # default Steam install location
#   bash install_lovely_macos.sh /path/to/Balatro   # custom game folder
set -euo pipefail

GAME_DIR="${1:-$HOME/Library/Application Support/Steam/steamapps/common/Balatro}"

if [ ! -d "$GAME_DIR/Balatro.app" ]; then
	echo "Could not find Balatro.app in: $GAME_DIR"
	echo "If your game is installed somewhere else, pass the folder as an argument:"
	echo "  bash install_lovely_macos.sh \"/path/to/steamapps/common/Balatro\""
	exit 1
fi

case "$(uname -m)" in
	arm64)  ASSET="lovely-aarch64-apple-darwin.tar.gz" ;;
	x86_64) ASSET="lovely-x86_64-apple-darwin.tar.gz" ;;
	*) echo "Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

URL="https://github.com/ethangreen-dev/lovely-injector/releases/latest/download/$ASSET"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading lovely injector ($ASSET)..."
curl -fsSL "$URL" -o "$TMP/lovely.tar.gz"

echo "Installing into: $GAME_DIR"
tar -xzf "$TMP/lovely.tar.gz" -C "$GAME_DIR" liblovely.dylib run_lovely_macos.sh
chmod +x "$GAME_DIR/run_lovely_macos.sh"
# Clear the Gatekeeper quarantine flag if present (harmless when absent)
xattr -d com.apple.quarantine "$GAME_DIR/liblovely.dylib" 2>/dev/null || true

echo ""
echo "Done. IMPORTANT: launch Balatro with this command (NOT the Steam Play button,"
echo "which skips the injector on macOS):"
echo ""
echo "  \"$GAME_DIR/run_lovely_macos.sh\""
echo ""
echo "Tip: keep that command in your Terminal history, or make an alias."
