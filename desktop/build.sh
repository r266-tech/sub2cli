#!/usr/bin/env bash
# desktop/build.sh — build an unsigned sub2cli.app + zip + DMG via PyInstaller.
#
# Prerequisites:
#   - python3
#
# Run:
#   cd desktop && ./build.sh
#
# Outputs:
#   dist/sub2cli.app           — final .app bundle
#   dist/sub2cli/              — flat onedir output (.app wraps this)
#   dist/sub2cli-<version>.zip — unsigned app zip
#   dist/sub2cli-<version>.dmg — unsigned installer DMG
#   build/                     — intermediate (gitignored)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${SUB2CLI_BUILD_VENV:-$SCRIPT_DIR/.venv}"
BUILD_ROOT="$SCRIPT_DIR/.build"
INJECT_ENTRY="$BUILD_ROOT/sub2cli_inject_entry.py"
INJECT_DIST="$BUILD_ROOT/inject-dist"
INJECT_BIN="$INJECT_DIST/sub2cli-inject-bundle"

cd "$SCRIPT_DIR"

run_pyinstaller_child() {
  env \
    -u _PYI_APPLICATION_HOME_DIR \
    -u _PYI_ARCHIVE_FILE \
    -u _PYI_PARENT_PROCESS_LEVEL \
    PYINSTALLER_RESET_ENVIRONMENT=1 \
    "$@"
}

sign_app_bundle() {
  codesign --force --deep --sign - dist/sub2cli.app
}

# --- 0. Build environment ---

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "→ create build venv: $VENV"
  python3 -m venv "$VENV"
fi

echo "→ ensure PyInstaller/runtime deps"
PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV/bin/python" -m pip install -r requirements.txt pyinstaller >/dev/null

# --- 1. Build bundled injector from source ---

rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"
cp "$REPO_ROOT/sub2cli-inject" "$INJECT_ENTRY"
echo "→ pyinstaller sub2cli-inject (output: $INJECT_BIN)"
"$VENV/bin/pyinstaller" --noconfirm --onefile \
  --name sub2cli-inject-bundle \
  --target-architecture arm64 \
  --distpath "$INJECT_DIST" \
  --workpath "$BUILD_ROOT/inject-build" \
  --specpath "$BUILD_ROOT" \
  "$INJECT_ENTRY"
[[ -x "$INJECT_BIN" ]] || { echo "✗ injector binary not produced: $INJECT_BIN"; exit 1; }

# --- 2. Build .app ---

echo "→ pyinstaller main.spec (output: dist/sub2cli.app)"
rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm main.spec

[[ -d "dist/sub2cli.app" ]] || { echo "✗ no .app produced"; exit 1; }

APP_PYSCRIPTS="dist/sub2cli.app/Contents/Resources/pyscripts"
APP_INJECT_BIN="$APP_PYSCRIPTS/sub2cli-inject-bundle"
mkdir -p "$APP_PYSCRIPTS"
rm -f "$APP_INJECT_BIN"
cp "$INJECT_BIN" "$APP_INJECT_BIN"
chmod 755 "$APP_INJECT_BIN"

for binary in "dist/sub2cli.app/Contents/MacOS/sub2cli" "$APP_INJECT_BIN"; do
  archs="$(lipo -archs "$binary")"
  [[ "$archs" == "arm64" ]] || {
    echo "✗ release binary must be arm64: $binary ($archs)"
    exit 1
  }
done

minimum_system_version="$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' dist/sub2cli.app/Contents/Info.plist)"
[[ "$minimum_system_version" == "26.0" ]] || {
  echo "✗ release app must require macOS 26.0: got $minimum_system_version"
  exit 1
}

sign_app_bundle
if [[ "${SUB2CLI_SKIP_SMOKE:-0}" == "1" ]]; then
  echo "↷ bundled injector smoke skipped (SUB2CLI_SKIP_SMOKE=1)"
elif run_pyinstaller_child "$APP_INJECT_BIN" --help >/dev/null; then
  echo "✓ bundled injector runs"
else
  echo "✗ bundled injector failed smoke test"
  exit 1
fi

size=$(du -sh dist/sub2cli.app | cut -f1)
echo "✓ dist/sub2cli.app  ($size)"

# --- 3. Smoke test ---

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

if [[ "${SUB2CLI_SKIP_SMOKE:-0}" == "1" ]]; then
  echo "↷ bundled app smoke skipped (SUB2CLI_SKIP_SMOKE=1)"
elif run_smoke; then
  echo "✓ bundled app runs"
else
  echo "✗ bundled app failed smoke test"
  exit 1
fi

# --- 4. Unsigned DMG ---

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' dist/sub2cli.app/Contents/Info.plist)"
DMG_STAGE="dist/dmg-stage"
DMG_PATH="dist/sub2cli-${VERSION}.dmg"
ZIP_PATH="dist/sub2cli-${VERSION}.zip"
rm -f "$ZIP_PATH"
ditto -c -k --keepParent dist/sub2cli.app "$ZIP_PATH"
echo "✓ $ZIP_PATH  ($(du -sh "$ZIP_PATH" | cut -f1))"
if [[ "${SUB2CLI_SKIP_DMG:-0}" == "1" ]]; then
  echo "↷ DMG skipped (SUB2CLI_SKIP_DMG=1)"
  exit 0
fi
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

# --- 5. Distribution notes ---

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
  gh release create "v$VERSION" "$DMG_PATH" "$ZIP_PATH" "$REPO_ROOT/SHA256SUMS" \\
    --title "sub2cli desktop $VERSION" \\
    --notes "Unsigned macOS 26+ Apple Silicon desktop build."
EOF
