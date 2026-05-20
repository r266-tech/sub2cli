#!/bin/sh
# sub2cli installer.
#
# Local (cloned repo):
#   ./install.sh
#
# One-liner (curl-piped):
#   curl -fsSL https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh | sh
set -eu

REPO="r266-tech/sub2cli"
BRANCH="main"
DEST_DIR="${SUB2CLI_INSTALL_DIR:-${HOME}/.local/bin}"
BINS="sub2cli sub2cli-inject"

mkdir -p "$DEST_DIR"

# 检测是否在 clone 出来的 repo 里跑 (有同目录的 3 个 binary); 否则走 curl 下载
SCRIPT_DIR=""
case "${0:-}" in
  ""|sh|-sh|bash|-bash|-) ;;  # piped from curl
  *) SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || SCRIPT_DIR="" ;;
esac

install_bin() {
  bin="$1"
  src="$SCRIPT_DIR/$bin"
  dest="$DEST_DIR/$bin"
  if [ -n "$SCRIPT_DIR" ] && [ -f "$src" ]; then
    cp "$src" "$dest"
  else
    echo "↓ 下载 $bin 从 $REPO@$BRANCH"
    curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/${bin}" -o "$dest"
  fi
  chmod 755 "$dest"
  echo "已安装: $dest"
}

for b in $BINS; do
  install_bin "$b"
done

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
