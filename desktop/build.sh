#!/usr/bin/env bash
# desktop/build.sh — build sub2cli.app via PyInstaller, then point at the
# manual codesign / notarize / GitHub release pipeline.
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
#   build/                     — intermediate (gitignored via spike/-style)
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
if timeout 10 dist/sub2cli.app/Contents/MacOS/sub2cli --smoke; then
  echo "✓ bundled app runs"
else
  echo "✗ bundled app failed smoke test (continuing to print codesign hint)"
fi

# --- 3. Codesign + notarize hints (manual, needs V's creds) ---

cat <<EOF

next steps (manual, requires V's Apple Developer credentials):

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
  ditto -c -k --keepParent dist/sub2cli.app dist/sub2cli-\$(date +%Y%m%d).zip
  gh release create v0.1.0 dist/sub2cli-*.zip \\
    --title "sub2cli desktop 0.1.0" --notes "First desktop release"

  # auto-update: Sparkle (native) or GitHub-releases polling (Python).
  # P6 ships the bundle + crash log; Sparkle integration is P6.1 (separate task).
EOF
