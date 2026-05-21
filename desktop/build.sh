#!/usr/bin/env bash
# desktop/build.sh — build an unsigned sub2cli.app + DMG via PyInstaller.
#
# Prerequisites:
#   - venv at ../spike/venv with pyinstaller + pywebview + requests + websocket-client + keyring
#   - sub2cli-inject standalone binary at ../spike/dist/sub2cli-inject-bundle
#
# Run:
#   cd desktop && ./build.sh
#
# Outputs:
#   dist/sub2cli.app           — final .app bundle
#   dist/sub2cli/              — flat onedir output (.app wraps this)
#   dist/sub2cli-<version>.dmg — unsigned installer DMG
#   build/                     — intermediate (gitignored)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/spike/venv"
INJECT_BIN="$REPO_ROOT/spike/dist/sub2cli-inject-bundle"

cd "$SCRIPT_DIR"

# --- 0. Prereq checks ---

[[ -x "$VENV/bin/pyinstaller" ]] || {
  echo "✗ pyinstaller not found in $VENV. Run:"
  echo "    python3 -m venv $VENV"
  echo "    $VENV/bin/pip install -r requirements.txt pyinstaller"
  exit 1
}

[[ -f "$INJECT_BIN" ]] || {
  echo "✗ sub2cli-inject standalone binary not found at $INJECT_BIN."
  echo "  Build it first:"
  echo "    cp $REPO_ROOT/sub2cli-inject $REPO_ROOT/spike/sub2cli_inject_entry.py"
  echo "    cd $REPO_ROOT/spike && ./venv/bin/pyinstaller --onefile \\"
  echo "      --name sub2cli-inject-bundle --distpath dist --workpath build \\"
  echo "      --specpath . sub2cli_inject_entry.py"
  exit 1
}

# --- 1. Build .app ---

echo "→ pyinstaller main.spec (output: dist/sub2cli.app)"
rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm main.spec

[[ -d "dist/sub2cli.app" ]] || { echo "✗ no .app produced"; exit 1; }
size=$(du -sh dist/sub2cli.app | cut -f1)
echo "✓ dist/sub2cli.app  ($size)"

# --- 2. Smoke test ---

echo "→ smoke test (--smoke: opens window 1s, exits 0)"
run_smoke() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 90 dist/sub2cli.app/Contents/MacOS/sub2cli --smoke
    return $?
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout 90 dist/sub2cli.app/Contents/MacOS/sub2cli --smoke
    return $?
  fi

  dist/sub2cli.app/Contents/MacOS/sub2cli --smoke &
  local pid=$!
  for _ in {1..90}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      wait "$pid"
      return $?
    fi
    sleep 1
  done
  kill "$pid" >/dev/null 2>&1 || true
  wait "$pid" >/dev/null 2>&1 || true
  return 124
}

if run_smoke; then
  echo "✓ bundled app runs"
else
  echo "✗ bundled app failed smoke test (continuing to package unsigned dmg)"
fi

# --- 3. Unsigned DMG ---

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' dist/sub2cli.app/Contents/Info.plist)"
DMG_STAGE="dist/dmg-stage"
DMG_PATH="dist/sub2cli-${VERSION}.dmg"
rm -rf "$DMG_STAGE" "$DMG_PATH"
mkdir -p "$DMG_STAGE"
cp -R dist/sub2cli.app "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create \
  -volname "sub2cli ${VERSION}" \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$DMG_PATH"
rm -rf "$DMG_STAGE"
echo "✓ $DMG_PATH  ($(du -sh "$DMG_PATH" | cut -f1))"

# --- 4. Distribution notes ---

cat <<EOF

unsigned distribution:

  $DMG_PATH

Gatekeeper bypass after drag-installing to /Applications:

  xattr -dr com.apple.quarantine /Applications/sub2cli.app
  open /Applications/sub2cli.app

optional signed distribution later (requires Apple Developer credentials):

  # codesign with Developer ID Application cert
  codesign --deep --force --verify --verbose=2 \\
    --sign "Developer ID Application: <Your Name> (TEAMID)" \\
    --options runtime \\
    --timestamp \\
    dist/sub2cli.app

  # zip for notarytool
  ditto -c -k --keepParent dist/sub2cli.app dist/sub2cli.zip

  # notarize (uses keychain profile previously stored with notarytool store-credentials)
  xcrun notarytool submit dist/sub2cli.zip \\
    --keychain-profile "sub2cli-notary" \\
    --wait

  # staple
  xcrun stapler staple dist/sub2cli.app

  # GitHub release (gh CLI)
  gh release create "v$VERSION" "$DMG_PATH" \\
    --title "sub2cli desktop $VERSION" --notes "Unsigned macOS desktop build."
EOF
