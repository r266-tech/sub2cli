#!/bin/sh
# sub2cli installer.
#
# Local (cloned repo):
#   ./install.sh
#
# One-liner (curl-piped):
#   curl -fsSL https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh | sh
#
# Integrity:
#   These binaries write ~/.codex credentials and shell out to codex/osascript,
#   so downloads are verified against a SHA-256 manifest when one is available.
#   - REF pins which git ref to fetch from (default: main). Set SUB2CLI_REF to a
#     release tag (e.g. v0.2.8) for a reproducible, version-matched install.
#   - The manifest (SHA256SUMS) is fetched from the same REF; each binary's
#     digest is checked before install. If the manifest is absent the installer
#     prints a clear UNVERIFIED warning instead of silently trusting the bytes.
#   - Override a single digest out-of-band with SUB2CLI_SHA256_<bin> if needed.
set -eu

REPO="r266-tech/sub2cli"
REF="${SUB2CLI_REF:-main}"
DEST_DIR="${SUB2CLI_INSTALL_DIR:-${HOME}/.local/bin}"
BINS="sub2cli sub2cli-inject"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/${REF}"

mkdir -p "$DEST_DIR"

# 检测是否在 clone 出来的 repo 里跑 (有同目录的 binary); 否则走 curl 下载
SCRIPT_DIR=""
case "${0:-}" in
  ""|sh|-sh|bash|-bash|-) ;;  # piped from curl
  *) SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || SCRIPT_DIR="" ;;
esac

# --- sha256 helpers (portable across macOS shasum / Linux sha256sum) ---
sha256_of() {
  # echo the hex digest of file $1, or empty string if no tool is available
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
  else
    echo ""
  fi
}

# Fetch the SHA256SUMS manifest once (best-effort). Empty file if unavailable.
MANIFEST="$(mktemp "${TMPDIR:-/tmp}/sub2cli-sums.XXXXXX")" || MANIFEST=""
if [ -n "$MANIFEST" ]; then
  if ! curl -fsSL "${RAW_BASE}/SHA256SUMS" -o "$MANIFEST" 2>/dev/null; then
    : > "$MANIFEST"  # truncate -> "no manifest"
  fi
fi

cleanup() { [ -n "${MANIFEST:-}" ] && rm -f "$MANIFEST" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

expected_sha_for() {
  # env override wins; else look up in the manifest by filename
  bin="$1"
  env_name="SUB2CLI_SHA256_$(printf '%s' "$bin" | tr 'a-z-' 'A-Z_')"
  eval "ov=\${$env_name:-}"
  if [ -n "${ov:-}" ]; then printf '%s' "$ov"; return; fi
  [ -n "${MANIFEST:-}" ] && [ -s "$MANIFEST" ] || { printf ''; return; }
  awk -v f="$bin" '($2==f)||($2=="*"f){print $1; exit}' "$MANIFEST"
}

verify_or_die() {
  bin="$1"; file="$2"
  want="$(expected_sha_for "$bin")"
  if [ -z "$want" ]; then
    echo "  ⚠️  UNVERIFIED: 没有 $bin 的 SHA-256 校验值 (REF=$REF 无 SHA256SUMS)。"
    echo "      已安装但未校验完整性。建议设 SUB2CLI_REF 到带 SHA256SUMS 的 release tag。"
    return 0
  fi
  got="$(sha256_of "$file")"
  if [ -z "$got" ]; then
    echo "✗ 无法计算 SHA-256 (缺 shasum/sha256sum)，拒绝安装未校验的 $bin" >&2
    return 1
  fi
  if [ "$got" != "$want" ]; then
    echo "✗ $bin SHA-256 不匹配！期望 $want 实得 $got — 拒绝安装。" >&2
    return 1
  fi
  echo "  ✓ SHA-256 校验通过"
  return 0
}

install_bin() {
  bin="$1"
  src="$SCRIPT_DIR/$bin"
  dest="$DEST_DIR/$bin"
  tmp="$(mktemp "${dest}.XXXXXX")" || { echo "✗ 无法创建临时文件" >&2; return 1; }

  if [ -n "$SCRIPT_DIR" ] && [ -f "$src" ]; then
    cp "$src" "$tmp"
  else
    echo "↓ 下载 $bin 从 $REPO@$REF"
    if ! curl -fsSL "${RAW_BASE}/${bin}" -o "$tmp"; then
      rm -f "$tmp"
      echo "✗ 下载 $bin 失败" >&2
      return 1
    fi
  fi

  # Reject an empty/truncated download before it can overwrite a good install.
  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    echo "✗ $bin 下载为空 / 被截断，未安装" >&2
    return 1
  fi

  # Verify integrity for the curl path (local clone is trusted source).
  if [ -z "$SCRIPT_DIR" ] || [ ! -f "$src" ]; then
    if ! verify_or_die "$bin" "$tmp"; then
      rm -f "$tmp"
      return 1
    fi
  fi

  chmod 755 "$tmp"
  mv -f "$tmp" "$dest"   # atomic replace; partial download never clobbers
  echo "已安装: $dest"
}

rc=0
for b in $BINS; do
  install_bin "$b" || rc=1
done
[ "$rc" -eq 0 ] || { echo "" ; echo "✗ 安装未全部成功 (见上)" >&2; exit 1; }

echo ""
echo "Python 依赖 (sub2cli 需要):"
echo "  pip3 install --user requests websocket-client"
echo "  (sub2cli-inject 无第三方依赖)"

case ":$PATH:" in
  *":$DEST_DIR:"*) ;;
  *)
    echo ""
    echo "$DEST_DIR 不在 PATH; 添加到 shell profile:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo ""
echo "运行: sub2cli"
