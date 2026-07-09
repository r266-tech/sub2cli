#!/bin/sh
# sub2cli installer.
#
# Local (cloned repo):
#   ./install.sh
#
# One-liner (curl-piped):
#   curl -fsSL https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh | sh
#
# One-command Codex API setup:
#   curl -fsSL https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh \
#     | SUB2CLI_API_URL='https://www.codex2api.com/v1' sh
#
# Integrity:
#   These binaries write ~/.codex credentials and shell out to codex/osascript,
#   so downloads are verified against a SHA-256 manifest when one is available.
#   - REF pins which git ref to fetch from (default: main). Set SUB2CLI_REF to a
#     release tag (e.g. v0.2.12) for a reproducible, version-matched install.
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
  *) SCRIPT_DIR=$(CDPATH=; cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || SCRIPT_DIR="" ;;
esac

LOCAL_SOURCE=0
if [ -n "$SCRIPT_DIR" ]; then
  LOCAL_SOURCE=1
  for b in $BINS; do
    [ -f "$SCRIPT_DIR/$b" ] || LOCAL_SOURCE=0
  done
fi

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
  if [ "$LOCAL_SOURCE" -eq 1 ]; then
    : > "$MANIFEST"
  elif ! curl -fsSL "${RAW_BASE}/SHA256SUMS" -o "$MANIFEST" 2>/dev/null; then
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

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

has_python310() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

normalize_api_url() {
  url="$1"
  if [ -z "$url" ]; then
    echo "✗ SUB2CLI_API_URL 不能为空" >&2
    return 1
  fi
  case "$url" in
    http://*|https://*) ;;
    *)
      echo "✗ SUB2CLI_API_URL 必须以 http:// 或 https:// 开头: $url" >&2
      return 1
      ;;
  esac
  while [ "${url%/}" != "$url" ]; do
    url="${url%/}"
  done
  case "$url" in
    */v1|*/V1) printf '%s' "$url" ;;
    *) printf '%s/v1' "$url" ;;
  esac
}

escape_json_string() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

escape_toml_string() {
  escape_json_string "$1"
}

prompt_api_key() {
  if [ ! -r /dev/tty ] || [ ! -w /dev/tty ]; then
    return 1
  fi
  printf 'API key (hidden): ' > /dev/tty
  old_stty="$(stty -g < /dev/tty 2>/dev/null || true)"
  stty -echo < /dev/tty 2>/dev/null || true
  IFS= read -r key < /dev/tty || key=""
  [ -n "$old_stty" ] && stty "$old_stty" < /dev/tty 2>/dev/null || true
  printf '\n' > /dev/tty
  printf '%s' "$key"
}

restart_codex_if_needed() {
  if is_truthy "${SUB2CLI_API_NO_RESTART:-}" || is_truthy "${SUB2CLI_NO_RESTART:-}"; then
    echo "  未重启 Codex (--no-restart)"
    return 0
  fi
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'tell application "Codex" to quit' >/dev/null 2>&1 || true
    sleep 1
  fi
  if command -v open >/dev/null 2>&1; then
    if [ -n "${SUB2CLI_CODEX_APP:-}" ]; then
      open "$SUB2CLI_CODEX_APP" >/dev/null 2>&1 || true
    else
      open -a Codex >/dev/null 2>&1 || true
    fi
  fi
}

write_direct_codex_api_config() {
  api_url="$1"
  api_key="$2"
  model="${SUB2CLI_API_MODEL:-gpt-5.5}"
  codex_home="${CODEX_HOME:-${HOME}/.codex}"
  auth_json="$codex_home/auth.json"
  config_toml="$codex_home/config.toml"
  backup_root="$codex_home/provider-switch-backups"
  stamp="$(date +%Y%m%d-%H%M%S 2>/dev/null || date +%s 2>/dev/null || echo now)"
  backup_dir="$backup_root/install-api-$stamp"

  [ -e "$backup_dir" ] && backup_dir="$backup_dir-$$"
  mkdir -p "$codex_home" "$backup_dir"
  if [ -e "$auth_json" ] || [ -L "$auth_json" ]; then
    cp "$auth_json" "$backup_dir/auth.json" 2>/dev/null && chmod 600 "$backup_dir/auth.json" 2>/dev/null || true
  fi
  if [ -e "$config_toml" ] || [ -L "$config_toml" ]; then
    cp "$config_toml" "$backup_dir/config.toml" 2>/dev/null && chmod 600 "$backup_dir/config.toml" 2>/dev/null || true
  fi

  api_key_json="$(escape_json_string "$api_key")"
  api_url_toml="$(escape_toml_string "$api_url")"
  model_toml="$(escape_toml_string "$model")"
  umask 077
  auth_tmp="$auth_json.$$"
  config_tmp="$config_toml.$$"
  {
    printf '{\n'
    printf '  "OPENAI_API_KEY": "%s",\n' "$api_key_json"
    printf '  "auth_mode": "apikey"\n'
    printf '}\n'
  } > "$auth_tmp"
  mv -f "$auth_tmp" "$auth_json"
  chmod 600 "$auth_json" 2>/dev/null || true

  {
    printf 'model = "%s"\n' "$model_toml"
    printf 'model_provider = "OpenAI"\n'
    printf 'api_base_url = "https://api.openai.com/v1"\n'
    printf 'disable_response_storage = true\n'
    printf '\n'
    printf '[model_providers.OpenAI]\n'
    printf 'name = "OpenAI"\n'
    printf 'base_url = "%s"\n' "$api_url_toml"
    printf 'wire_api = "responses"\n'
    printf 'requires_openai_auth = true\n'
  } > "$config_tmp"
  mv -f "$config_tmp" "$config_toml"
  chmod 600 "$config_toml" 2>/dev/null || true

  echo "  已写入: $auth_json"
  echo "  已写入: $config_toml"
  echo "  备份目录: $backup_dir"
  restart_codex_if_needed
}

bootstrap_codex_api() {
  raw_url="${SUB2CLI_API_URL:-}"
  [ -n "$raw_url" ] || return 0
  api_url="$(normalize_api_url "$raw_url")" || return 1
  api_key="${SUB2CLI_API_KEY:-}"
  if [ -z "$api_key" ]; then
    api_key="$(prompt_api_key || true)"
  fi
  if [ -z "$api_key" ]; then
    echo "✗ 缺少 SUB2CLI_API_KEY，无法自动配置 Codex API" >&2
    echo "  示例: curl -fsSL ... | SUB2CLI_API_URL='https://relay.example/v1' SUB2CLI_API_KEY='sk-...' sh" >&2
    return 1
  fi

  echo ""
  echo "Codex API 一键配置:"
  echo "  url: $api_url"

  inject="$DEST_DIR/sub2cli-inject"
  if has_python310 && ! is_truthy "${SUB2CLI_FORCE_DIRECT_CONFIG:-}"; then
    echo "  mode: sub2cli-inject"
    set -- add-api "$api_url" --api-key-stdin
    if is_truthy "${SUB2CLI_API_SKIP_CHECK:-}"; then
      set -- "$@" --skip-check
    fi
    if [ -n "${SUB2CLI_API_MODEL:-}" ]; then
      set -- "$@" --model "$SUB2CLI_API_MODEL"
    fi
    if is_truthy "${SUB2CLI_API_NO_RESTART:-}" || is_truthy "${SUB2CLI_NO_RESTART:-}"; then
      set -- "$@" --no-restart
    fi
    printf '%s' "$api_key" | "$inject" "$@"
  else
    if has_python310; then
      echo "  mode: direct ~/.codex config"
    elif command -v python3 >/dev/null 2>&1; then
      echo "  mode: direct ~/.codex config (python3 < 3.10)"
    else
      echo "  mode: direct ~/.codex config (python3 not found)"
    fi
    write_direct_codex_api_config "$api_url" "$api_key"
  fi
}

rc=0
for b in $BINS; do
  install_bin "$b" || rc=1
done
[ "$rc" -eq 0 ] || { echo "" ; echo "✗ 安装未全部成功 (见上)" >&2; exit 1; }

echo ""
echo "Python 依赖 (sub2cli 需要):"
echo "  pip3 install --user requests websocket-client"
echo "  (sub2cli-inject 需要 Python 3.10+，但无第三方依赖；无 Python 时一键配置会走直写兜底)"

bootstrap_codex_api

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
