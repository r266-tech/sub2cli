#!/bin/sh
# sub2cli installer.
#
# Local (cloned repo):
#   ./install.sh
#
# One-liner (curl-piped):
#   curl -fsSL https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh | sh
#
# One-command ChatGPT API setup:
#   curl -fsSL https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh \
#     | SUB2CLI_API_URL='https://www.codex2api.com/v1' sh
#
# Integrity:
#   These binaries write ~/.codex credentials and shell out to codex/osascript,
#   so downloads are verified against a SHA-256 manifest when one is available.
#   - REF pins which git ref to fetch from (default: main). Set SUB2CLI_REF to a
#     release tag (e.g. v0.2.15) for a reproducible, version-matched install.
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

DIRECT_AUTH_TMP=""
DIRECT_AUTH_COMMIT_TMP=""
DIRECT_AUTH_ORIGINAL_TMP=""
DIRECT_AUTH_LIVE=""
DIRECT_CONFIG_TMP=""
DIRECT_CONFIG_COMMIT_TMP=""
DIRECT_CONFIG_LIVE=""
DIRECT_BACKUP_DIR=""
DIRECT_TRANSACTION_COMMITTED=0
PROMPT_STTY_STATE=""
PROMPT_STTY_ACTIVE=0
PROMPT_API_KEY_RESULT=""
restore_prompt_tty() {
  if [ "${PROMPT_STTY_ACTIVE:-0}" -eq 1 ] && [ -n "${PROMPT_STTY_STATE:-}" ]; then
    stty "$PROMPT_STTY_STATE" < /dev/tty 2>/dev/null || stty echo < /dev/tty 2>/dev/null || true
  fi
  PROMPT_STTY_ACTIVE=0
  PROMPT_STTY_STATE=""
}
cleanup() {
  original_status=$?
  trap - EXIT
  trap '' INT TERM
  set +e
  restore_prompt_tty
  if [ "${DIRECT_TRANSACTION_COMMITTED:-0}" -ne 1 ]; then
    remove_owned_live_file \
      "${DIRECT_AUTH_TMP:-}" "${DIRECT_AUTH_COMMIT_TMP:-}" "${DIRECT_AUTH_LIVE:-}"
    restore_held_auth_no_clobber \
      "${DIRECT_AUTH_ORIGINAL_TMP:-}" "${DIRECT_AUTH_LIVE:-}" "${DIRECT_BACKUP_DIR:-}"
    remove_owned_live_file \
      "${DIRECT_CONFIG_TMP:-}" "${DIRECT_CONFIG_COMMIT_TMP:-}" "${DIRECT_CONFIG_LIVE:-}"
  else
    [ -n "${DIRECT_AUTH_ORIGINAL_TMP:-}" ] && rm -f "$DIRECT_AUTH_ORIGINAL_TMP" 2>/dev/null
  fi
  [ -n "${MANIFEST:-}" ] && rm -f "$MANIFEST" 2>/dev/null || true
  [ -n "${DIRECT_AUTH_TMP:-}" ] && rm -f "$DIRECT_AUTH_TMP" 2>/dev/null || true
  [ -n "${DIRECT_AUTH_COMMIT_TMP:-}" ] && rm -f "$DIRECT_AUTH_COMMIT_TMP" 2>/dev/null || true
  [ -n "${DIRECT_CONFIG_TMP:-}" ] && rm -f "$DIRECT_CONFIG_TMP" 2>/dev/null || true
  [ -n "${DIRECT_CONFIG_COMMIT_TMP:-}" ] && rm -f "$DIRECT_CONFIG_COMMIT_TMP" 2>/dev/null || true
  PROMPT_API_KEY_RESULT=""
  exit "$original_status"
}
handle_signal() {
  signal_status="$1"
  trap - INT TERM
  exit "$signal_status"
}
trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

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
  PROMPT_STTY_STATE="$(stty -g < /dev/tty 2>/dev/null || true)"
  if [ -n "$PROMPT_STTY_STATE" ] && stty -echo < /dev/tty 2>/dev/null; then
    PROMPT_STTY_ACTIVE=1
  fi
  prompt_read_ok=1
  IFS= read -r key < /dev/tty || { key=""; prompt_read_ok=0; }
  restore_prompt_tty
  printf '\n' > /dev/tty
  PROMPT_API_KEY_RESULT="$key"
  [ "$prompt_read_ok" -eq 1 ]
}

resolve_codex_app_bundle() {
  if [ -n "${SUB2CLI_CHATGPT_APP:-}" ]; then
    printf '%s' "$SUB2CLI_CHATGPT_APP"
    return 0
  fi
  if [ -n "${SUB2CLI_CODEX_APP:-}" ]; then
    printf '%s' "$SUB2CLI_CODEX_APP"
    return 0
  fi
  for app in \
    "/Applications/ChatGPT.app" \
    "${HOME}/Applications/ChatGPT.app" \
    "/Applications/Codex.app" \
    "${HOME}/Applications/Codex.app"
  do
    if [ -d "$app" ]; then
      printf '%s' "$app"
      return 0
    fi
  done
  printf ''
}

restart_codex_if_needed() {
  if is_truthy "${SUB2CLI_API_NO_RESTART:-}" || is_truthy "${SUB2CLI_NO_RESTART:-}"; then
    echo "  未重启 ChatGPT/Codex (--no-restart)"
    return 0
  fi
  app_bundle="$(resolve_codex_app_bundle)"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'tell application id "com.openai.codex" to quit' >/dev/null 2>&1 || true
    sleep 1
  fi
  if command -v open >/dev/null 2>&1; then
    if [ -n "$app_bundle" ]; then
      open "$app_bundle" >/dev/null 2>&1 || true
    else
      open -a ChatGPT >/dev/null 2>&1 || open -a Codex >/dev/null 2>&1 || true
    fi
  fi
}

refuse_unsafe_direct_config() {
  reason="$1"
  api_url="$2"
  echo "✗ 检测到已有 ${reason}，为避免覆盖已拒绝一行直配。" >&2
  echo "  未修改现有 ChatGPT/Codex 配置或连接池状态。" >&2
  printf '  请改用高级配置流程: %s/sub2cli-inject add-api "%s"\n' "$DEST_DIR" "$api_url" >&2
  return 1
}

assert_direct_config_is_safe() {
  api_url="$1"
  codex_home="${CODEX_HOME:-${HOME}/.codex}"
  config_toml="$codex_home/config.toml"
  slots_json="$codex_home/provider-slots.json"

  # Any existing config may contain user-owned state and, more importantly,
  # cannot be created with no-clobber semantics during the final commit.
  if [ -e "$config_toml" ] || [ -L "$config_toml" ]; then
    refuse_unsafe_direct_config "config.toml / 自定义配置" "$api_url"
    return 1
  fi

  # provider-slots.json is the authoritative saved-provider / route-pool state.
  # Even an empty or malformed file must be handled by sub2cli-inject, not
  # silently made inconsistent with a newly written auth/config pair.
  if [ -e "$slots_json" ] || [ -L "$slots_json" ]; then
    refuse_unsafe_direct_config "provider-slots.json / 连接池状态" "$api_url"
    return 1
  fi
}

backup_file_or_die() {
  source_path="$1"
  destination_path="$2"
  label="$3"
  if ! cp "$source_path" "$destination_path"; then
    echo "✗ backup failed for $label; 现有配置未覆盖。" >&2
    return 1
  fi
  if ! chmod 600 "$destination_path"; then
    echo "✗ backup permission setup failed for $label; 现有配置未覆盖。" >&2
    return 1
  fi
}

remove_owned_live_file() {
  expected_path="${1:-}"
  committed_path="${2:-}"
  live_path="${3:-}"
  [ -n "$expected_path" ] && [ -n "$committed_path" ] && [ -n "$live_path" ] || return 0
  if [ -e "$expected_path" ] && [ -e "$committed_path" ] && [ -e "$live_path" ] && \
     [ "$committed_path" -ef "$live_path" ] && cmp -s "$expected_path" "$live_path"; then
    rm -f "$live_path" || return 1
  fi
}

restore_held_auth_no_clobber() {
  held_path="${1:-}"
  live_path="${2:-}"
  backup_dir="${3:-}"
  [ -n "$held_path" ] && { [ -e "$held_path" ] || [ -L "$held_path" ]; } || return 0

  # A hard link is an atomic no-clobber restore for the regular auth files
  # accepted by direct bootstrap. If another process recreated auth.json, its
  # version wins and the held state is moved into the private recovery backup.
  if [ ! -e "$live_path" ] && [ ! -L "$live_path" ]; then
    if ln "$held_path" "$live_path"; then
      rm -f "$held_path" || return 1
      return 0
    fi
    # macOS and GNU mv both provide -n. It is a no-clobber fallback when hard
    # linking is unavailable; a concurrent live-file winner is never replaced.
    if [ ! -e "$live_path" ] && [ ! -L "$live_path" ] && \
       mv -n "$held_path" "$live_path" && \
       { [ -e "$live_path" ] || [ -L "$live_path" ]; } && \
       [ ! -e "$held_path" ] && [ ! -L "$held_path" ]; then
      return 0
    fi
  fi

  if [ -n "$backup_dir" ] && [ -d "$backup_dir" ]; then
    recovery_path="$backup_dir/auth.concurrent.$$.json"
    recovery_index=0
    while [ -e "$recovery_path" ] || [ -L "$recovery_path" ]; do
      recovery_index=$((recovery_index + 1))
      recovery_path="$backup_dir/auth.concurrent.$$.$recovery_index.json"
    done
    if mv "$held_path" "$recovery_path"; then
      chmod 600 "$recovery_path" 2>/dev/null || true
      return 0
    fi
  fi
  return 1
}

rollback_direct_transaction() {
  rollback_ok=1
  remove_owned_live_file \
    "$DIRECT_AUTH_TMP" "$DIRECT_AUTH_COMMIT_TMP" "$DIRECT_AUTH_LIVE" || rollback_ok=0
  restore_held_auth_no_clobber \
    "$DIRECT_AUTH_ORIGINAL_TMP" "$DIRECT_AUTH_LIVE" "$DIRECT_BACKUP_DIR" || rollback_ok=0
  remove_owned_live_file \
    "$DIRECT_CONFIG_TMP" "$DIRECT_CONFIG_COMMIT_TMP" "$DIRECT_CONFIG_LIVE" || rollback_ok=0
  [ "$rollback_ok" -eq 1 ]
}

write_direct_codex_api_config() {
  api_url="$1"
  api_key="$2"
  model="${SUB2CLI_API_MODEL:-gpt-5.6-sol}"
  codex_home="${CODEX_HOME:-${HOME}/.codex}"
  auth_json="$codex_home/auth.json"
  config_toml="$codex_home/config.toml"
  backup_root="$codex_home/provider-switch-backups"
  stamp="$(date +%Y%m%d-%H%M%S 2>/dev/null || date +%s 2>/dev/null || echo now)"
  backup_dir="$backup_root/install-api-$stamp"

  assert_direct_config_is_safe "$api_url" || return 1
  if [ -L "$auth_json" ]; then
    echo "✗ 检测到 auth.json 符号链接；请先用 $DEST_DIR/sub2cli-inject init 迁移后再配置。" >&2
    return 1
  fi
  umask 077
  mkdir -p "$codex_home" "$backup_root" || return 1
  backup_dir="$(mktemp -d "$backup_root/install-api-$stamp.XXXXXX")" || return 1
  chmod 700 "$backup_dir" || return 1
  DIRECT_TRANSACTION_COMMITTED=0
  DIRECT_AUTH_LIVE="$auth_json"
  DIRECT_CONFIG_LIVE="$config_toml"
  DIRECT_BACKUP_DIR="$backup_dir"
  auth_existed=0
  if [ -e "$auth_json" ]; then
    auth_existed=1
    backup_file_or_die "$auth_json" "$backup_dir/auth.json" "auth.json" || return 1
  fi

  # Recheck after backup I/O so a concurrently created custom config/pool is
  # never followed by the destructive writes below.
  assert_direct_config_is_safe "$api_url" || return 1

  api_key_json="$(escape_json_string "$api_key")"
  api_url_toml="$(escape_toml_string "$api_url")"
  model_toml="$(escape_toml_string "$model")"
  DIRECT_AUTH_TMP="$(mktemp "$backup_dir/auth.stage.XXXXXX")" || return 1
  DIRECT_AUTH_COMMIT_TMP="$(mktemp "$backup_dir/auth.commit.XXXXXX")" || return 1
  DIRECT_AUTH_ORIGINAL_TMP="$backup_dir/auth.original"
  DIRECT_CONFIG_TMP="$(mktemp "$backup_dir/config.stage.XXXXXX")" || return 1
  DIRECT_CONFIG_COMMIT_TMP="$(mktemp "$backup_dir/config.commit.XXXXXX")" || return 1
  {
    printf '{\n'
    printf '  "OPENAI_API_KEY": "%s",\n' "$api_key_json"
    printf '  "auth_mode": "apikey"\n'
    printf '}\n'
  } > "$DIRECT_AUTH_TMP"
  chmod 600 "$DIRECT_AUTH_TMP" || return 1
  cp "$DIRECT_AUTH_TMP" "$DIRECT_AUTH_COMMIT_TMP" || return 1
  chmod 600 "$DIRECT_AUTH_COMMIT_TMP" || return 1

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
  } > "$DIRECT_CONFIG_TMP"
  chmod 600 "$DIRECT_CONFIG_TMP" || return 1
  cp "$DIRECT_CONFIG_TMP" "$DIRECT_CONFIG_COMMIT_TMP" || return 1
  chmod 600 "$DIRECT_CONFIG_COMMIT_TMP" || return 1

  # Create config.toml with no-clobber semantics. Keeping the staged hard link
  # lets rollback prove that it only removes the file created by this run.
  assert_direct_config_is_safe "$api_url" || return 1
  if ! ln "$DIRECT_CONFIG_COMMIT_TMP" "$config_toml"; then
    echo "✗ config.toml appeared concurrently; existing state was not overwritten." >&2
    return 1
  fi
  if [ -e "$codex_home/provider-slots.json" ] || [ -L "$codex_home/provider-slots.json" ] || \
     [ ! "$DIRECT_CONFIG_COMMIT_TMP" -ef "$config_toml" ] || \
     ! cmp -s "$DIRECT_CONFIG_TMP" "$config_toml"; then
    rollback_direct_transaction || true
    echo "✗ connection-pool/config state changed concurrently; API setup is being rolled back." >&2
    return 1
  fi

  # Hold the exact current auth with an atomic rename, then verify it against
  # the backup. If ChatGPT refreshed auth after backup, that newer file is
  # restored rather than overwritten. The new auth is linked into place with
  # no-clobber semantics and the stage link remains as rollback ownership proof.
  if [ "$auth_existed" -eq 1 ]; then
    if [ ! -f "$auth_json" ] || [ -L "$auth_json" ] || \
       ! cmp -s "$backup_dir/auth.json" "$auth_json"; then
      rollback_direct_transaction || true
      echo "✗ auth.json changed concurrently; existing state was not overwritten." >&2
      return 1
    fi
    if [ -e "$DIRECT_AUTH_ORIGINAL_TMP" ] || [ -L "$DIRECT_AUTH_ORIGINAL_TMP" ] || \
       ! mv "$auth_json" "$DIRECT_AUTH_ORIGINAL_TMP"; then
      rollback_direct_transaction || true
      echo "✗ auth.json could not be held safely; config.toml is being rolled back." >&2
      return 1
    fi
    if [ -L "$DIRECT_AUTH_ORIGINAL_TMP" ] || \
       ! cmp -s "$backup_dir/auth.json" "$DIRECT_AUTH_ORIGINAL_TMP"; then
      rollback_direct_transaction || true
      echo "✗ auth.json changed during commit; concurrent state was preserved." >&2
      return 1
    fi
  elif [ -e "$auth_json" ] || [ -L "$auth_json" ]; then
    rollback_direct_transaction || true
    echo "✗ auth.json appeared concurrently; existing state was not overwritten." >&2
    return 1
  fi

  if ! ln "$DIRECT_AUTH_COMMIT_TMP" "$auth_json"; then
    rollback_direct_transaction || true
    echo "✗ auth.json appeared concurrently; config.toml is being rolled back." >&2
    return 1
  fi

  if [ -e "$codex_home/provider-slots.json" ] || [ -L "$codex_home/provider-slots.json" ] || \
     [ ! "$DIRECT_CONFIG_COMMIT_TMP" -ef "$config_toml" ] || \
     ! cmp -s "$DIRECT_CONFIG_TMP" "$config_toml" || \
     [ ! "$DIRECT_AUTH_COMMIT_TMP" -ef "$auth_json" ] || \
     ! cmp -s "$DIRECT_AUTH_TMP" "$auth_json"; then
    if rollback_direct_transaction; then
      echo "✗ connection-pool/config state changed concurrently; API setup was rolled back." >&2
    else
      echo "✗ concurrent state change detected and automatic rollback was incomplete; inspect $backup_dir." >&2
    fi
    return 1
  fi

  # Mark the pair committed before deleting rollback evidence. A TERM/INT after
  # this point keeps the valid live pair and only removes private temp files.
  DIRECT_TRANSACTION_COMMITTED=1
  rm -f \
    "$DIRECT_AUTH_ORIGINAL_TMP" \
    "$DIRECT_CONFIG_TMP" "$DIRECT_CONFIG_COMMIT_TMP" \
    "$DIRECT_AUTH_TMP" "$DIRECT_AUTH_COMMIT_TMP" || \
    echo "⚠️  temporary file cleanup will be retried on exit: $backup_dir" >&2
  [ -e "$DIRECT_AUTH_ORIGINAL_TMP" ] || DIRECT_AUTH_ORIGINAL_TMP=""
  [ -e "$DIRECT_CONFIG_TMP" ] || DIRECT_CONFIG_TMP=""
  [ -e "$DIRECT_CONFIG_COMMIT_TMP" ] || DIRECT_CONFIG_COMMIT_TMP=""
  [ -e "$DIRECT_AUTH_TMP" ] || DIRECT_AUTH_TMP=""
  [ -e "$DIRECT_AUTH_COMMIT_TMP" ] || DIRECT_AUTH_COMMIT_TMP=""

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
    PROMPT_API_KEY_RESULT=""
    if prompt_api_key; then
      api_key="$PROMPT_API_KEY_RESULT"
    fi
    PROMPT_API_KEY_RESULT=""
  fi
  if [ -z "$api_key" ]; then
    echo "✗ 缺少 SUB2CLI_API_KEY，无法自动配置 ChatGPT API" >&2
    echo "  示例: curl -fsSL ... | SUB2CLI_API_URL='https://relay.example/v1' SUB2CLI_API_KEY='sk-...' sh" >&2
    return 1
  fi

  echo ""
  echo "ChatGPT API 一键配置:"
  echo "  url: $api_url"
  # Fresh-machine URL + key setup must have one deterministic auth shape.
  # Advanced saved-provider and route-pool workflows remain available through
  # explicit sub2cli-inject commands after installation.
  echo "  mode: direct ~/.codex config"
  write_direct_codex_api_config "$api_url" "$api_key"
}

rc=0
for b in $BINS; do
  install_bin "$b" || rc=1
done
[ "$rc" -eq 0 ] || { echo "" ; echo "✗ 安装未全部成功 (见上)" >&2; exit 1; }

echo ""
echo "Python 依赖 (sub2cli 需要):"
echo "  pip3 install --user requests websocket-client"
echo "  (sub2cli-inject 的高级账号/连接池功能需要 Python 3.10+；一键 API 配置不需要 Python)"

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
